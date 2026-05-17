"""analytics_engine — coordinator + sub-agents + tool registry + Groq client.

Consolidates the former app.tools (TurnState, registry, Groq, SSE emitter,
cost guard, 14 tools) and app.agents (coordinator + 7 sub-agents +
DashboardAgent + DataCleanAgent) into a single module.

Cross-module rule: imports go down — `app.infrastructure` only — never up.
The HTTP layer (`app.core_system`) imports from this module.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure import (
    ALLOWED_TABLES,
    SCHEMA_COLUMNS,
    count_rows,
    fetch_all,
    fetch_one,
    insert_rows,
    put_cached,
    quoted,
    resolve_entities,
    schema_dict,
    settings,
)


# ===========================================================================
# 1. TURN STATE
# ===========================================================================

class ToolCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    ok: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    iteration: int = 0


class TurnState(BaseModel):
    """Single source of truth for a query turn. Frozen — use `apply()`."""

    model_config = ConfigDict(frozen=True)

    # Identity / input
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str
    cache_key: str | None = None
    # Single-user MVP — only conversation_id matters for follow-up continuity.
    conversation_id: str | None = None

    # Dispatch
    route: str | None = None              # set by RouteClassifier
    sub_agent: str | None = None          # set by Coordinator dispatcher

    # Intent / extraction
    intent: dict[str, Any] | None = None  # set by IntentAnalyzer
    time_window: dict[str, Any] | None = None  # set by TimeKPI
    granularity: str | None = None        # set by TimeKPI
    kpis: list[dict[str, Any]] = Field(default_factory=list)  # set by TimeKPI
    entities: list[dict[str, Any]] = Field(default_factory=list)  # set by EntityResolver

    # Schema / SQL
    db_schema: dict[str, Any] | None = None
    sql_plan: dict[str, Any] | None = None
    sql_draft: str | None = None
    sql_final: str | None = None

    # Result
    rows: list[dict[str, Any]] | None = None
    aggregates: dict[str, Any] | None = None
    insights: dict[str, Any] | None = None
    chart_data: dict[str, Any] | None = None

    # Output
    final_answer: str | None = None
    response_record: dict[str, Any] | None = None

    # Metrics / housekeeping
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    bytes_scanned: int = 0
    iteration: int = 0

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def apply(self, **updates: Any) -> "TurnState":
        return self.model_copy(update=updates)

    def append_tool_call(self, record: ToolCallRecord) -> "TurnState":
        return self.model_copy(update={"tool_calls": [*self.tool_calls, record]})

    def append_error(self, error: str) -> "TurnState":
        return self.model_copy(update={"errors": [*self.errors, error]})


# ===========================================================================
# 2. SSE EVENT EMITTER  (extracted to app.orchestrator_v2.monitoring.sse
#    during P9 sunset prep — re-exported here for v1 backward compat)
# ===========================================================================

from app.orchestrator_v2.monitoring.sse import (  # noqa: E402
    EventEmitter,
    format_comment,
    format_sse,
    _COMMENT_MARKER,
)


# ===========================================================================
# 3. COST GUARD — hard limits on iterations, spend, scan size.
# ===========================================================================

class CostGuardError(Exception):
    """Raised when a per-turn budget is exceeded."""


def check_loop_iteration(state: TurnState) -> None:
    if state.iteration >= settings.max_loop_iterations:
        raise CostGuardError(
            f"Loop iteration limit reached ({settings.max_loop_iterations})"
        )


def check_cost(state: TurnState) -> None:
    if state.cost_usd > settings.cost_limit_usd:
        raise CostGuardError(
            f"Cost limit exceeded: ${state.cost_usd:.4f} > ${settings.cost_limit_usd}"
        )


def check_sql_scan_estimate(estimated_bytes: int) -> None:
    if estimated_bytes > settings.sql_max_bytes_scanned:
        raise CostGuardError(
            f"SQL scan estimate too large: {estimated_bytes} bytes "
            f"> {settings.sql_max_bytes_scanned}"
        )


# ===========================================================================
# 4. GROQ CLIENT  (extracted to app.orchestrator_v2.llm.groq during P9
#    sunset prep — re-exported here for v1 backward compat)
# ===========================================================================

from app.orchestrator_v2.llm.groq import (  # noqa: E402
    GroqMessage,
    GroqResponse,
    GroqStreamChunk,
    GroqToolCall,
    GroqToolResponse,
    GroqClient,
    _err_response,
    _err_tool_response,
    _err_chunk,
    parse_strict_json,
    set_request_groq,
    reset_request_groq,
    get_groq,
)

# Legacy alias — a handful of log statements elsewhere in this module
# still reference the local name. The new home of the logger is
# orchestrator_v2/llm/groq.py.
_groq_log = logging.getLogger("agentic_ai.groq")


# (GroqClient + parse_strict_json + set/reset/get_request_groq removed —
#  they now live in app.orchestrator_v2.llm.groq and are re-exported
#  via the import block above.)


# ===========================================================================
# 5. TOOL BASE + ToolResult + `require` helper
# ===========================================================================

_tool_log = logging.getLogger("agentic_ai.tools")


class ToolResult(BaseModel):
    ok: bool
    output: Any = None
    state_updates: dict[str, Any] = {}
    delta_metrics: dict[str, float] = {}
    error: str | None = None
    duration_ms: float = 0.0


class Tool(ABC):
    name: str = ""
    description: str = ""
    args_model: type[BaseModel] = BaseModel
    independent: bool = True

    @abstractmethod
    async def run(self, state: TurnState, args: BaseModel) -> ToolResult:
        ...

    async def execute(self, state: TurnState, raw_args: dict[str, Any]) -> ToolResult:
        """Validate args, run, never raise. Sets duration_ms."""
        start = time.perf_counter()
        try:
            args = self.args_model(**(raw_args or {}))
        except Exception as e:
            _tool_log.warning("tool %s arg validation failed: %s", self.name, e)
            return ToolResult(
                ok=False,
                error=f"Invalid args for {self.name}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        try:
            result = await self.run(state, args)
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        except Exception as e:
            _tool_log.exception("tool %s failed for turn %s", self.name, state.turn_id)
            return ToolResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )


_REQUIRE_SENTINEL = object()


def require(state: TurnState, *fields: str) -> ToolResult | None:
    """Helper: ensures predecessor TurnState fields exist; returns a failing
    ToolResult on miss, or None on success.

    `None` (or attribute missing) is treated as "tool didn't run yet".
    An empty list / dict / string is treated as a VALID successful result —
    e.g. SqlExecutor legitimately returns `state.rows = []` when the SQL
    matched 0 rows. Downstream tools must handle that case themselves."""
    for f in fields:
        v = getattr(state, f, _REQUIRE_SENTINEL)
        if v is _REQUIRE_SENTINEL or v is None:
            return ToolResult(
                ok=False,
                error=f"prerequisite '{f}' not set in TurnState",
            )
    return None


# ===========================================================================
# 6. TOOL IMPLEMENTATIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# 6.1 Database — RESTRICTED (pin-gated) storage tool.
# ---------------------------------------------------------------------------

INGESTION_PIN = "DCA_INGESTION_ONLY"
READ_PIN = "DA_READ_ONLY"


class DatabaseArgs(BaseModel):
    op: str = Field(description="'insert' or 'select'")
    pin: str = ""
    table: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    source: str = "upload"
    file_name: str | None = None
    row_hashes: list[str] | None = None
    sql: str | None = None
    params: list[Any] = Field(default_factory=list)


class DatabaseTool(Tool):
    name = "Database"
    description = (
        "Restricted storage tool. INGESTION_PIN allows insert into sales/purchase; "
        "READ_PIN allows arbitrary SELECT. Not callable from the LLM coordinator."
    )
    args_model = DatabaseArgs
    independent = True

    async def run(self, state: TurnState, args: DatabaseArgs) -> ToolResult:
        op = (args.op or "").lower()
        if op == "insert":
            if args.pin != INGESTION_PIN:
                return ToolResult(ok=False, error="Database.insert requires INGESTION_PIN")
            if args.table not in ALLOWED_TABLES:
                return ToolResult(ok=False, error=f"unknown table {args.table!r}")
            if not args.batch_id:
                return ToolResult(ok=False, error="batch_id required for insert")
            inserted = await asyncio.to_thread(
                insert_rows,
                args.table,
                list(args.rows),
                batch_id=args.batch_id,
                source=args.source or "upload",
                file_name=args.file_name,
                row_hashes=args.row_hashes,
            )
            return ToolResult(
                ok=True,
                output={
                    "table": args.table,
                    "rows_inserted": inserted,
                    "table_total": await count_rows(args.table),
                },
            )

        if op == "select":
            if args.pin != READ_PIN:
                return ToolResult(ok=False, error="Database.select requires READ_PIN")
            if not args.sql:
                return ToolResult(ok=False, error="sql required for select")
            rows = await fetch_all(args.sql, tuple(args.params))
            return ToolResult(
                ok=True,
                output={"row_count": len(rows), "rows": rows},
            )

        return ToolResult(ok=False, error=f"unknown op {op!r}")


# ---------------------------------------------------------------------------
# 6.2 RouteClassifier
# ---------------------------------------------------------------------------

_RCA_KW       = ("why ", "why did", "what caused", "what's driving", "drop", "decline",
                 "fell", "decreased", "down ", "root cause", "rca")
_FORECAST_KW  = ("forecast", "predict", "projection", "next month", "next 30",
                 "next 7", "future", "upcoming")
_ANALYTICS_KW = ("trend", "compare", "comparison", "vs ", "versus", "growth",
                 "change over", "movement")
_PURCHASE_KW  = ("purchase", "purchases", "supplier", "vendor", "bought", "buy ")
_SALES_KW     = ("sale", "sales", "revenue", "income", "earned", "earnings",
                 "turnover", "order", "orders", "transaction", "customer",
                 "buyer", "product", "best seller", "top selling",
                 "how much", "how many", "total ", "this month", "last month",
                 "this week", "last week", "today", "yesterday")


class RouteClassifierArgs(BaseModel):
    pass


class RouteClassifierTool(Tool):
    name = "RouteClassifier"
    description = "Classifies the question into a coarse route label."
    args_model = RouteClassifierArgs
    independent = True

    async def run(self, state: TurnState, args: RouteClassifierArgs) -> ToolResult:
        q = (state.question or "").lower().strip()
        if not q:
            return ToolResult(ok=False, error="empty question")

        if any(k in q for k in _RCA_KW):
            route = "RCA"
        elif any(k in q for k in _FORECAST_KW):
            route = "FORECAST"
        elif any(k in q for k in _ANALYTICS_KW):
            route = "ANALYTICS"
        elif any(k in q for k in _PURCHASE_KW):
            route = "PURCHASE_QUERY"
        elif any(k in q for k in _SALES_KW):
            route = "SALES_QUERY"
        else:
            route = "UNKNOWN"

        return ToolResult(
            ok=True,
            output={"route": route},
            state_updates={"route": route},
        )


# ---------------------------------------------------------------------------
# 6.3 IntentAnalyzer
# ---------------------------------------------------------------------------

# ---- Typo normalization (deterministic — no LLM) -------------------------
#
# Two layers:
#   1. _TYPO_MAP — hand-curated common misspellings of domain words. Cheap,
#      O(1) lookup, keeps known typos stable across releases.
#   2. difflib.get_close_matches against _DOMAIN_VOCAB — catches anything
#      that's within ~1-2 edits of a real domain word AND wasn't in the
#      explicit map. Cutoff is 0.85 + minimum length 5 to avoid corrupting
#      legitimate short words ("hi", "ok", numbers).
#
# Normalization runs at the top of `classify_intent` and `classify_query_kind`
# so every downstream layer sees the corrected text. Pure substitution — we
# do not change capitalization or punctuation, just word tokens.

import difflib as _difflib  # local alias, avoid namespace pollution

_TYPO_MAP: dict[str, str] = {
    # product / item family
    "produt": "product", "produkt": "product", "prodcut": "product",
    "prodct": "product", "produsts": "products", "produts": "products",
    "iteam": "item", "itam": "item",
    # selling / sold
    "sellling": "selling", "selliing": "selling", "sellign": "selling",
    "selled": "sold", "soldd": "sold",
    # revenue / sales / profit
    "reveneu": "revenue", "revneu": "revenue", "revanue": "revenue",
    "revnue": "revenue", "saless": "sales", "salses": "sales",
    "profitablee": "profitable", "profittable": "profitable",
    # ranking superlatives
    "topest": "top", "topp": "top", "tops": "top",
    "lowst": "lowest", "loweest": "lowest",
    "wrost": "worst", "wost": "worst",
    "higest": "highest", "higheset": "highest", "hihest": "highest",
    "biggset": "biggest",
    # time
    "mounth": "month", "monht": "month", "montgh": "month", "moth": "month",
    "wek": "week", "weak": "week",
    "yestrday": "yesterday", "yseterday": "yesterday",
    "tody": "today", "todya": "today",
    # entity nouns
    "custmer": "customer", "custome": "customer", "customrs": "customers",
    "buyr": "buyer", "buyers": "buyers",
    "catgory": "category", "categry": "category", "catagory": "category",
    "brnd": "brand", "brnads": "brands",
    "vendr": "vendor", "supplie": "supplier",
    "skus": "sku",
}

# Anchor vocabulary for the difflib fallback. Keep this tight — only words
# that DOMAIN matters for. Padding it with generic English would cause
# false corrections (e.g. "than" → "then" is wrong here).
_DOMAIN_VOCAB: frozenset[str] = frozenset({
    # entities
    "product", "products", "item", "items", "sku",
    "brand", "brands", "category", "categories",
    "customer", "customers", "buyer", "buyers", "client", "clients",
    "vendor", "vendors", "supplier", "suppliers", "party", "parties",
    "salesperson", "salespeople", "channel", "channels",
    # metrics
    "sales", "selling", "sold", "sale", "revenue", "income",
    "profit", "profits", "profitable", "earnings", "turnover",
    "orders", "order", "units", "quantity",
    # ranking superlatives
    "top", "bottom", "best", "worst", "highest", "lowest",
    "most", "least", "biggest", "smallest", "leading",
    # time
    "today", "yesterday", "month", "months", "week", "weeks",
    "year", "years", "day", "days", "quarter",
    # qualifiers
    "performing", "performance", "performer", "popular",
})


def _normalize_typos(question: str) -> str:
    """Correct common misspellings of domain terms. Pure-deterministic — no
    LLM. Tokens that are already correct (in `_DOMAIN_VOCAB`) are left
    alone; tokens in `_TYPO_MAP` are substituted; otherwise a difflib
    fuzzy match (cutoff 0.85, min length 5) is consulted.

    Preserves whitespace and punctuation exactly so downstream regex /
    keyword matching keeps working unchanged."""
    if not question:
        return question
    out: list[str] = []
    for tok in re.split(r"(\W+)", question):
        if not tok or not tok.isalpha():
            out.append(tok)
            continue
        low = tok.lower()
        if low in _DOMAIN_VOCAB:
            out.append(tok)
            continue
        if low in _TYPO_MAP:
            corrected = _TYPO_MAP[low]
            # Preserve simple capitalization on the first letter.
            out.append(corrected.capitalize() if tok[0].isupper() else corrected)
            continue
        if len(low) >= 5:
            close = _difflib.get_close_matches(low, _DOMAIN_VOCAB, n=1, cutoff=0.85)
            if close:
                corrected = close[0]
                out.append(corrected.capitalize() if tok[0].isupper() else corrected)
                continue
        out.append(tok)
    return "".join(out)


_METRIC_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:gmv|gross merchandise|sales|revenue|turnover|earned|earnings)\b", re.I), "total_amount"),
    (re.compile(r"\b(?:order count|orders|transactions|number of (?:sales|orders))\b", re.I), "orders"),
    (re.compile(r"\b(?:customers?|buyers?|users?|distinct (?:parties|customers))\b", re.I), "customers"),
    (re.compile(r"\b(?:aov|average order value)\b", re.I), "aov"),
    (re.compile(r"\b(?:refunds?|returns?|loyalty redeemed)\b", re.I), "refunds"),
]

_COMPARISON_RE = re.compile(
    r"\b(compare|vs\.?|versus|change|movement|growth|delta)\b", re.I
)
_TOP_RE = re.compile(r"\btop\s+(\d+)\b", re.I)


# ---------------------------------------------------------------------------
# Fine-grained intent classifier (used by Coordinator + IntentAnalyzer).
#
# Returns (intent_type, confidence, hints). The coordinator uses the type
# to deterministically pick a sub-agent when confidence is high; the
# downstream tools (SqlPlanner, ResultAggregator, ResponseFormatter)
# branch on the type to produce specialized SQL / aggregates / narrative.
#
# Rules are ordered: more-specific intents (forecasting, RCA) are checked
# before generic intents (sales_summary). First match wins; multiple
# matches inside a single rule boost confidence slightly.
# ---------------------------------------------------------------------------

INTENT_TYPES: tuple[str, ...] = (
    "sales_summary",
    "product_performance",
    "root_cause_analysis",
    "forecasting",
    "trend_analysis",
    "comparison",
    "anomaly_detection",
    "purchase_analysis",
)


_INTENT_RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("forecasting", 0.95, (
        "forecast", "predict", "projection", "project sales",
        "next week", "next month", "next 7", "next 14", "next 30",
        "next quarter", "next year", "future sales", "upcoming sales",
        "will be", "expected sales", "estimate next",
    )),
    ("root_cause_analysis", 0.95, (
        "why did", "why are", "why is", "why has", "why have",
        "why sales", "why revenue", "why orders", "why customers",
        "what caused", "what's causing", "whats causing",
        "what is causing", "what's driving", "whats driving",
        "root cause", " rca ", "reason for", "reason behind",
        "drop in sales", "drop in revenue", "decline in",
        "sales dropped", "revenue dropped", "orders dropped",
        "sales fell", "sales declined", "explain the drop",
        "explain decline", "why the drop",
    )),
    ("comparison", 0.92, (
        "compare ", "compared to", "compared with",
        " vs ", " vs.", "versus", "this month vs",
        "last month vs", "this week vs", "last week vs",
        "this year vs", "difference between", "delta between",
        "change vs", "month over month", "year over year",
        "yoy", "mom", "wow",
    )),
    ("product_performance", 0.92, (
        "top performing", "top performer", "top item", "top items",
        "top product", "top products", "top brand", "top brands",
        "top shoe", "top shoes", "top sneaker", "top sneakers",
        "best seller", "best-seller", "best selling", "top selling",
        "best product", "best item", "best brand", "best shoe",
        "ranking", "leaderboard",
        "most sold", "most popular", "most ordered", "most bought",
        "highest revenue by", "lowest revenue by",
        "highest revenue product", "highest revenue item",
        "highest revenue brand", "highest selling",
        "biggest seller", "biggest sale",
        "best customer", "top customer", "biggest customer",
        "best vendor", "top vendor", "biggest vendor",
        "best buyer", "top buyer", "biggest buyer",
        "best parties", "top parties", "biggest parties",
        "best performing", "top performing",
        "worst customer", "worst client", "worst buyer",
        "lowest customer", "lowest client", "lowest buyer",
        "highest sales by", "lowest sales by",
        "by customer", "by party", "by vendor", "by supplier",
        "by product", "by item", "by brand",
        "which product", "which item", "which brand", "which shoe",
        "what product", "what item", "what brand",
        "top n ", "rank by ",
        # Bottom / lowest / worst / least — same intent, opposite sort
        # direction. The `rank_direction` hint flips the ORDER BY.
        "lowest selling", "lowest sale", "least selling", "least sold",
        "worst selling", "worst performing", "worst performer",
        "bottom selling", "bottom seller", "bottom performer",
        "least popular", "least ordered", "smallest seller",
        "underperforming", "lowest performing",
    )),
    ("anomaly_detection", 0.85, (
        "anomaly", "anomalies", "outlier", "outliers",
        "unusual", "spike", "spikes", "abnormal",
        "irregularity", "dip in",
    )),
    ("trend_analysis", 0.85, (
        "trend", "trends", "trending", "growth rate",
        "growth over", "movement over", "moving up", "moving down",
        "change over time", "growing", "shrinking",
        "rising", "falling", "trajectory",
    )),
    ("purchase_analysis", 0.88, (
        "purchase ", "purchases", "supplier", "suppliers",
        "vendor", "vendors", "bought ", "buying ",
        "expense", "expenses", "spending on",
        "cost of goods", "cogs",
    )),
    ("sales_summary", 0.82, (
        "sales", "revenue", "income", "earning", "earnings",
        "turnover", "total sale", "total revenue",
        "gmv", "income earned", "how much",
    )),
)


# "top 5 customers", "top 10 items", "bottom 3 vendors", … — a high-signal
# pattern that should always route to product_performance.
_RANK_NOUN_RE = re.compile(
    r"\b(?:top|bottom)\s+(\d+)\s+"
    r"(customer|client|buyer|item|product|seller|vendor|supplier|"
    r"party|performer|brand|sku)s?\b",
    re.IGNORECASE,
)

# Words that signal the *subject* of a ranking — customer/party (the buyer)
# vs product/item/brand (what was sold). Used to pick GROUP BY column for
# product_performance plans.
_CUSTOMER_RANK_KWS = (
    "customer", "client", "buyer", "party", "patron", "guest",
    "biggest spender", "top spender", "regulars",
)
_PRODUCT_RANK_KWS = (
    "product", "item", "brand", "model", "sku", "seller", "shoe",
    "footwear", "sneaker", "merchandise", "stock", "inventory",
)


def _ranking_subject(question: str) -> str:
    """Classify a product_performance query as ranking customers vs products.

    Default is "product" — most retail "top performers" questions mean
    "what sold the most", not "who bought the most".
    """
    q = (question or "").lower()
    for kw in _CUSTOMER_RANK_KWS:
        if kw in q:
            return "customer"
    for kw in _PRODUCT_RANK_KWS:
        if kw in q:
            return "product"
    return "product"


# Bottom-of-ranking signals. When any of these match the (already typo-
# normalized) question, the ranking plan inverts ORDER BY direction so the
# WORST sellers come first instead of the best.
_BOTTOM_RANK_KWS: tuple[str, ...] = (
    "lowest", "least", "worst", "bottom", "smallest",
    "underperforming", "under-performing",
)


def _ranking_direction(question: str) -> str:
    """Return 'asc' if the question asks for the lowest/worst/bottom of a
    ranking, else 'desc' (the default — top/best/highest)."""
    q = (question or "").lower()
    for kw in _BOTTOM_RANK_KWS:
        if f" {kw} " in f" {q} " or q.startswith(kw + " ") or q.endswith(" " + kw):
            return "asc"
    return "desc"


def classify_intent(question: str) -> tuple[str, float, dict[str, Any]]:
    """Deterministic intent classifier — returns (type, confidence, hints).

    Confidence is a float in [0.0, 0.99]. Treat ≥ 0.7 as high-confidence
    (use deterministic routing); below that, callers may invoke a
    semantic fallback.

    The question is run through `_normalize_typos` first so typo-heavy
    phrasings ("topest selling produt", "highest reveneu") still hit the
    keyword vocabulary. Normalization preserves whitespace and punctuation
    exactly so this is byte-compatible with the original keyword scan.
    """
    if not question or not question.strip():
        return "sales_summary", 0.0, {"matched": None, "reason": "empty"}

    question = _normalize_typos(question)
    q = " " + question.lower().strip() + " "

    # Special-case: "top N <ranking-noun>" — the rule loop's keywords list
    # can't capture every singular/plural variant cleanly so we promote
    # this pattern to product_performance with high confidence.
    m_rank = _RANK_NOUN_RE.search(q)
    if m_rank:
        try:
            n = int(m_rank.group(1))
        except ValueError:
            n = 10
        # The ranking-noun itself ('customer'/'product'/...) tells us the
        # subject directly — bypass the keyword scan.
        rank_noun = m_rank.group(2).lower()
        subject = (
            "customer" if rank_noun in ("customer", "client", "buyer", "party", "performer")
            else "product"
        )
        return "product_performance", 0.96, {
            "matched":          m_rank.group(0).strip(),
            "matches":          1,
            "top_n":            max(1, min(n, 50)),
            "ranking_subject":  subject,
            "rank_direction":   "asc" if m_rank.group(0).lower().lstrip().startswith("bottom") else "desc",
        }

    # Use space-padded matching for short tokens to avoid false hits inside
    # other words (e.g. "sales" should not match in "wholesale supplier").
    def _needle(kw: str) -> str:
        return kw if len(kw) >= 5 else f" {kw.strip()} "

    for intent_type, base_conf, kws in _INTENT_RULES:
        first_match: str | None = None
        hits = 0
        for kw in kws:
            if _needle(kw) in q:
                hits += 1
                if first_match is None:
                    first_match = kw
        if first_match is not None:
            conf = min(0.99, base_conf + (hits - 1) * 0.02)
            top_n_match = _TOP_RE.search(q)
            top_n = (
                int(top_n_match.group(1)) if top_n_match
                else (10 if intent_type == "product_performance" else None)
            )
            hints: dict[str, Any] = {
                "matched": first_match,
                "matches": hits,
                "top_n":   top_n,
            }
            if intent_type == "product_performance":
                hints["ranking_subject"] = _ranking_subject(question)
                hints["rank_direction"] = _ranking_direction(question)
            return intent_type, conf, hints

    # No rule matched — low-confidence default.
    return "sales_summary", 0.4, {"matched": None, "top_n": None}


# Sales-overview detection (short, plain "what's my sales / show my sales" type).
_OVERVIEW_KEYWORDS: tuple[str, ...] = (
    "sales", "revenue", "income", "earnings", "turnover", "gmv",
)
_OVERVIEW_DEMOTERS: tuple[str, ...] = (
    "by ", " in ", " from ", "between", "during", "for ", "per ",
    "last ", "this ", "yesterday", "today", "tomorrow", " ago",
    "month", "week", "day ", "year", "quarter", "hour",
    "daily", "weekly", "monthly", "yearly", "quarterly",
    "ytd", "year to date",
    "vs ", "vs.", "versus", "compare", "compared",
    "growth", "trend", "drop", "decline", "fall", "fell", "rose",
    "why", "what caused", "rca", "root cause",
    "forecast", "predict", "projection",
    "top ", "bottom ", "best ", "worst ", "highest", "lowest",
    "by customer", "by product", "by party",
    "jan ", "january", "feb ", "february", "mar ", "march",
    "apr ", "april", "may ", "jun ", "june",
    "jul ", "july", "aug ", "august", "sep ", "september",
    "oct ", "october", "nov ", "november", "dec ", "december",
)
_MAX_OVERVIEW_TOKENS = 6


def _is_sales_overview(question: str) -> bool:
    q = (question or "").lower().strip()
    if not q:
        return False
    if not any(k in q for k in _OVERVIEW_KEYWORDS):
        return False
    if len(q.split()) > _MAX_OVERVIEW_TOKENS:
        return False
    padded = f" {q} "
    for d in _OVERVIEW_DEMOTERS:
        if d in padded:
            return False
    return True


class IntentAnalyzerArgs(BaseModel):
    pass


class IntentAnalyzerTool(Tool):
    name = "IntentAnalyzer"
    description = (
        "Extracts metric / filters / comparison / overview flags from the "
        "question. Sets state.intent."
    )
    args_model = IntentAnalyzerArgs
    independent = True

    async def run(self, state: TurnState, args: IntentAnalyzerArgs) -> ToolResult:
        q = (state.question or "").strip()
        if not q:
            return ToolResult(ok=False, error="empty question")

        metric = "total_amount"
        for pat, name in _METRIC_MAP:
            if pat.search(q):
                metric = name
                break

        comparison = bool(_COMPARISON_RE.search(q))
        top = None
        m = _TOP_RE.search(q)
        if m:
            try:
                top = int(m.group(1))
            except ValueError:
                top = None
        overview = _is_sales_overview(q)

        # If the coordinator pre-classified the intent, preserve its type +
        # confidence + hints. Otherwise classify here so this tool always
        # produces a fully-populated intent dict.
        existing = state.intent or {}
        if existing.get("type"):
            intent_type = existing["type"]
            confidence  = float(existing.get("confidence") or 0.0)
            hints       = dict(existing.get("hints") or {})
        else:
            intent_type, confidence, hints = classify_intent(q)

        # Resolve top_n: explicit "top N" wins, otherwise the type's default.
        if top is None and isinstance(hints.get("top_n"), int):
            top = hints["top_n"]

        intent = {
            "type":       intent_type,
            "confidence": confidence,
            "hints":      hints,
            "metric":     metric,
            "comparison": comparison,
            "top_n":      top,
            "overview":   overview,
            "raw":        q,
        }
        return ToolResult(ok=True, output=intent, state_updates={"intent": intent})


# ---------------------------------------------------------------------------
# 6.4 TimeKPI — anchor-aware time window + KPI list + granularity.
# ---------------------------------------------------------------------------

_time_log = logging.getLogger("agentic_ai.time_kpi")

_KPI_CATALOG: dict[str, dict] = {
    "gmv":      {"name": "GMV",         "expression": 'SUM("Total Amount")',           "unit": "INR"},
    "revenue":  {"name": "Revenue",     "expression": 'SUM("Total Amount")',           "unit": "INR"},
    "orders":   {"name": "Orders",      "expression": "COUNT(*)",                       "unit": "count"},
    "aov":      {"name": "AOV",         "expression": 'SUM("Total Amount")/COUNT(*)',   "unit": "INR"},
    "users":    {"name": "Customers",   "expression": 'COUNT(DISTINCT "Party Name")',   "unit": "count"},
    "refunds":  {"name": "Refunds",     "expression": 'SUM("Loyalty Redeemed")',        "unit": "INR"},
}

_KPI_ALIASES: dict[str, str] = {
    "sales": "gmv", "gmv": "gmv", "revenue": "revenue", "net revenue": "revenue",
    "orders": "orders", "transactions": "orders",
    "aov": "aov", "average order value": "aov",
    "users": "users", "customers": "users", "buyers": "users",
    "refunds": "refunds", "returns": "refunds",
}

_GRANULARITY_HINTS: dict[str, str] = {
    "hour": "hourly", "hourly": "hourly",
    "day": "daily", "daily": "daily",
    "week": "weekly", "weekly": "weekly",
    "month": "monthly", "monthly": "monthly",
    "quarter": "quarterly", "quarterly": "quarterly",
    "year": "yearly", "yearly": "yearly",
}


async def _table_max_date(table: str) -> date | None:
    """Return the maximum normalized Date in `table`, or None if empty / error."""
    if table not in ALLOWED_TABLES:
        return None
    try:
        row = await fetch_one(
            f'SELECT MAX("Date") AS max_d '
            f'FROM {quoted(table)} '
            f'WHERE "Date" IS NOT NULL AND "Date" GLOB \'????-??-??\''
        )
    except Exception:
        _time_log.warning("TimeKPI: MAX(Date) probe failed on %s", table, exc_info=True)
        return None
    if row is None:
        return None
    raw = row.get("max_d")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _resolve_anchor(route: str | None) -> tuple[date, str]:
    """Choose the reference date for all relative time phrases.

    Returns (anchor, source) where source is one of:
      "sales" | "purchase" | "system_clock"
    """
    primary, secondary = ("purchase", "sales") if route == "PURCHASE_QUERY" else ("sales", "purchase")
    for table in (primary, secondary):
        max_d = await _table_max_date(table)
        if max_d is not None:
            return max_d, table
    return date.today(), "system_clock"


class TimeKPIArgs(BaseModel):
    pass


class TimeKPITool(Tool):
    name = "TimeKPI"
    description = (
        "Sets state.time_window, state.kpis, and state.granularity from the "
        "user's question, anchored to the latest date present in the dataset "
        "(MAX(Date) from sales / purchase) so historical datasets aren't "
        "filtered out of their own time range. Falls back to today() only when "
        "neither table has any data. Defaults: last 30 days, GMV, daily."
    )
    args_model = TimeKPIArgs
    independent = False

    async def run(self, state: TurnState, args: TimeKPIArgs) -> ToolResult:
        q = (state.question or "").lower()
        if not q:
            return ToolResult(ok=False, error="empty question")

        anchor, anchor_source = await _resolve_anchor(state.route)
        _time_log.info(
            "TimeKPI anchor: %s (source=%s, route=%s)",
            anchor.isoformat(), anchor_source, state.route,
        )

        start, end = self._time_window(q, anchor)
        granularity = self._granularity(q)
        kpis = self._kpis(q)

        window = {
            "start_date":     start.isoformat(),
            "end_date":       end.isoformat(),
            "as_of":          anchor.isoformat(),
            "anchor_source":  anchor_source,
        }
        return ToolResult(
            ok=True,
            output={"time_window": window, "kpis": kpis, "granularity": granularity},
            state_updates={
                "time_window": window,
                "kpis": kpis,
                "granularity": granularity,
            },
        )

    @staticmethod
    def _time_window(q: str, anchor: date) -> tuple[date, date]:
        m = re.search(r"last\s+(\d+)\s+(day|week|month|year)s?", q)
        if m:
            n = int(m.group(1)); unit = m.group(2)
            if unit == "day":   return anchor - timedelta(days=n), anchor
            if unit == "week":  return anchor - timedelta(weeks=n), anchor
            if unit == "month": return anchor - timedelta(days=n * 30), anchor
            return anchor.replace(year=anchor.year - n), anchor
        if "yesterday" in q:
            d = anchor - timedelta(days=1); return d, d
        if "today" in q:
            return anchor, anchor
        if "this week" in q:
            return anchor - timedelta(days=anchor.weekday()), anchor
        if "last week" in q:
            ws = anchor - timedelta(days=anchor.weekday())
            return ws - timedelta(days=7), ws - timedelta(days=1)
        if "this month" in q:
            return anchor.replace(day=1), anchor
        if "last month" in q:
            first = anchor.replace(day=1)
            end = first - timedelta(days=1)
            return end.replace(day=1), end
        if "this quarter" in q:
            q_start_month = ((anchor.month - 1) // 3) * 3 + 1
            return anchor.replace(month=q_start_month, day=1), anchor
        if "this year" in q:
            return anchor.replace(month=1, day=1), anchor
        if "last year" in q:
            ystart = anchor.replace(year=anchor.year - 1, month=1, day=1)
            yend = anchor.replace(year=anchor.year - 1, month=12, day=31)
            return ystart, yend
        if "ytd" in q or "year to date" in q:
            return anchor.replace(month=1, day=1), anchor
        return anchor - timedelta(days=30), anchor

    @staticmethod
    def _granularity(q: str) -> str:
        for k, v in _GRANULARITY_HINTS.items():
            if k in q:
                return v
        return "daily"

    @staticmethod
    def _kpis(q: str) -> list[dict]:
        seen, out = set(), []
        for alias in sorted(_KPI_ALIASES, key=len, reverse=True):
            if alias in q:
                canon = _KPI_ALIASES[alias]
                if canon not in seen:
                    out.append(dict(_KPI_CATALOG[canon]))
                    seen.add(canon)
        if not out:
            out.append(dict(_KPI_CATALOG["gmv"]))
        return out


# ---------------------------------------------------------------------------
# 6.5 EntityResolver
# ---------------------------------------------------------------------------

class EntityResolverArgs(BaseModel):
    pass


class EntityResolverTool(Tool):
    name = "EntityResolver"
    description = (
        "Resolves merchant / category / product mentions in the question "
        "against the synonyms memory store. Falls back to the vector "
        "retrieval layer for typo / paraphrase cases. Sets state.entities."
    )
    args_model = EntityResolverArgs
    independent = True

    async def run(self, state: TurnState, args: EntityResolverArgs) -> ToolResult:
        if not state.question:
            return ToolResult(ok=False, error="empty question")
        # Stage 1: deterministic synonym match (legacy behaviour). The
        # synonyms.json substring-match is the right primary path because
        # it's exact, fast, and yields high-confidence canonical names.
        entities = resolve_entities(state.question)

        # Stage 2: semantic fallback. Only fires when stage 1 is empty —
        # we never replace a deterministic hit with a fuzzy one. The
        # vector vocabulary is registered at boot from the synonyms file,
        # plus dynamically extended at upload time with new product /
        # customer names from the inserted batch.
        if not entities:
            from app.vector import hybrid_retrieve  # local import (cycle-safe)
            try:
                matches = hybrid_retrieve(state.question, limit=3)
            except Exception:
                # Vector retrieval must NEVER break the analytics pipeline —
                # log + continue with empty entities.
                _agents_log.exception("vector retrieval failed")
                matches = []
            for m in matches:
                # Only accept semantic hits at the source level; exact /
                # alias hits would have been caught by stage 1 anyway.
                # Cosine ≥ 0.55 already enforced in hybrid_retrieve.
                entities.append({
                    "canonical":       m.canonical,
                    "matched_aliases": [m.matched_text],
                    "source":          m.source,
                    "confidence":      round(m.score, 3),
                })

        return ToolResult(
            ok=True,
            output={"entities": entities},
            state_updates={"entities": entities},
        )


# ---------------------------------------------------------------------------
# 6.6 SchemaRetriever
# ---------------------------------------------------------------------------

class SchemaRetrieverArgs(BaseModel):
    pass


class SchemaRetrieverTool(Tool):
    name = "SchemaRetriever"
    description = "Returns the canonical financial DB schema as a dict."
    args_model = SchemaRetrieverArgs
    independent = True

    async def run(self, state: TurnState, args: SchemaRetrieverArgs) -> ToolResult:
        schema = schema_dict()
        return ToolResult(ok=True, output=schema, state_updates={"db_schema": schema})


# ---------------------------------------------------------------------------
# 6.7 SqlPlanner
# ---------------------------------------------------------------------------

_BUCKET_BY_GRAN: dict[str, str] = {
    "hourly":    "strftime('%Y-%m-%d %H', \"Date\")",
    "daily":     '"Date"',
    "weekly":    "strftime('%Y-W%W', \"Date\")",
    "monthly":   "strftime('%Y-%m', \"Date\")",
    "quarterly": "strftime('%Y-%m', \"Date\")",
    "yearly":    "strftime('%Y', \"Date\")",
}


class SqlPlannerArgs(BaseModel):
    pass


class SqlPlannerTool(Tool):
    name = "SqlPlanner"
    description = (
        "Plans the SQL query — branches on state.intent.type so different "
        "intents (sales_summary, product_performance, forecasting, …) emit "
        "different SELECT / GROUP BY / ORDER BY shapes."
    )
    args_model = SqlPlannerArgs
    independent = False

    async def run(self, state: TurnState, args: SqlPlannerArgs) -> ToolResult:
        miss = require(state, "intent", "time_window")
        if miss:
            return miss

        intent = state.intent or {}
        intent_type = intent.get("type") or "sales_summary"

        # Route → table. purchase_analysis intent forces the purchase table;
        # otherwise honour the legacy RouteClassifier route field.
        if intent_type == "purchase_analysis" or state.route == "PURCHASE_QUERY":
            table = "purchase"
        else:
            table = "sales"

        if intent_type == "product_performance":
            plan = self._plan_ranking(state, table)
        elif intent_type == "anomaly_detection":
            plan = self._plan_default(state, table, kind="anomaly")
        elif intent_type == "trend_analysis":
            plan = self._plan_default(state, table, kind="trend")
        elif intent_type == "forecasting":
            plan = self._plan_default(state, table, kind="forecast")
        elif intent_type in ("comparison", "root_cause_analysis"):
            # Comparison-bearing intents still produce a default daily series
            # for the *current* window; ResultAggregator runs the second
            # (previous-period) probe and merges totals.
            plan = self._plan_default(
                state, table,
                kind="comparison" if intent_type == "comparison" else "rca",
            )
        else:
            plan = self._plan_default(state, table, kind="summary")

        # Stash the intent so SqlValidator + ResultAggregator can reason
        # about kind without re-reading state.intent.
        plan["intent_type"] = intent_type
        state_updates: dict[str, Any] = {"sql_plan": plan}
        # Forecast plan forces daily granularity (the forecast projector only
        # handles ISO YYYY-MM-DD buckets); reflect that in state so
        # ResultAggregator + chart payload stay consistent.
        if intent_type == "forecasting":
            state_updates["granularity"] = "daily"
        return ToolResult(ok=True, output=plan, state_updates=state_updates)

    # ---- planners ---------------------------------------------------------

    @staticmethod
    def _entity_filters(state: TurnState) -> list[dict]:
        """Translate detected entities into WHERE filters. Previously always
        filtered against Party Name; that's wrong for product/brand mentions
        (Nike, Sneaker A) which live in Product Name. We now route by the
        active ranking_subject hint:

          - product_performance + ranking_subject=product   → Product Name
          - product_performance + ranking_subject=customer  → Party Name
          - everything else                                 → Party Name
                                                             (legacy default)

        A future improvement is to support OR-of-filters so an ambiguous
        entity can match BOTH columns; that requires extending SqlWriter's
        WHERE rendering, which is out of scope here.
        """
        intent = state.intent or {}
        hints = intent.get("hints") or {}
        intent_type = intent.get("type")
        ranking_subject = (hints.get("ranking_subject") or "").lower()

        if intent_type == "product_performance" and ranking_subject == "product":
            target_col = "Product Name"
        elif intent_type == "product_performance" and ranking_subject == "customer":
            target_col = "Party Name"
        else:
            target_col = "Party Name"

        out: list[dict] = []
        for ent in state.entities or []:
            canonical = ent.get("canonical")
            if canonical:
                out.append({
                    "col": target_col,
                    "op": "LIKE",
                    "value": f"%{canonical}%",
                })
        return out

    @staticmethod
    def _date_window_filters(state: TurnState) -> list[dict]:
        time_window = state.time_window or {}
        return [
            {"col": "Date", "op": ">=", "value": time_window.get("start_date")},
            {"col": "Date", "op": "<=", "value": time_window.get("end_date")},
        ]

    def _plan_default(self, state: TurnState, table: str, *, kind: str) -> dict:
        """Daily series + totals — works for sales_summary, trend, forecast,
        and the *current-window* portion of comparison / RCA."""
        intent = state.intent or {}
        # The forecast projector expects daily ISO buckets to extrapolate;
        # phrases like "forecast next week" set granularity="weekly" via
        # TimeKPI, which breaks the projector. Force daily for forecast.
        granularity = state.granularity or "daily"
        if kind == "forecast":
            granularity = "daily"
        bucket_expr = _BUCKET_BY_GRAN.get(granularity, '"Date"')

        metric = intent.get("metric") or "total_amount"
        select: list[dict[str, str]] = [
            {"expr": bucket_expr, "alias": "bucket"},
        ]
        if metric in ("total_amount", "revenue"):
            select.append({"expr": 'SUM("Total Amount")', "alias": "sales"})
        else:
            # Always project sales so the chart has something on the Y axis.
            select.append({"expr": 'SUM("Total Amount")', "alias": "sales"})
        select.append({"expr": "COUNT(*)", "alias": "orders"})
        if metric == "customers":
            select.append({
                "expr": 'COUNT(DISTINCT "Party Name")',
                "alias": "customers",
            })
        if metric == "aov":
            select.append({
                "expr": 'CAST(SUM("Total Amount") AS REAL) / NULLIF(COUNT(*), 0)',
                "alias": "aov",
            })
        if metric == "refunds":
            select.append({"expr": 'SUM("Loyalty Redeemed")', "alias": "refunds"})

        where = (
            self._date_window_filters(state)
            + self._entity_filters(state)
        )

        return {
            "kind":     kind,
            "table":    table,
            "select":   select,
            "where":    where,
            "group_by": ["bucket"],
            "order_by": [{"col": "bucket", "dir": "ASC"}],
            "limit":    1000,
        }

    def _plan_ranking(self, state: TurnState, table: str) -> dict:
        """Top-N ranking by revenue.

        The grouping column is chosen from the intent's `ranking_subject`
        hint:
          - "product"  → GROUP BY "Product Name" (default for shoe shop —
                         "top performing items / brands / sneakers" all
                         resolve here)
          - "customer" → GROUP BY "Party Name"  (the legacy behaviour, kept
                         for "top customers / buyers / parties" queries)

        We alias the grouping column AS `bucket` so the chart payload and
        ResultAggregator see the same shape regardless of subject.
        """
        intent = state.intent or {}
        hints = intent.get("hints") or {}
        subject = (hints.get("ranking_subject") or "product").lower()
        # rank_direction: "desc" → top performers (default);
        #                  "asc"  → bottom performers ("lowest selling", "worst").
        direction = (hints.get("rank_direction") or "desc").lower()
        sort_dir = "ASC" if direction == "asc" else "DESC"
        top_n = int(intent.get("top_n") or hints.get("top_n") or 10)
        top_n = max(3, min(top_n, 50))

        if subject == "customer":
            group_col = "Party Name"
        else:
            group_col = "Product Name"

        select = [
            {"expr": f'"{group_col}"',          "alias": "bucket"},
            {"expr": 'SUM("Total Amount")',     "alias": "sales"},
            {"expr": "COUNT(*)",                "alias": "orders"},
        ]
        # SQLite: `col != ''` already filters out NULLs because the
        # comparison evaluates to NULL (not TRUE) — no separate IS NOT
        # NULL clause needed.
        where = (
            self._date_window_filters(state)
            + self._entity_filters(state)
            + [{"col": group_col, "op": "!=", "value": ""}]
        )
        return {
            "kind":            "ranking",
            "ranking_subject": subject,
            "rank_direction":  direction,
            "table":           table,
            "select":          select,
            "where":           where,
            "group_by":        [group_col],
            "order_by":        [{"col": "sales", "dir": sort_dir}],
            "limit":           top_n,
        }


# ---------------------------------------------------------------------------
# 6.8 SqlWriter
# ---------------------------------------------------------------------------

_ALLOWED_OPS = {"=", "!=", "<", "<=", ">", ">=", "LIKE", "IN"}


class SqlWriterArgs(BaseModel):
    pass


class SqlWriterTool(Tool):
    name = "SqlWriter"
    description = "Renders sql_plan into a SQL string + parameter list."
    args_model = SqlWriterArgs
    independent = False

    async def run(self, state: TurnState, args: SqlWriterArgs) -> ToolResult:
        miss = require(state, "sql_plan")
        if miss:
            return miss
        plan = state.sql_plan or {}
        table = plan.get("table")
        if table not in ALLOWED_TABLES:
            return ToolResult(ok=False, error=f"plan.table invalid: {table!r}")

        select_parts = []
        for s in plan.get("select", []):
            expr = (s.get("expr") or "").strip()
            alias = (s.get("alias") or "").strip()
            if not expr:
                return ToolResult(ok=False, error="empty select expr in plan")
            if alias:
                select_parts.append(f"{expr} AS {quoted(alias)}")
            else:
                select_parts.append(expr)
        if not select_parts:
            return ToolResult(ok=False, error="plan.select is empty")

        where_parts = []
        params: list = []
        for w in plan.get("where", []):
            col = w.get("col"); op = (w.get("op") or "").upper(); val = w.get("value")
            if not col or op not in _ALLOWED_OPS:
                return ToolResult(ok=False, error=f"bad where clause: {w!r}")
            if val is None:
                return ToolResult(ok=False, error=f"where value missing for {col!r}")
            if op == "IN":
                if not isinstance(val, (list, tuple)) or not val:
                    return ToolResult(ok=False, error=f"IN requires non-empty list for {col!r}")
                ph = ",".join(["?"] * len(val))
                where_parts.append(f"{quoted(col)} IN ({ph})")
                params.extend(val)
            else:
                where_parts.append(f"{quoted(col)} {op} ?")
                params.append(val)

        group_by = plan.get("group_by") or []
        group_clause = ""
        if group_by:
            group_clause = "GROUP BY " + ", ".join(quoted(c) for c in group_by)

        order_by = plan.get("order_by") or []
        order_parts = []
        for o in order_by:
            col = o.get("col"); direction = (o.get("dir") or "ASC").upper()
            if not col or direction not in ("ASC", "DESC"):
                return ToolResult(ok=False, error=f"bad order_by: {o!r}")
            order_parts.append(f"{quoted(col)} {direction}")
        order_clause = "ORDER BY " + ", ".join(order_parts) if order_parts else ""

        limit = int(plan.get("limit") or 1000)
        if limit <= 0 or limit > 100000:
            return ToolResult(ok=False, error=f"limit out of range: {limit}")
        limit_clause = f"LIMIT {limit}"

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        sql = " ".join(filter(None, [
            "SELECT", ", ".join(select_parts),
            "FROM", quoted(table),
            where_clause,
            group_clause,
            order_clause,
            limit_clause,
        ]))

        new_plan = dict(plan)
        new_plan["_params"] = params

        return ToolResult(
            ok=True,
            output={"sql": sql, "params_count": len(params)},
            state_updates={"sql_draft": sql, "sql_plan": new_plan},
        )


# ---------------------------------------------------------------------------
# 6.9 SqlValidator
# ---------------------------------------------------------------------------

_SELECT_RE = re.compile(r"^\s*select\b", re.IGNORECASE | re.DOTALL)
_DANGEROUS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|merge|"
    r"replace|truncate|attach|detach|vacuum|pragma|exec)\b",
    re.IGNORECASE,
)
_QUOTED_IDENT_RE = re.compile(r'"([^"]+)"')

_KNOWN_ALIASES = {"bucket", "sales", "orders", "customers", "aov", "refunds"}
_KNOWN_TABLES = set(ALLOWED_TABLES)
_KNOWN_COLUMNS = set(SCHEMA_COLUMNS)


class SqlValidatorArgs(BaseModel):
    pass


class SqlValidatorTool(Tool):
    name = "SqlValidator"
    description = "Validates the drafted SQL — SELECT-only, single statement, columns exist, scan-size budget."
    args_model = SqlValidatorArgs
    independent = False

    async def run(self, state: TurnState, args: SqlValidatorArgs) -> ToolResult:
        miss = require(state, "sql_draft", "db_schema")
        if miss:
            return miss
        sql = (state.sql_draft or "").strip().rstrip(";").strip()
        if not sql:
            return ToolResult(ok=False, error="empty SQL")

        if not _SELECT_RE.search(sql):
            return ToolResult(ok=False, error="SQL must be a SELECT statement")
        if _DANGEROUS.search(sql):
            return ToolResult(ok=False, error="dangerous SQL keyword detected")
        if ";" in sql:
            return ToolResult(ok=False, error="only one SQL statement allowed")

        unknown: list[str] = []
        for ident in _QUOTED_IDENT_RE.findall(sql):
            if (
                ident in _KNOWN_TABLES
                or ident in _KNOWN_COLUMNS
                or ident in _KNOWN_ALIASES
            ):
                continue
            unknown.append(ident)
        if unknown:
            return ToolResult(
                ok=False,
                error=f"unknown identifier(s) in SQL: {sorted(set(unknown))!r}",
            )

        # Crude scan estimate: 10 MB per FROM clause. SQLite has no EXPLAIN
        # equivalent so we approximate.
        from_count = max(len(re.findall(r"\bfrom\b", sql, re.IGNORECASE)), 1)
        estimated_bytes = from_count * 10_000_000
        try:
            check_sql_scan_estimate(estimated_bytes)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

        return ToolResult(
            ok=True,
            output={"valid": True, "estimated_bytes": estimated_bytes},
            state_updates={
                "sql_final": sql,
                "bytes_scanned": state.bytes_scanned + estimated_bytes,
            },
        )


# ---------------------------------------------------------------------------
# 6.10 SqlExecutor
# ---------------------------------------------------------------------------

_executor_log = logging.getLogger("agentic_ai.sql_executor")


class SqlExecutorArgs(BaseModel):
    pass


class SqlExecutorTool(Tool):
    name = "SqlExecutor"
    description = "Executes the validated SQL against financial_records.db and returns rows."
    args_model = SqlExecutorArgs
    independent = False

    async def run(self, state: TurnState, args: SqlExecutorArgs) -> ToolResult:
        miss = require(state, "sql_final")
        if miss:
            _executor_log.warning("SqlExecutor halted — sql_final not set")
            return miss

        plan = state.sql_plan or {}
        params = list(plan.get("_params") or [])
        sql = state.sql_final or ""

        _executor_log.info("EXECUTING SQL: %s  (params=%d)", sql, len(params))

        try:
            rows = await fetch_all(sql, tuple(params))
        except sqlite3.Error as e:
            _executor_log.warning("SqlExecutor SQL error: %s", e)
            return ToolResult(ok=False, error=f"SQL exec failed: {e}")
        except Exception as e:
            _executor_log.exception("SqlExecutor unexpected failure")
            return ToolResult(
                ok=False,
                error=f"DB error: {type(e).__name__}: {e}",
            )

        _executor_log.info("ROWS RETURNED: %d", len(rows))

        # rows MAY be []. That is valid (no records matched), not a failure.
        return ToolResult(
            ok=True,
            output={"row_count": len(rows), "rows_preview": rows[:5]},
            state_updates={"rows": rows},
        )


# ---------------------------------------------------------------------------
# 6.11 ResultAggregator
# ---------------------------------------------------------------------------

_aggregator_log = logging.getLogger("agentic_ai.result_aggregator")


async def _diagnose_empty(state: TurnState) -> dict[str, Any] | None:
    """Probe the planned table to explain WHY no rows came back.

    Returns one of:
      {"reason": "table_empty",         "table": <t>}
      {"reason": "filter_excluded_all", "table": <t>,
       "table_total_rows": <n>,
       "available_min_date": <iso>,
       "available_max_date": <iso>,
       "queried_window": <state.time_window>}
      None  — couldn't run the probe (no plan, unknown table, etc.)
    """
    plan = state.sql_plan or {}
    table = plan.get("table") or "sales"
    if table not in ALLOWED_TABLES:
        return None
    try:
        row = await fetch_one(
            f'SELECT COUNT(*) AS n, '
            f'       MIN("Date") AS min_d, '
            f'       MAX("Date") AS max_d '
            f'FROM {quoted(table)}'
        )
    except Exception:
        _aggregator_log.warning("aggregator: empty-state probe failed", exc_info=True)
        return None
    if row is None:
        return None
    n = int(row.get("n") or 0)
    if n == 0:
        return {"reason": "table_empty", "table": table}
    return {
        "reason": "filter_excluded_all",
        "table": table,
        "table_total_rows": n,
        "available_min_date": row.get("min_d"),
        "available_max_date": row.get("max_d"),
        "queried_window": state.time_window,
    }


class ResultAggregatorArgs(BaseModel):
    pass


# Canonical aggregate "kinds" — these drive the response narrative AND the
# frontend chart shape. The set is small and closed; new intent types should
# map onto an existing kind whenever possible.
AGGREGATE_KINDS: tuple[str, ...] = (
    "summary",      # totals + daily series  (default analytics + sales_summary)
    "trend",        # totals + daily series, narrated as a trend
    "ranking",      # top-N items keyed on Party Name
    "comparison",   # current vs previous window
    "rca",          # comparison + decline narration
    "forecast",     # daily series ready for the forecast projector
    "anomaly",      # daily series, narrated as outlier scan
)


def _kind_for_intent(intent_type: str) -> str:
    return {
        "sales_summary":        "summary",
        "purchase_analysis":    "summary",
        "product_performance":  "ranking",
        "trend_analysis":       "trend",
        "comparison":           "comparison",
        "root_cause_analysis":  "rca",
        "forecasting":          "forecast",
        "anomaly_detection":    "anomaly",
    }.get(intent_type, "summary")


def _shift_window(start_iso: str, end_iso: str) -> tuple[str, str] | None:
    """Return the immediately-prior period of equal length, or None if the
    inputs aren't parseable."""
    try:
        s = date.fromisoformat(start_iso)
        e = date.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    if e < s:
        return None
    span = e - s
    prev_e = s - timedelta(days=1)
    prev_s = prev_e - span
    return prev_s.isoformat(), prev_e.isoformat()


async def _period_totals(table: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    row = await fetch_one(
        f'SELECT SUM("Total Amount") AS sales, COUNT(*) AS orders, '
        f'       COUNT(DISTINCT "Party Name") AS customers '
        f'FROM {quoted(table)} '
        f'WHERE "Date" >= ? AND "Date" <= ?',
        (start_iso, end_iso),
    )
    return {
        "period":    f"{start_iso} to {end_iso}",
        "start":     start_iso,
        "end":       end_iso,
        "sales":     round(float((row or {}).get("sales") or 0), 2),
        "orders":    int((row or {}).get("orders") or 0),
        "customers": int((row or {}).get("customers") or 0),
    }


async def _compute_period_comparison(state: TurnState, table: str) -> dict[str, Any] | None:
    """Run a previous-period totals probe alongside the current window.
    Returns None when the time window isn't usable."""
    tw = state.time_window or {}
    start = tw.get("start_date")
    end = tw.get("end_date")
    if not (isinstance(start, str) and isinstance(end, str)):
        return None
    if table not in ALLOWED_TABLES:
        return None

    prev = _shift_window(start, end)
    if prev is None:
        return None
    prev_s, prev_e = prev

    current  = await _period_totals(table, start,  end)
    previous = await _period_totals(table, prev_s, prev_e)

    cur_sales = current["sales"]
    prev_sales = previous["sales"]
    delta_abs = round(cur_sales - prev_sales, 2)
    delta_pct: float | None
    if prev_sales > 0:
        delta_pct = round((cur_sales - prev_sales) / prev_sales * 100, 2)
    elif cur_sales > 0:
        delta_pct = None  # infinite — treat as "new activity"
    else:
        delta_pct = 0.0

    return {
        "current":   current,
        "previous":  previous,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
    }


class ResultAggregatorTool(Tool):
    name = "ResultAggregator"
    description = (
        "Normalizes SQL rows into chart-ready aggregates. Emits a `kind` "
        "field driven by state.intent.type, runs a previous-period probe "
        "for comparison / RCA intents, and pivots ranking rows into an "
        "items list."
    )
    args_model = ResultAggregatorArgs
    independent = False

    async def run(self, state: TurnState, args: ResultAggregatorArgs) -> ToolResult:
        miss = require(state, "rows")
        if miss:
            _aggregator_log.error("ResultAggregator halted — state.rows is None")
            return ToolResult(
                ok=False,
                error=(
                    "SqlExecutor did not produce rows — state.rows is None. "
                    "The SQL execution step did not complete successfully."
                ),
            )

        rows = state.rows  # type: ignore[assignment]
        if not isinstance(rows, list):
            return ToolResult(
                ok=False,
                error=f"state.rows is not a list (got {type(rows).__name__})",
            )

        granularity = state.granularity or "daily"
        intent_type = (state.intent or {}).get("type") or "sales_summary"
        kind = _kind_for_intent(intent_type)
        plan = state.sql_plan or {}
        table = plan.get("table") or "sales"

        _aggregator_log.info(
            "AGGREGATOR INPUT ROW COUNT: %d (granularity=%s, kind=%s, intent=%s)",
            len(rows), granularity, kind, intent_type,
        )

        empty_reason = None
        if len(rows) == 0:
            empty_reason = await _diagnose_empty(state)
            if empty_reason:
                _aggregator_log.info("AGGREGATOR EMPTY DIAGNOSTIC: %s", empty_reason)
            else:
                _aggregator_log.info("AGGREGATOR EMPTY DIAGNOSTIC: probe unavailable")

        series: list[dict] = []
        total_sales = 0.0
        total_orders = 0
        max_customers = 0
        for r in rows:
            sales = float(r.get("sales") or 0)
            orders_v = int(r.get("orders") or 0)
            bucket = r.get("bucket")
            if bucket is not None:
                series.append({
                    "bucket": str(bucket),
                    "sales":  round(sales, 2),
                    "orders": orders_v,
                })
            total_sales += sales
            total_orders += orders_v
            cust = r.get("customers")
            if cust is not None:
                try:
                    max_customers = max(max_customers, int(cust))
                except (TypeError, ValueError):
                    pass

        aggregates: dict[str, Any] = {
            "kind":        kind,
            "intent_type": intent_type,
            "granularity": granularity,
            "totals": {
                "total_sales": round(total_sales, 2),
                "orders":      int(total_orders),
                "customers":   int(max_customers),
            },
            "series": series,
        }

        # Ranking-specific: expose the rows as a typed `items` list so the
        # response formatter and chart can consume them directly.
        if kind == "ranking":
            items = []
            for r in rows:
                name = r.get("bucket")
                if not name:
                    continue
                items.append({
                    "name":   str(name),
                    "sales":  round(float(r.get("sales") or 0), 2),
                    "orders": int(r.get("orders") or 0),
                })
            aggregates["items"] = items
            # Carry the subject through so the formatter / chart can
            # pluralise correctly ("more products" vs "more customers").
            aggregates["ranking_subject"] = plan.get("ranking_subject", "product")

        # Comparison + RCA: probe the previous period and attach it.
        if kind in ("comparison", "rca") and not empty_reason:
            comparison = await _compute_period_comparison(state, table)
            if comparison is not None:
                aggregates["comparison"] = comparison

        if empty_reason is not None:
            aggregates["empty_reason"] = empty_reason

        _aggregator_log.info(
            "AGGREGATOR TOTALS: %s  series=%d kind=%s",
            aggregates["totals"], len(series), kind,
        )

        return ToolResult(
            ok=True,
            output=aggregates,
            state_updates={"aggregates": aggregates, "chart_data": aggregates},
        )


# ---------------------------------------------------------------------------
# 6.12 InsightEngine — rule-based or LLM-narrated paragraph.
# ---------------------------------------------------------------------------

_insight_log = logging.getLogger("agentic_ai.insight_engine")

_INSIGHT_LLM_SYSTEM = """You are a senior business analyst. Read the provided
aggregates JSON and write a single paragraph (3-5 sentences) of plain-English
insight grounded ONLY in the numbers given. Do NOT invent values.
Output STRICT JSON: {"narrative": "<paragraph>"}.
"""


class InsightEngineArgs(BaseModel):
    mode: str = Field(default="rule")  # "rule" | "llm"


def _trend(series: list[dict]) -> tuple[str, float]:
    if not series or len(series) < 2:
        return "flat", 0.0
    first = series[0].get("sales") or 0.0
    last = series[-1].get("sales") or 0.0
    if first <= 0:
        return ("up" if last > 0 else "flat"), 0.0
    delta_pct = (last - first) / first * 100
    direction = "up" if delta_pct >= 0 else "down"
    return direction, delta_pct


def _rule_summary(aggregates: dict) -> dict:
    totals = aggregates.get("totals") or {}
    series = aggregates.get("series") or []
    sales = float(totals.get("total_sales") or 0)
    orders = int(totals.get("orders") or 0)
    customers = int(totals.get("customers") or 0)
    direction, pct = _trend(series)
    parts = [f"Total sales ₹{sales:,.2f} across {orders:,} orders."]
    if customers:
        parts.append(f"From {customers:,} unique customers.")
    if series and len(series) >= 2:
        parts.append(
            f"Trend across {len(series)} {aggregates.get('granularity','daily')} "
            f"buckets: {direction} {abs(pct):.1f}%."
        )
    narrative = " ".join(parts)
    return {"summary": narrative, "trend": direction, "delta_pct": round(pct, 2),
            "narrative": narrative}


class InsightEngineTool(Tool):
    name = "InsightEngine"
    description = (
        "Builds a textual insight from state.aggregates. Rule-based by default; "
        "LLM-enhanced narrative when mode='llm'."
    )
    args_model = InsightEngineArgs
    independent = False

    async def run(self, state: TurnState, args: InsightEngineArgs) -> ToolResult:
        miss = require(state, "aggregates")
        if miss:
            return miss
        aggregates = state.aggregates or {}
        rule = _rule_summary(aggregates)

        if args.mode != "llm":
            return ToolResult(ok=True, output=rule, state_updates={"insights": rule})

        try:
            groq = get_groq()
            user_payload = {
                "question": state.question,
                "aggregates": aggregates,
                "rule_based_summary": rule["summary"],
            }
            resp = await groq.complete(
                [
                    GroqMessage(role="system", content=_INSIGHT_LLM_SYSTEM),
                    GroqMessage(role="user", content=json.dumps(user_payload, default=str)),
                ],
                temperature=0.2,
                max_tokens=400,
                force_json=True,
            )
            if resp.error or not resp.content:
                return ToolResult(
                    ok=True,
                    output={**rule, "llm_error": resp.error},
                    state_updates={"insights": rule},
                    delta_metrics={"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out},
                )
            try:
                parsed = json.loads(resp.content)
                narrative = str(parsed.get("narrative") or rule["narrative"])
            except Exception:
                narrative = rule["narrative"]
            insights = {**rule, "narrative": narrative}
            return ToolResult(
                ok=True,
                output=insights,
                state_updates={"insights": insights},
                delta_metrics={"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out},
            )
        except Exception as e:
            _insight_log.warning("InsightEngine LLM call failed: %s", e, exc_info=True)
            return ToolResult(ok=True, output=rule, state_updates={"insights": rule})


# ---------------------------------------------------------------------------
# 6.13 ResponseFormatter
# ---------------------------------------------------------------------------

def _empty_message(empty_reason: dict) -> str:
    reason = empty_reason.get("reason")
    if reason == "table_empty":
        return "No sales data available. Please upload data."
    if reason == "filter_excluded_all":
        lo = empty_reason.get("available_min_date") or "?"
        hi = empty_reason.get("available_max_date") or "?"
        return f"No sales found in this time range. Available data is from {lo} to {hi}."
    return "No matching data found for your query."


def _trend_sentence(series: list[dict]) -> str | None:
    if not series or len(series) < 2:
        return None
    first = float(series[0].get("sales") or 0)
    last = float(series[-1].get("sales") or 0)
    if first <= 0:
        return None
    delta_pct = (last - first) / first * 100
    if abs(delta_pct) < 5:
        return "Overall performance remained stable across the period."
    if delta_pct > 0:
        return f"Sales show an upward trend of {abs(delta_pct):.1f}% across the period."
    return f"Sales show a downward trend of {abs(delta_pct):.1f}% across the period."


def _format_summary(aggregates: dict) -> str:
    """sales_summary / purchase_analysis: totals + a one-line trend."""
    totals = aggregates.get("totals") or {}
    sales = float(totals.get("total_sales") or 0)
    orders = int(totals.get("orders") or 0)
    if not sales and not orders:
        return "No sales recorded yet."
    parts = [
        f"Total sales are ₹{sales:,.2f} across {orders:,} orders."
        if orders
        else f"Total sales are ₹{sales:,.2f}."
    ]
    trend = _trend_sentence(aggregates.get("series") or [])
    if trend:
        parts.append(trend)
    return " ".join(parts)


def _format_ranking(aggregates: dict) -> str:
    """product_performance: name the top performer + show next runners.

    Wording depends on `aggregates.ranking_subject`:
      - "product"  → "products in the ranking"
      - "customer" → "customers in the ranking"
    """
    items = aggregates.get("items") or []
    if not items:
        return "No transactions to rank in the selected period."

    subject = (aggregates.get("ranking_subject") or "product").lower()
    noun_singular = "customer" if subject == "customer" else "product"
    noun_plural = noun_singular + "s"

    total = sum(float(it.get("sales") or 0) for it in items) or 0.0
    leader = items[0]
    leader_name = leader.get("name") or "Unknown"
    leader_sales = float(leader.get("sales") or 0)
    leader_orders = int(leader.get("orders") or 0)
    share = (leader_sales / total * 100) if total > 0 else 0.0
    parts = [
        f"{leader_name} leads with ₹{leader_sales:,.2f} across "
        f"{leader_orders:,} orders ({share:.1f}% of the top {len(items)} {noun_plural})."
    ]
    runners = items[1:4]
    if runners:
        runners_str = ", ".join(
            f"{it.get('name')} (₹{float(it.get('sales') or 0):,.0f})" for it in runners
        )
        parts.append(f"Next: {runners_str}.")
    remaining = len(items) - 4
    if remaining > 0:
        noun_for_remaining = noun_singular if remaining == 1 else noun_plural
        parts.append(f"{remaining} more {noun_for_remaining} in the ranking.")
    return " ".join(parts)


def _format_comparison(aggregates: dict, *, rca: bool = False) -> str:
    """comparison / RCA: explicit current-vs-previous narrative."""
    cmp = aggregates.get("comparison") or {}
    cur = cmp.get("current") or {}
    prev = cmp.get("previous") or {}
    delta_pct = cmp.get("delta_pct")
    delta_abs = float(cmp.get("delta_abs") or 0.0)
    cur_sales = float(cur.get("sales") or 0)
    prev_sales = float(prev.get("sales") or 0)
    cur_orders = int(cur.get("orders") or 0)
    prev_orders = int(prev.get("orders") or 0)

    if not cmp:
        # Fall through to a summary if the previous-period probe didn't run.
        return _format_summary(aggregates)

    direction = "up" if delta_abs > 0 else "down" if delta_abs < 0 else "flat"
    cur_window = cur.get("period") or "current period"
    prev_window = prev.get("period") or "prior period"

    head = (
        f"This period ({cur_window}) recorded ₹{cur_sales:,.2f} across "
        f"{cur_orders:,} orders, vs ₹{prev_sales:,.2f} across {prev_orders:,} "
        f"orders in the prior period ({prev_window})."
    )
    parts = [head]

    if delta_pct is None:
        if cur_sales > 0 and prev_sales == 0:
            parts.append(f"Net new activity of ₹{cur_sales:,.2f} — no prior baseline to compare.")
    else:
        magnitude_word = (
            "sharp"  if abs(delta_pct) >= 25 else
            "moderate" if abs(delta_pct) >= 10 else
            "mild"   if abs(delta_pct) >= 3 else
            "flat"
        )
        if magnitude_word == "flat":
            parts.append("Period-over-period change is essentially flat.")
        else:
            parts.append(
                f"That's a {magnitude_word} {direction}-swing of "
                f"{abs(delta_pct):.1f}% (Δ ₹{abs(delta_abs):,.2f})."
            )

    if rca:
        if direction == "down" and prev_orders > 0:
            order_delta_pct = (cur_orders - prev_orders) / prev_orders * 100
            if order_delta_pct < -5:
                parts.append(
                    f"Order volume dropped {abs(order_delta_pct):.1f}% — fewer "
                    f"transactions are reaching the till."
                )
            elif order_delta_pct > 5:
                parts.append(
                    "Order volume actually rose; the decline is driven by lower "
                    "average order value."
                )
            else:
                parts.append(
                    "Order count stayed flat; the change came from average "
                    "order value rather than transaction count."
                )
        elif direction == "up":
            parts.append(
                "Despite the framing of the question, sales did not decline — "
                "the period actually grew. No root-cause investigation needed."
            )
        else:  # flat
            parts.append(
                "No meaningful decline detected — sales were essentially flat "
                "compared to the prior period."
            )

    return " ".join(parts)


def _format_forecast(aggregates: dict) -> str:
    """forecasting: highlight horizon + projected delta."""
    series = aggregates.get("series") or []
    horizon = int(aggregates.get("forecast_horizon_days") or 0)
    historical = [s for s in series if not s.get("predicted")]
    predicted  = [s for s in series if s.get("predicted")]
    if not historical and not predicted:
        return "Not enough history to project a forecast."

    hist_total = sum(float(s.get("sales") or 0) for s in historical)
    pred_total = sum(float(s.get("sales") or 0) for s in predicted)

    parts: list[str] = []
    if historical:
        parts.append(
            f"Historical window: ₹{hist_total:,.2f} over {len(historical)} buckets."
        )
    if predicted:
        days = horizon or len(predicted)
        avg_pred = pred_total / max(len(predicted), 1)
        parts.append(
            f"Projected next {days} day{'s' if days != 1 else ''}: "
            f"₹{pred_total:,.2f} (≈ ₹{avg_pred:,.0f}/day) "
            f"based on linear extrapolation of recent activity."
        )
    elif historical:
        parts.append("Forecast not produced — fall back to the historical view.")
    return " ".join(parts)


def _format_trend(aggregates: dict) -> str:
    """trend_analysis: emphasise direction + magnitude over the window."""
    totals = aggregates.get("totals") or {}
    sales = float(totals.get("total_sales") or 0)
    orders = int(totals.get("orders") or 0)
    series = aggregates.get("series") or []
    if not series:
        return _format_summary(aggregates)

    direction, pct = ("flat", 0.0)
    if len(series) >= 2:
        first = float(series[0].get("sales") or 0)
        last = float(series[-1].get("sales") or 0)
        if first > 0:
            pct = (last - first) / first * 100
            direction = "rising" if pct > 0 else "falling" if pct < 0 else "flat"

    head = (
        f"Across {len(series)} {aggregates.get('granularity', 'daily')} buckets, "
        f"sales totalled ₹{sales:,.2f} on {orders:,} orders."
    )
    if direction == "flat" or abs(pct) < 3:
        tail = "The trend line is essentially flat — no meaningful momentum either way."
    else:
        tail = (
            f"The trend is {direction} at {abs(pct):.1f}% from start to end of "
            f"the window."
        )
    return f"{head} {tail}"


def _format_anomaly(aggregates: dict) -> str:
    """anomaly_detection: surface the highest and lowest buckets."""
    series = aggregates.get("series") or []
    if not series:
        return _format_summary(aggregates)
    sorted_by_sales = sorted(series, key=lambda s: float(s.get("sales") or 0))
    low = sorted_by_sales[0]
    high = sorted_by_sales[-1]
    avg = sum(float(s.get("sales") or 0) for s in series) / len(series)
    return (
        f"Across {len(series)} buckets, the average is ₹{avg:,.0f}. "
        f"Peak: {high.get('bucket')} (₹{float(high.get('sales') or 0):,.2f}). "
        f"Trough: {low.get('bucket')} (₹{float(low.get('sales') or 0):,.2f})."
    )


def _build_answer(aggregates: dict) -> str:
    """Kind-driven funnel — different intents produce different narratives."""
    empty_reason = aggregates.get("empty_reason")
    if empty_reason:
        return _empty_message(empty_reason)

    kind = aggregates.get("kind") or "summary"
    if kind == "ranking":
        return _format_ranking(aggregates)
    if kind == "comparison":
        return _format_comparison(aggregates, rca=False)
    if kind == "rca":
        return _format_comparison(aggregates, rca=True)
    if kind == "forecast":
        return _format_forecast(aggregates)
    if kind == "trend":
        return _format_trend(aggregates)
    if kind == "anomaly":
        return _format_anomaly(aggregates)
    return _format_summary(aggregates)


class ResponseFormatterArgs(BaseModel):
    pass


class ResponseFormatterTool(Tool):
    name = "ResponseFormatter"
    description = (
        "Builds the clean 2–3 sentence answer + the persisted record. "
        "Workflow diagram and section labels are intentionally suppressed; "
        "the chart payload (state.chart_data) is the primary visual output."
    )
    args_model = ResponseFormatterArgs
    independent = False

    async def run(self, state: TurnState, args: ResponseFormatterArgs) -> ToolResult:
        miss = require(state, "aggregates", "insights")
        if miss:
            return miss

        aggregates = state.aggregates or {}
        insights = state.insights or {}
        body = _build_answer(aggregates)

        record = {
            "turn_id":         state.turn_id,
            "cache_key":       state.cache_key,
            "stored_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query":           state.question,
            "sub_agent":       state.sub_agent,
            "route":           state.route,
            "sql":             state.sql_final,
            "rows":            state.rows,
            "aggregates":      aggregates,
            "insights":        insights,
            "chart":           aggregates,
            "final_answer":    body,
            "response_format": "clean",
        }
        return ToolResult(
            ok=True,
            output={"answer_preview": body[:300]},
            state_updates={
                "final_answer":   body,
                "response_record": record,
                "chart_data":     aggregates,
            },
        )


# ---------------------------------------------------------------------------
# 6.14 ResponseStored
# ---------------------------------------------------------------------------

class ResponseStoredArgs(BaseModel):
    pass


class ResponseStoredTool(Tool):
    name = "ResponseStored"
    description = "Persists the formatted response record into data/response_store.json."
    args_model = ResponseStoredArgs
    independent = False

    async def run(self, state: TurnState, args: ResponseStoredArgs) -> ToolResult:
        miss = require(state, "response_record", "cache_key")
        if miss:
            return miss
        try:
            put_cached(state.cache_key or "", state.response_record or {})
        except Exception as e:
            return ToolResult(ok=False, error=f"persist failed: {type(e).__name__}: {e}")
        return ToolResult(
            ok=True,
            output={"stored": True, "cache_key": state.cache_key},
        )


# ===========================================================================
# 6.5 CAPABILITY TOOLS — coarse, LLM-orchestrated wrappers over the 14 tools.
# ===========================================================================
#
# The agentic loop never picks the 14 fine-grained tools directly — their
# ordering dependencies (SqlWriter needs sql_plan, SqlValidator needs
# sql_draft, …) would force the LLM to relearn the pipeline. Instead it picks
# from these ~4 coarse capabilities, each of which composes a deterministic
# sub-sequence of the underlying tools. The rigid SQL micro-chain
# (SchemaRetriever → SqlPlanner → SqlWriter → SqlValidator → SqlExecutor →
# ResultAggregator) is collapsed into one `run_data_query` capability.

# Query shapes `run_data_query` accepts — these map 1:1 onto the intent types
# SqlPlanner / ResultAggregator already branch on.
QUERY_TYPES: tuple[str, ...] = (
    "sales_summary",
    "purchase_analysis",
    "product_performance",
    "trend_analysis",
    "comparison",
    "root_cause_analysis",
    "anomaly_detection",
    "forecasting",
)


async def _run_internal(
    state: TurnState, steps: list[tuple[str, dict]]
) -> tuple[TurnState, ToolResult | None]:
    """Run a fixed sub-sequence of registry tools, threading TurnState.

    Returns (updated_state, None) on success or (state_at_failure, failing
    ToolResult) on the first tool that returns ok=False. Token deltas from
    each step are folded into the threaded state."""
    registry = get_registry()
    work = state
    for name, targs in steps:
        res = await registry.execute(name, targs, work)
        if not res.ok:
            return work, res
        if res.state_updates:
            work = work.apply(**res.state_updates)
        if res.delta_metrics:
            work = work.apply(
                tokens_in=work.tokens_in + int(res.delta_metrics.get("tokens_in", 0)),
                tokens_out=work.tokens_out + int(res.delta_metrics.get("tokens_out", 0)),
            )
    return work, None


def _capability_result(
    base: TurnState, work: TurnState, fields: tuple[str, ...], output: Any
) -> ToolResult:
    """Build a ToolResult carrying only the TurnState fields a capability
    produced, plus the token delta accumulated while it ran."""
    return ToolResult(
        ok=True,
        output=output,
        state_updates={f: getattr(work, f) for f in fields},
        delta_metrics={
            "tokens_in": work.tokens_in - base.tokens_in,
            "tokens_out": work.tokens_out - base.tokens_out,
        },
    )


class ResolveTimeWindowArgs(BaseModel):
    pass


class ResolveTimeWindowTool(Tool):
    name = "resolve_time_window"
    description = (
        "Resolve the date range the question is asking about. Anchors "
        "relative phrases ('last month', 'this quarter', 'yesterday') to the "
        "latest date present in the dataset — not today's calendar date. "
        "Call this before run_data_query whenever the question mentions any "
        "time period. Sets the time window, granularity, and KPI list."
    )
    args_model = ResolveTimeWindowArgs

    async def run(self, state: TurnState, args: ResolveTimeWindowArgs) -> ToolResult:
        from app.dynamic_ingest import list_dynamic_tables as _ldt_tw
        if _ldt_tw():
            # Dynamic tables present — time window is encoded in the date columns
            # of the user-uploaded tables.  Tell the LLM to handle dates directly
            # in its SQL (e.g. WHERE transaction_date >= '2026-01-01').
            return ToolResult(ok=True, output={
                "note": (
                    "Dynamic tables are active. Do not use this tool. "
                    "Handle time filters directly in your query_user_table SQL using "
                    "WHERE on the relevant date column (e.g. transaction_date, date)."
                )
            })
        work, fail = await _run_internal(state, [("TimeKPI", {})])
        if fail is not None:
            return ToolResult(ok=False, error=f"TimeKPI: {fail.error}")
        return _capability_result(
            state, work, ("time_window", "granularity", "kpis"),
            {"time_window": work.time_window, "granularity": work.granularity},
        )


class ResolveEntitiesArgs(BaseModel):
    pass


class ResolveEntitiesTool(Tool):
    name = "resolve_entities"
    description = (
        "Map fuzzy names in the question (merchants, customers, products, "
        "categories, brands) onto their canonical database values. Call this "
        "when the question names a specific entity ('how did Nike do', "
        "'sales for ACME Corp'). Skip it for whole-dataset questions. Sets "
        "the resolved entity list used as SQL filters."
    )
    args_model = ResolveEntitiesArgs

    async def run(self, state: TurnState, args: ResolveEntitiesArgs) -> ToolResult:
        work, fail = await _run_internal(state, [("EntityResolver", {})])
        if fail is not None:
            return ToolResult(ok=False, error=f"EntityResolver: {fail.error}")
        return _capability_result(
            state, work, ("entities",), {"entities": work.entities},
        )


class RunDataQueryArgs(BaseModel):
    query_type: str = Field(
        ...,
        description=(
            "The shape of analysis to run. One of: sales_summary, "
            "purchase_analysis, product_performance (top-N ranking), "
            "trend_analysis, comparison (period-over-period), "
            "root_cause_analysis, anomaly_detection, forecasting."
        ),
    )


class RunDataQueryTool(Tool):
    name = "run_data_query"
    description = (
        "Plan, write, validate, and execute a SQL query against the financial "
        "dataset, then aggregate the rows into chart-ready totals + series. "
        "This is the core data capability — call it once you know the time "
        "window (and entities, if any). Pick `query_type` to match what the "
        "user wants: a plain total is sales_summary; 'top 5 products' is "
        "product_performance; 'vs last month' is comparison; 'why did X drop' "
        "is root_cause_analysis; 'predict next week' is forecasting. "
        "comparison and root_cause_analysis automatically probe the previous "
        "period; forecasting automatically projects the series forward."
    )
    args_model = RunDataQueryArgs

    async def run(self, state: TurnState, args: RunDataQueryArgs) -> ToolResult:
        query_type = (args.query_type or "").strip()
        if query_type not in QUERY_TYPES:
            return ToolResult(
                ok=False,
                error=(
                    f"unknown query_type {query_type!r}; "
                    f"valid: {', '.join(QUERY_TYPES)}"
                ),
            )

        work = state
        # Defensive: the loop is told to resolve the time window first, but if
        # it skipped that step, resolve it now so SqlPlanner has a window.
        if work.time_window is None:
            work, fail = await _run_internal(work, [("TimeKPI", {})])
            if fail is not None:
                return ToolResult(ok=False, error=f"TimeKPI: {fail.error}")

        # Override the (non-binding) deterministic intent hint with the
        # query_type the LLM chose. SqlPlanner + ResultAggregator branch on
        # intent.type, so this is how the LLM steers the query shape.
        merged_intent = {**(work.intent or {}), "type": query_type}
        work = work.apply(intent=merged_intent)

        work, fail = await _run_internal(work, [
            ("SchemaRetriever", {}),
            ("SqlPlanner", {}),
            ("SqlWriter", {}),
            ("SqlValidator", {}),
            ("SqlExecutor", {}),
            ("ResultAggregator", {}),
        ])
        if fail is not None:
            return ToolResult(ok=False, error=fail.error)

        # Forecasting: project the daily series forward in-memory (same
        # least-squares projection the old ForecastAgent used).
        if query_type == "forecasting" and work.aggregates:
            aggregates = dict(work.aggregates)
            series = list(aggregates.get("series") or [])
            aggregates["series"] = _project_forecast(series)
            aggregates["forecast_horizon_days"] = _FORECAST_HORIZON_DAYS
            work = work.apply(aggregates=aggregates, chart_data=aggregates)

        aggregates = work.aggregates or {}
        # Compact summary for the LLM — `series` is omitted (only its length
        # matters for narration) and `items` is capped so the tool reply
        # stays small enough to round-trip cleanly.
        items = aggregates.get("items")
        if isinstance(items, list) and len(items) > 25:
            items = items[:25]
        summary = {
            "query_type":  query_type,
            "kind":        aggregates.get("kind"),
            "row_count":   len(work.rows or []),
            "totals":      aggregates.get("totals"),
            "series_len":  len(aggregates.get("series") or []),
            "items":       items,
            "comparison":  aggregates.get("comparison"),
            "empty_reason": aggregates.get("empty_reason"),
        }
        return _capability_result(
            state, work,
            ("intent", "time_window", "granularity", "kpis", "db_schema",
             "sql_plan", "sql_draft", "sql_final", "rows", "aggregates",
             "chart_data"),
            summary,
        )


class GenerateNarrativeArgs(BaseModel):
    pass


class GenerateNarrativeTool(Tool):
    name = "generate_narrative"
    description = (
        "Write a short plain-English analyst narrative over the aggregates "
        "produced by the most recent run_data_query. Optional — call it when "
        "the user wants insight/explanation rather than a bare number. "
        "Requires run_data_query to have run first."
    )
    args_model = GenerateNarrativeArgs

    async def run(self, state: TurnState, args: GenerateNarrativeArgs) -> ToolResult:
        miss = require(state, "aggregates")
        if miss:
            return ToolResult(
                ok=False,
                error="no aggregates yet — call run_data_query before generate_narrative",
            )
        work, fail = await _run_internal(state, [("InsightEngine", {"mode": "llm"})])
        if fail is not None:
            return ToolResult(ok=False, error=f"InsightEngine: {fail.error}")
        return _capability_result(
            state, work, ("insights",),
            {"narrative": (work.insights or {}).get("narrative")},
        )


class GoogleDriveArgs(BaseModel):
    action: str = Field(
        description="'list' to show the user's Drive data files, "
        "'ingest' to load selected files into the database."
    )
    file_ids: list[str] = Field(
        default_factory=list,
        description="Drive file ids to ingest. Required when action='ingest'.",
    )
    target: str = Field(
        default="sales",
        description="Which table to load into: 'sales' or 'purchase'.",
    )
    dedup_mode: str = Field(
        default="skip",
        description="Duplicate-row policy: 'skip' | 'replace' | 'append' | 'block'.",
    )


class GoogleDriveTool(Tool):
    name = "google_drive"
    description = (
        "Access the user's Google Drive. action='list' returns their "
        "available data files (CSV / XLSX / Google Sheets); action='ingest' "
        "downloads the given file_ids and loads them into the sales or "
        "purchase table. After a successful ingest the new rows are live, so "
        "subsequent run_data_query calls will see them. Requires the user to "
        "have connected Google Drive on the Upload page first."
    )
    args_model = GoogleDriveArgs

    async def run(self, state: TurnState, args: GoogleDriveArgs) -> ToolResult:
        from app import google_drive

        if not google_drive.is_configured():
            return ToolResult(
                ok=False,
                error="Google Drive is not configured on this server "
                      "(missing OAuth credentials).",
            )
        creds = await asyncio.to_thread(google_drive.load_credentials)
        if creds is None:
            return ToolResult(
                ok=False,
                error="Google Drive not connected. Ask the user to click "
                      "'Connect Google Drive' on the Upload page.",
            )

        action = (args.action or "").strip().lower()
        if action == "list":
            files = await google_drive.list_drive_files(creds)
            return ToolResult(ok=True, output={"files": files})
        if action == "ingest":
            if not args.file_ids:
                return ToolResult(
                    ok=False,
                    error="action='ingest' requires file_ids — call "
                          "action='list' first to get them.",
                )
            target = (args.target or "sales").strip().lower()
            if target not in ALLOWED_TABLES:
                return ToolResult(
                    ok=False, error=f"target must be one of {ALLOWED_TABLES!r}",
                )
            results = await google_drive.ingest_drive_files(
                creds, args.file_ids, target, args.dedup_mode or "skip",
            )
            return ToolResult(ok=True, output={"results": results})
        return ToolResult(
            ok=False, error=f"unknown action {args.action!r}; use 'list' or 'ingest'.",
        )


# ---------------------------------------------------------------------------
# 6.7 query_user_table — LLM-callable raw SQL on user-uploaded dynamic tables
# ---------------------------------------------------------------------------
# When the question is about data that lives in a user-uploaded sheet
# (inventory, product hierarchy, store transactions, etc. — anything outside
# the legacy sales/purchase schema), the LLM writes a SELECT against the
# relevant `u_<sheet>` table directly. The tool validates + executes + auto-
# builds a chart from the result.
#
# Hard constraints enforced by the tool:
#   • Only SELECT statements
#   • Only identifiers (tables/columns) present in the dynamic registry
#     plus the four metadata columns (_id, _batch_id, _source_sheet,
#     _inserted_at) and the SQL aliases the LLM declares for chart axes.
#   • A single statement (no `;` chains, no CTEs that span statements)
#   • Row limit defaulted + capped to 500 — prevents accidental table scans
#   • Scan estimate counted toward the cost guard

_raw_sql_log = logging.getLogger("agentic_ai.raw_sql_tool")

_RAW_SQL_DANGEROUS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|"
    r"replace|truncate|rename|reindex)\b",
    re.IGNORECASE,
)
_RAW_SQL_SELECT = re.compile(r"^\s*select\b", re.IGNORECASE | re.DOTALL)
_RAW_SQL_QUOTED_IDENT = re.compile(r'"([^"]+)"')
_RAW_SQL_BACKTICK_IDENT = re.compile(r"`([^`]+)`")
_RAW_SQL_BARE_IDENT = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")
_RAW_SQL_MAX_LIMIT = 500


class RawSqlQueryArgs(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "A complete SELECT statement against one of the user-uploaded "
            "u_* tables listed in the schema. Quote identifiers with double "
            'quotes when they contain underscores ("u_inventory_master"). '
            "Single statement only — no semicolons. Add a LIMIT clause for "
            "result-set queries (max 500 rows enforced)."
        ),
    )
    chart_x_column: str | None = Field(
        default=None,
        description=(
            "Column name (or SQL alias) from the SELECT list to use as the "
            "chart X-axis (the category/bucket/label). Required when the "
            "result has more than 1 row. Match the alias in the SQL exactly."
        ),
    )
    chart_y_column: str | None = Field(
        default=None,
        description=(
            "Numeric column / SQL alias from the SELECT list to use as the "
            "chart Y-axis (the value). Match the alias in the SQL exactly."
        ),
    )
    chart_kind: str = Field(
        default="ranking",
        description=(
            "Chart shape. 'ranking' (bar — top-N items), 'trend' (area — "
            "values over time), 'summary' (single-value KPI). Pick based on "
            "what the user asked for."
        ),
    )


def _build_dynamic_chart(
    *,
    rows: list[dict[str, Any]],
    x_col: str | None,
    y_col: str | None,
    kind: str,
    question: str,
) -> dict[str, Any]:
    """Convert raw SQL rows into the SalesChart-shaped chart_data dict the
    frontend ChatChart already understands.

    Returns the same {kind, totals, series, items} structure produced by the
    legacy ResultAggregator, populated from whatever the LLM aliased.
    """
    if not rows:
        return {
            "kind":        kind if kind in ("ranking", "trend", "summary") else "summary",
            "intent_type": "user_table_query",
            "granularity": "daily",
            "totals":      {"total_sales": 0.0, "orders": 0, "customers": 0},
            "series":      [],
            "items":       [],
            "empty":       True,
        }

    available_cols = list(rows[0].keys())

    # Auto-pick X/Y columns if the LLM didn't specify them.
    if x_col is None or x_col not in available_cols:
        # First non-numeric column is the natural X.
        for c in available_cols:
            v = rows[0].get(c)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                x_col = c
                break
        else:
            x_col = available_cols[0]
    if y_col is None or y_col not in available_cols:
        # First numeric column is the natural Y.
        for c in available_cols:
            v = rows[0].get(c)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                y_col = c
                break
        else:
            # No numeric — pick the last column as a count placeholder.
            y_col = available_cols[-1] if len(available_cols) > 1 else available_cols[0]

    def _y_value(r: dict) -> float:
        v = r.get(y_col)
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    total_value = sum(_y_value(r) for r in rows)
    row_count = len(rows)

    # Pick chart kind from caller, but verify it's reasonable.
    if kind not in ("ranking", "trend", "summary", "comparison"):
        kind = "ranking" if row_count > 1 else "summary"

    if kind == "trend":
        # Time-series: series of {bucket, sales, orders}
        series = [
            {
                "bucket": str(r.get(x_col, "")),
                "sales":  round(_y_value(r), 2),
                "orders": 1,
            }
            for r in rows
        ]
        return {
            "kind":        "trend",
            "intent_type": "user_table_query",
            "granularity": "daily",
            "totals":      {
                "total_sales": round(total_value, 2),
                "orders":      row_count,
                "customers":   0,
            },
            "series":      series,
            "items":       [],
        }

    if kind == "ranking":
        items = [
            {
                "name":   str(r.get(x_col, ""))[:80],
                "sales":  round(_y_value(r), 2),
                "orders": 1,
            }
            for r in rows[:_RAW_SQL_MAX_LIMIT]
        ]
        return {
            "kind":             "ranking",
            "intent_type":      "user_table_query",
            "granularity":      "daily",
            "ranking_subject":  "item",
            "totals":           {
                "total_sales": round(total_value, 2),
                "orders":      row_count,
                "customers":   0,
            },
            "series":           [],
            "items":            items,
        }

    # summary — single value with a token series so the chart still renders.
    return {
        "kind":        "summary",
        "intent_type": "user_table_query",
        "granularity": "daily",
        "totals":      {
            "total_sales": round(total_value, 2),
            "orders":      row_count,
            "customers":   0,
        },
        "series":      [
            {"bucket": str(r.get(x_col, "")), "sales": round(_y_value(r), 2), "orders": 1}
            for r in rows[:50]
        ],
        "items":       [],
    }


class RawSqlQueryTool(Tool):
    name = "query_user_table"
    description = (
        "Run a SELECT statement directly against one of the user-uploaded "
        "u_* tables. Use this for ANY question about data that lives in a "
        "user-uploaded sheet — inventory, store transactions, product "
        "hierarchy, item details, etc. (use run_data_query only for the "
        "legacy sales/purchase tables). The tool validates the SQL, executes "
        "it, and AUTOMATICALLY builds a chart from the result — pass "
        "chart_x_column / chart_y_column with the SELECT aliases you used "
        "so the chart axes match. Always include a LIMIT (max 500). "
        "Use this whenever the answer needs data from u_inventory_master, "
        "u_sales_transactions, u_product_hierarchy, u_sale_report, "
        "u_item_details, or u_transaction_hierarchy."
    )
    args_model = RawSqlQueryArgs
    independent = False

    async def run(self, state: TurnState, args: RawSqlQueryArgs) -> ToolResult:
        from app.dynamic_ingest import known_dynamic_identifiers, list_dynamic_tables

        sql = (args.sql or "").strip().rstrip(";").strip()
        if not sql:
            return ToolResult(ok=False, error="empty SQL")
        if not _RAW_SQL_SELECT.search(sql):
            return ToolResult(ok=False, error="SQL must be a SELECT statement")
        if _RAW_SQL_DANGEROUS.search(sql):
            return ToolResult(ok=False, error="dangerous SQL keyword detected (insert/update/delete/...)")
        if ";" in sql:
            return ToolResult(ok=False, error="only one SQL statement allowed")
        # Safety limit: add LIMIT 50 if not present to prevent huge results
        # filling the 8K context window of small models.
        if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
            sql = sql + " LIMIT 50"

        # Identifier whitelist: every quoted/backticked identifier must be
        # in the dynamic registry. Bare identifiers can be SQL aliases the
        # LLM declares — we don't whitelist those (validator below uses the
        # quoted-identifier check only).
        whitelist = known_dynamic_identifiers() | {
            "u_sales", "u_purchase",  # safety for forward-compat
        }
        unknown: list[str] = []
        for ident in _RAW_SQL_QUOTED_IDENT.findall(sql):
            if ident in whitelist:
                continue
            unknown.append(ident)
        for ident in _RAW_SQL_BACKTICK_IDENT.findall(sql):
            if ident in whitelist:
                continue
            unknown.append(ident)
        if unknown:
            # Allow the LLM to retry with a hint about what IS available.
            tables = [t["table"] for t in list_dynamic_tables()]
            return ToolResult(
                ok=False,
                error=(
                    f"unknown identifier(s) in SQL: {sorted(set(unknown))!r}. "
                    f"Available user tables: {tables}. "
                    f"Use the exact column names from the schema in the system prompt."
                ),
            )

        # Cost guard
        estimated_bytes = max(len(sql) * 10_000, 5_000_000)  # rough heuristic
        try:
            check_sql_scan_estimate(estimated_bytes)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

        # Execute
        _raw_sql_log.info("RAW SQL: %s", sql)
        try:
            rows = await fetch_all(sql, ())
        except sqlite3.Error as e:
            return ToolResult(ok=False, error=f"SQL exec failed: {e}")
        except Exception as e:
            return ToolResult(ok=False, error=f"DB error: {type(e).__name__}: {e}")

        # Enforce max-row safety net (in case LLM forgot LIMIT)
        if len(rows) > _RAW_SQL_MAX_LIMIT:
            rows = rows[:_RAW_SQL_MAX_LIMIT]

        # Build the chart payload
        chart_data = _build_dynamic_chart(
            rows=rows,
            x_col=args.chart_x_column,
            y_col=args.chart_y_column,
            kind=(args.chart_kind or "ranking").strip().lower(),
            question=state.question,
        )

        _raw_sql_log.info(
            "RAW SQL OK: rows=%d chart_kind=%s totals=%s",
            len(rows), chart_data.get("kind"), chart_data.get("totals"),
        )

        # Stash on state so ResponseFormatter / final emit pick it up.
        return ToolResult(
            ok=True,
            output={
                "row_count": len(rows),
                "rows":      rows[:50],  # capped echo for LLM context
                "chart":     {k: v for k, v in chart_data.items() if k != "series" or len(v) <= 30},
            },
            state_updates={
                "rows":        rows,
                "aggregates":  chart_data,
                "chart_data":  chart_data,
                "bytes_scanned": state.bytes_scanned + estimated_bytes,
            },
        )


# Capabilities the agentic loop exposes to the LLM, in no particular order.
QUERY_CAPABILITIES: tuple[str, ...] = (
    "resolve_time_window",
    "resolve_entities",
    "run_data_query",
    "query_user_table",
    "generate_narrative",
    "google_drive",
)


# ===========================================================================
# 7. TOOL REGISTRY
# ===========================================================================

# Authoritative list of tool names — must equal the registered set at boot.
# The first 14 are the fine-grained pipeline tools; the last 5 are the coarse
# LLM-orchestrated capabilities (section 6.5) that compose them.
TOOL_NAMES: tuple[str, ...] = (
    "RouteClassifier",
    "IntentAnalyzer",
    "TimeKPI",
    "EntityResolver",
    "SchemaRetriever",
    "SqlPlanner",
    "SqlWriter",
    "SqlValidator",
    "SqlExecutor",
    "ResultAggregator",
    "InsightEngine",
    "ResponseFormatter",
    "ResponseStored",
    "Database",
    "resolve_time_window",
    "resolve_entities",
    "run_data_query",
    "query_user_table",
    "generate_narrative",
    "google_drive",
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool must have a name")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    @property
    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def schemas_for_prompt(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in self._tools.values():
            try:
                schema = tool.args_model.model_json_schema()
            except Exception:
                schema = {"type": "object", "properties": {}}
            out.append({
                "name": tool.name,
                "description": tool.description,
                "args_schema": schema,
            })
        return out

    async def execute(
        self, name: str, args: dict[str, Any], state: TurnState
    ) -> ToolResult:
        # Wrap every tool execution in a Sentry span so per-tool latency +
        # failures show up in the transaction tree. Breadcrumb is dropped
        # before + after so a failed tool has a chain of "X start", "X
        # FAILED" entries on the parent transaction. No-op when SENTRY_DSN
        # is unset.
        from app.monitoring import instrument_tool  # local import (cycle-safe)
        try:
            tool = self.get(name)
        except KeyError as e:
            return ToolResult(ok=False, error=f"Unknown tool: {e}")
        with instrument_tool(
            name,
            turn_id=state.turn_id,
            conversation_id=state.conversation_id,
        ):
            return await tool.execute(state, args)


_registry: ToolRegistry | None = None


def _bootstrap(registry: ToolRegistry) -> None:
    """Register the 14 pipeline tools + 4 LLM-orchestrated capabilities."""
    classes: list[type[Tool]] = [
        RouteClassifierTool,
        IntentAnalyzerTool,
        TimeKPITool,
        EntityResolverTool,
        SchemaRetrieverTool,
        SqlPlannerTool,
        SqlWriterTool,
        SqlValidatorTool,
        SqlExecutorTool,
        ResultAggregatorTool,
        InsightEngineTool,
        ResponseFormatterTool,
        ResponseStoredTool,
        DatabaseTool,
        ResolveTimeWindowTool,
        ResolveEntitiesTool,
        RunDataQueryTool,
        RawSqlQueryTool,
        GenerateNarrativeTool,
        GoogleDriveTool,
    ]

    registered_names: list[str] = []
    for cls in classes:
        instance = cls()
        registry.register(instance)
        registered_names.append(instance.name)

    expected = set(TOOL_NAMES)
    actual = set(registered_names)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise RuntimeError(
            f"Tool registry mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _bootstrap(_registry)
    return _registry


# ===========================================================================
# AGENT ORCHESTRATION — coordinator + 7 sub-agents (was app/agents.py)
# ===========================================================================


# ---------------------------------------------------------------------------
# 2.0 Bridge — from app.infrastructure (was app.database)
# ---------------------------------------------------------------------------
from app.infrastructure import (ALLOWED_TABLES,
    COLUMN_TYPES,
    REQUIRED_COLUMNS,
    SCHEMA_COLUMNS,
    UploadError,
    cache_key_for,
    fetch_one,
    get_cached,
    get_connection,
    map_headers_strict,
    put_cached,
    quoted,
    record_upload_meta,
    stream_parse_csv_with_detection,
    stream_parse_xlsx_with_detection,)
# Symbols originally imported from app.tools are now defined above in this same
# module (tools layer is concatenated into analytics_engine.py).

import logging
import re
from collections import defaultdict
from datetime import date as _date_cls, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

# ===========================================================================
# 1. COORDINATOR
# ===========================================================================

# ---------------------------------------------------------------------------
# 1.1 Top-level query-kind guard
# ---------------------------------------------------------------------------

# Coarse-grained outer classifier that runs BEFORE any sub-agent dispatch.
# It separates four mutually-exclusive intents:
#
#   data_query        — answerable from the uploaded financial dataset.
#                       Routes to the existing intent classifier + sub-agent.
#   missing_data      — refers to a dimension the dataset doesn't carry
#                       (employee/salary, age/demographics, location,
#                       inventory, profit/cost, marketing spend, etc.).
#                       Returns a deterministic "we don't track that"
#                       template — never invents numbers, never runs SQL.
#   general_knowledge — definitions, how-to, advice ("what is profit
#                       margin?", "how can I improve sales?"). Routes to
#                       a Groq chat call with a knowledge-permissive prompt.
#   chat              — greetings + small talk.

Mode = Literal["chat", "agentic"]
QueryKind = Literal["data_query", "missing_data", "general_knowledge", "chat"]


# Out-of-scope dimensions. Each entry: (label, keyword tuple, friendly noun).
# Keywords use space-padded matching for short tokens to avoid false hits
# (e.g. " age " won't match "package").
_MISSING_DIMENSIONS: list[tuple[str, tuple[str, ...], str]] = [
    ("employee_salary", (
        "employee", "employees", "salary", "salaries", "wage", "wages",
        "payroll", "headcount", "hr ", "staff cost", "compensation",
        "employees pay",
    ), "employee or salary"),
    ("demographics", (
        "customer age", " ages ", " age ", "age group", "gender",
        "demographic", "demographics", "birthday", "birthdate",
        "ethnicity", "marital status",
    ), "customer demographic"),
    ("location", (
        " city ", " cities ", " state ", " states ",
        "country", "countries", " region ", " regions ",
        "geography", "geographic", "geo-",
        "warehouse", "warehouses", "branch", "branches",
        "outlet", "outlets", " zip ", "pincode", "store-wise",
        "by location",
    ), "location or geographic"),
    ("inventory", (
        "inventory", "stock level", "stock levels",
        "in stock", "out of stock", "warehouse stock",
        "remaining stock", "units left", "units in stock",
        "available stock", "stockout", "reorder",
    ), "inventory or stock"),
    ("cost_profit", (
        "cost of goods", "gross margin", "net margin",
        "gross profit", "net profit", "profit margin", "margin %",
        "operating cost", "operating expense",
        "expense", "expenses", " cogs ",
    ), "cost or profit margin"),
    ("marketing", (
        "marketing spend", "marketing cost", "ad spend",
        "campaign cost", "marketing budget", "promotion cost",
        "ad budget", "advertising spend",
    ), "marketing spend"),
    ("category", (
        "by category", "product category", "by size",
        "by colour", "by color", "size breakdown",
        "category sales", "category-wise",
        # Bare-word matches — any query mentioning category/categories
        # routes to the missing-data response (schema has Product Name but
        # no Category column). Keep these space-padded so they match as
        # whole words inside the padded query.
        " category ", " categories ",
    ), "product category / size / colour"),
    ("supplier_cost", (
        "supplier cost", "vendor cost", "purchase cost",
        "raw material", "supplier price",
    ), "supplier cost"),
    ("returns", (
        "return rate", "return reason", "return analysis",
        "refund analysis", "return frequency", "refund rate",
    ), "returns analysis"),
]


# Knowledge / advice / definition phrasings. The negative lookahead
# `(?!my\b|our\b)` after "what is/are/does" means "what is profit margin?"
# matches but "what is my profit margin?" does NOT — the latter is a
# data-lookup attempt and falls through to the missing-data check.
_KNOWLEDGE_VERB_RE = re.compile(
    r"\b(?:"
    r"how\s+(?:can\s+i|to|do\s+i|should\s+i|do\s+you|can\s+you)\s+"
    r"(?:improve|increase|grow|boost|scale|reduce|lower|"
    r"optimi[sz]e|measure|track|compute|calculate|analyz?e|"
    r"interpret|understand|forecast|predict|build|setup|set\s+up)|"
    # "how do/does <noun phrase> work" — multi-word allowed
    r"how\s+(?:does|do)\s+\w+(?:\s+\w+){0,3}\s+work|"
    # "what is/are/does X" — exclude data-lookup ("my/our") and the chat
    # pattern "what is your name" (the chat-pattern check below handles it,
    # but adding `your` here makes the gate explicit). Also exclude ranking
    # phrasings ("what are the top 10", "what are the best/highest/...")
    # which are data lookups despite the "what are the" lead-in.
    # Note: the article alternation below is REQUIRED-not-optional (no `?`),
    # and articles themselves are blocked from being the content word, so
    # the regex can't backtrack into accepting "the/a/an" as the X.
    r"what(?:\s+is|\s+are|\s+does|'s)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?!my\b|our\b|your\b|the\b|a\b|an\b|"
    r"top\b|bottom\b|best\b|worst\b|highest\b|lowest\b|"
    r"peak\b|busiest\b|main\b|biggest\b|largest\b|smallest\b|fastest\b|"
    r"slowest\b|most\b|least\b|leading\b|key\b|primary\b|"
    r"daily\b|weekly\b|monthly\b|yearly\b|total\b|average\b|avg\b|"
    r"repeat\b|seasonal\b)\w+|"
    # "explain X", "define X", "describe X", "tell me about X" — all reject
    # data-lookup phrasings ("my", "our", "last X", "this X", "the X").
    # That single change fixes the previous bug where "tell me about my sales"
    # got routed to general_knowledge instead of data_query.
    r"explain\s+(?!my\b|our\b|last\b|this\b|the\b)|"
    r"define\s+(?!my\b|our\b|last\b|this\b|the\b)|"
    r"describe\s+(?!my\b|our\b|last\b|this\b|the\b)|"
    r"tell\s+me\s+about\s+(?!my\b|our\b|last\b|this\b|the\b|past\b|previous\b|today\b|yesterday\b)|"
    r"tips\s+(?:for|on)\s+|advice\s+(?:for|on)\s+|best\s+practice|"
    r"why\s+is\s+\w+\s+important|"
    r"meaning\s+of\s+|definition\s+of\s+"
    r")",
    re.IGNORECASE,
)


# Labels that ARE answerable from user-uploaded dynamic tables. When the
# user has any u_* table loaded, these missing-data flags are suppressed —
# the agentic loop + query_user_table will figure out whether the relevant
# u_* table actually has the column, far better than a static keyword list
# can. Kept narrow on purpose: employee_salary / demographics / marketing
# / returns stay as missing-data triggers because they're rarely covered
# by upload sheets even when dynamic tables exist.
_DYNAMIC_COVERABLE_LABELS: frozenset[str] = frozenset({
    "inventory", "location", "category", "cost_profit", "supplier_cost",
})


def _check_missing_dimension(question_lower: str) -> dict[str, Any] | None:
    """Return a hints dict if the question explicitly references an
    out-of-scope dimension, otherwise None.

    When dynamic tables exist, labels in _DYNAMIC_COVERABLE_LABELS are
    suppressed — the LLM with the u_* schema will judge feasibility on its
    own.
    """
    has_dynamic = False
    try:
        from app.dynamic_ingest import list_dynamic_tables
        has_dynamic = bool(list_dynamic_tables())
    except Exception:
        has_dynamic = False

    padded = " " + question_lower + " "
    for label, kws, friendly in _MISSING_DIMENSIONS:
        if has_dynamic and label in _DYNAMIC_COVERABLE_LABELS:
            continue
        for kw in kws:
            needle = kw if len(kw) >= 5 else f" {kw.strip()} "
            if needle in padded:
                return {
                    "matched":       kw,
                    "missing_label": label,
                    "friendly_name": friendly,
                }
    return None


async def _has_any_uploaded_data() -> bool:
    """Quick probe: does the user have any data on file (legacy
    sales/purchase OR any dynamic u_* table)?

    Used by the intent classifier to bias ambiguous business-flavored
    questions toward agentic mode when data exists. Fails open: any DB
    error is treated as "data exists" so we never silently downgrade a
    real analytics question to chat just because the probe failed.
    """
    # Dynamic tables count as data too.
    try:
        from app.dynamic_ingest import list_dynamic_tables
        if list_dynamic_tables():
            return True
    except Exception:
        pass
    try:
        row = await fetch_one(
            f'SELECT '
            f'(SELECT COUNT(*) FROM {quoted("sales")}) '
            f'+ (SELECT COUNT(*) FROM {quoted("purchase")}) AS n'
        )
    except Exception:
        return True
    return bool(row and int(row.get("n") or 0) > 0)


# --- Scoring vocab (analytics intent) -------------------------------------
# Each category contributes additively to `analytics_score`. Domain words
# alone are usually enough; time + metric + question patterns layer on top.
DOMAIN_WORDS = frozenset({
    "sale", "sales", "selling", "sold", "sell",
    "purchase", "purchases", "purchased", "buying", "bought",
    "revenue", "income", "earning", "earnings", "turnover", "gmv",
    "transaction", "transactions",
    "order", "orders",
    "customer", "customers", "buyer", "buyers", "client", "clients",
    "vendor", "vendors", "supplier", "suppliers", "party", "parties",
    "invoice", "invoices", "bill", "bills",
    "product", "products", "item", "items", "sku",
    "report", "reports", "dashboard",
    "kpi", "kpis", "analytics", "analysis",
})

METRIC_WORDS = frozenset({
    "total", "sum", "count", "average", "avg", "mean", "aov",
    "metric", "metrics", "performance", "summary", "stats", "statistics",
    "amount", "amt", "value",
})

TREND_WORDS = frozenset({
    "trend", "trends", "growth", "decline", "drop", "rise", "rose", "fell",
    "increase", "increased", "decrease", "decreased", "movement",
    "compare", "compared", "comparison", "versus",
    "best", "worst", "top", "bottom", "highest", "lowest",
    "rca", "forecast", "predict", "prediction", "projection",
})

TIME_WORDS = frozenset({
    "today", "yesterday", "tomorrow",
    "week", "weeks", "weekly",
    "day", "days", "daily",
    "month", "months", "monthly",
    "year", "years", "yearly", "annual", "annually",
    "quarter", "quarters", "quarterly",
    "hour", "hours", "hourly",
    "ytd",
})

TIME_PHRASES: tuple[str, ...] = (
    "last week", "last month", "last year", "last quarter",
    "this week", "this month", "this year", "this quarter",
    "year to date", "month to date", "week to date",
    "past week", "past month", "past year",
    "previous week", "previous month",
)

QUESTION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bhow\s+(?:much|many)\b", re.I),
    re.compile(r"\b(?:show|tell|give|fetch|list|find|get)\s+(?:me|us|the|my|our)\b", re.I),
    re.compile(r"\bdid\s+(?:i|we)\b", re.I),
    re.compile(r"\bcan\s+you\s+(?:show|tell|give|fetch|find|list|get)\b", re.I),
)

NUMBER_TIME_RE = re.compile(
    r"\b\d+\s*(?:day|days|week|weeks|month|months|year|years|quarter|quarters)\b", re.I
)

CURRENCY_RE = re.compile(r"\b(?:rs|inr|₹|\$|usd|rupees?)\b", re.I)

POSSESSIVE_TOKENS = frozenset({"my", "our", "mine", "ours"})

ANALYTICS_THRESHOLD = 0.6   # firm cutoff for "definitely a data question"
ANALYTICS_SOFT       = 0.3   # weaker cutoff used when has_data context favors agentic


# --- Scoring helpers ------------------------------------------------------

def _score_analytics(
    lower: str, normal: str, tokens: set[str]
) -> tuple[float, list[str]]:
    """Score the analytics signal in a question. Returns (score, matched_patterns)."""
    score = 0.0
    matches: list[str] = []

    domain_hits = tokens & DOMAIN_WORDS
    if domain_hits:
        matches.append(f"domain:{sorted(domain_hits)[0]}")
        score += 1.0

    metric_hits = tokens & METRIC_WORDS
    if metric_hits:
        matches.append(f"metric:{sorted(metric_hits)[0]}")
        score += 0.5

    trend_hits = tokens & TREND_WORDS
    if trend_hits:
        matches.append(f"trend:{sorted(trend_hits)[0]}")
        score += 0.5

    time_hits = tokens & TIME_WORDS
    if time_hits:
        matches.append(f"time:{sorted(time_hits)[0]}")
        score += 0.4

    for phrase in TIME_PHRASES:
        if phrase in lower:
            matches.append(f"time_phrase:{phrase}")
            score += 0.3
            break

    if NUMBER_TIME_RE.search(lower):
        matches.append("num_time_pattern")
        score += 0.4

    if CURRENCY_RE.search(lower):
        matches.append("currency")
        score += 0.3

    for pat in QUESTION_PATTERNS:
        if pat.search(lower):
            matches.append("question_verb")
            score += 0.2
            break

    return score, matches


def _score_chat(
    lower: str, normal: str
) -> tuple[float, list[str]]:
    """Score the chat / small-talk signal. Returns (score, matched_patterns)."""
    if normal in _CHAT_EXACT:
        return 2.0, [f"chat_exact:{normal}"]
    matches: list[str] = []
    score = 0.0
    for pat in _CHAT_PATTERNS:
        if pat.search(lower):
            matches.append(f"chat_pat:{pat.pattern[:32]}")
            score += 1.5
            break
    return score, matches


# --- Conversational continuity (per-conversation_id, in-memory) -----------
# Single-user MVP — one process, no concurrent users — so a module-level
# dict keyed by conversation_id is safe.

_LAST_KIND_BY_CONVO: dict[str, "QueryKind"] = {}
_LAST_KIND_MAX = 1024


def _convo_key(conversation_id: str | None) -> str:
    return (conversation_id or "default").strip()


def _get_last_kind(conversation_id: str | None = None) -> "QueryKind | None":
    return _LAST_KIND_BY_CONVO.get(_convo_key(conversation_id))


def _set_last_kind(kind: "QueryKind", conversation_id: str | None = None) -> None:
    key = _convo_key(conversation_id)
    if len(_LAST_KIND_BY_CONVO) >= _LAST_KIND_MAX and key not in _LAST_KIND_BY_CONVO:
        # FIFO eviction.
        first = next(iter(_LAST_KIND_BY_CONVO))
        _LAST_KIND_BY_CONVO.pop(first, None)
    _LAST_KIND_BY_CONVO[key] = kind


def _reset_last_kind(conversation_id: str | None = None) -> None:
    if conversation_id is None:
        _LAST_KIND_BY_CONVO.clear()
    else:
        _LAST_KIND_BY_CONVO.pop(_convo_key(conversation_id), None)


# --- The new classifier ---------------------------------------------------

def classify_query_kind(
    question: str,
    *,
    has_data: bool = True,
    previous_kind: QueryKind | None = None,
) -> tuple[QueryKind, float, dict[str, Any]]:
    """4-way scoring classifier with conversational + has-data context.

    Returns (kind, confidence, hints) where hints contains a structured
    decision log:
        {
          "query":            "<original>",
          "analytics_score":  0.91,
          "chat_score":       0.0,
          "knowledge":        bool,
          "missing":          dict | None,
          "matched_patterns": ["domain:sales", "time_phrase:last week", ...],
          "selected_mode":    "data_query",
          "reason":           "<short>",
        }

    Design rules (in order of evaluation):
      1. Empty input          → chat
      2. Exact small-talk     → chat
      3. KNOWLEDGE wins ONLY when k_score≥1 AND analytics_score<ANALYTICS_THRESHOLD
                                AND no possessive ("my"/"our") in the question.
      4. MISSING_DATA wins when an explicit out-of-scope dimension is named
                                AND analytics_score is not strong enough to override.
      5. Strong analytics signal (≥ ANALYTICS_THRESHOLD) → data_query.
      6. Has-data + ANY analytics signal (≥ ANALYTICS_SOFT) → data_query.
      7. Conversational continuity: short follow-up after a data_query → data_query.
      8. Strong chat patterns → chat.
      9. Default short → chat. Default longer + has_data → data_query.
     10. Final fallback → chat.
    """
    if not question or not question.strip():
        return "chat", 1.0, {
            "query": question, "analytics_score": 0.0, "chat_score": 0.0,
            "knowledge": False, "missing": None, "matched_patterns": [],
            "selected_mode": "chat", "reason": "empty input",
        }

    # Typo-correct domain words BEFORE the scoring scan so phrasings like
    # "topest selling produt this mounth" still hit DOMAIN_WORDS / METRIC_WORDS.
    # Pure deterministic — no LLM. See `_normalize_typos`.
    question = _normalize_typos(question)

    lower = question.lower().strip()
    normal = re.sub(r"\s+", " ", lower).strip(" .,!?;:'\"")
    tokens = set(normal.split())

    # Score each axis.
    a_score, a_matches = _score_analytics(lower, normal, tokens)
    c_score, c_matches = _score_chat(lower, normal)
    has_knowledge = bool(_KNOWLEDGE_VERB_RE.search(lower))
    missing = _check_missing_dimension(lower)
    has_possessive = bool(tokens & POSSESSIVE_TOKENS)

    # Conversational continuity bias — if the previous turn was a data
    # query and the current turn already has *some* analytics signal,
    # boost it so short follow-ups like "what about last week" stay agentic.
    if previous_kind == "data_query" and a_score > 0:
        a_score += 0.5
        a_matches.append("continuity_bias")

    matched_patterns = list(a_matches) + list(c_matches)
    if has_knowledge:
        matched_patterns.append("knowledge_verb")
    if missing:
        matched_patterns.append(f"missing:{missing.get('missing_label')}")

    base_log: dict[str, Any] = {
        "query":            question,
        "analytics_score":  round(a_score, 2),
        "chat_score":       round(c_score, 2),
        "knowledge":        has_knowledge,
        "missing":          missing,
        "has_data":         has_data,
        "previous_kind":    previous_kind,
        "matched_patterns": matched_patterns,
    }

    def _decide(kind: QueryKind, conf: float, reason: str) -> tuple[
        QueryKind, float, dict[str, Any]
    ]:
        return kind, conf, {**base_log, "selected_mode": kind, "reason": reason}

    # 1. Hard small-talk override (exact-match phrase like "hi", "thanks").
    if normal in _CHAT_EXACT:
        return _decide("chat", 0.95, f"chat_exact:{normal!r}")

    # 2. Chat regex patterns ("how are you", "what is your name") — checked
    #    before the knowledge gate so they win over `what is X` matches.
    for pat in _CHAT_PATTERNS:
        if pat.search(lower):
            return _decide("chat", 0.9, f"chat_pattern:{pat.pattern[:32]}")

    # 3. KNOWLEDGE wins for genuine definitional / advice queries. The
    #    regex's own lookaheads already exclude data-lookup phrasings
    #    ("explain my X", "tell me about my/last/this X"), so any match
    #    here is a real knowledge intent — even when domain words appear
    #    ("explain net revenue", "tell me about customer retention").
    if has_knowledge:
        return _decide(
            "general_knowledge", 0.92,
            "knowledge phrasing (regex match after data-lookup filter)",
        )

    # 4. MISSING_DATA wins whenever an out-of-scope dimension is explicitly
    #    named ("sales by city", "ad spend last month", "employee headcount").
    #    The analytics pipeline can't answer these — better to surface a
    #    clear "we don't track that" message than run SQL that has no
    #    matching column.
    if missing is not None:
        hints = {**base_log, "selected_mode": "missing_data",
                 "reason": f"missing dimension {missing.get('missing_label')!r}",
                 **missing}
        return "missing_data", 0.93, hints

    # 4. Strong analytics signal → data_query.
    if a_score >= ANALYTICS_THRESHOLD:
        return _decide(
            "data_query",
            min(0.95, 0.55 + a_score / 6),
            f"analytics_score {a_score:.2f} ≥ {ANALYTICS_THRESHOLD}",
        )

    # 5. Has-data context: if there's data on file AND any analytics signal,
    #    bias strongly toward agentic. This is what catches casual phrasings
    #    like "last week" / "last 8 days" (time-only, no domain word).
    if has_data and a_score >= ANALYTICS_SOFT:
        return _decide(
            "data_query", 0.7,
            f"has_data + analytics_score {a_score:.2f} ≥ {ANALYTICS_SOFT}",
        )

    # 6. Conversational continuity: very short follow-ups after a data turn
    #    inherit the analytics route ("and last week", "what about may").
    if previous_kind == "data_query" and len(tokens) <= 6 and a_score == 0 \
            and c_score == 0 and not has_knowledge:
        return _decide(
            "data_query", 0.6,
            "follow-up to previous data turn",
        )

    # 7. Strong chat patterns → chat.
    if c_score >= 1.0:
        return _decide("chat", 0.9, f"chat_score {c_score:.2f} ≥ 1.0")

    # 8. Default routing — short tail → chat, longer with data → agentic.
    if len(tokens) <= 4 and len(normal) <= 30:
        if has_data and a_score >= ANALYTICS_SOFT:
            return _decide("data_query", 0.55, "short query with weak analytics signal + data")
        return _decide("chat", 0.5, f"short low-signal ({len(tokens)} tokens)")

    if has_data:
        return _decide(
            "data_query", 0.55,
            "longer ambiguous query; data exists → defaulting to agentic",
        )
    return _decide(
        "chat", 0.4,
        "no signals; no uploaded data; defaulting to chat",
    )


# ---------------------------------------------------------------------------
# 1.2 Legacy chat-vs-agentic classifier — kept as a thin compatibility
# wrapper around classify_query_kind so external callers (tests / docs)
# keep working.
# ---------------------------------------------------------------------------


_AGENTIC_KEYWORDS: tuple[str, ...] = (
    # Domain nouns
    "sale", "sales", "purchase", "purchases", "revenue", "income",
    "earning", "earnings", "turnover", "transaction", "transactions",
    "order", "orders", "customer", "customers", "buyer", "buyers",
    "vendor", "vendors", "supplier", "suppliers", "party",
    "invoice", "bill", "report", "dashboard", "kpi", "kpis",
    "analytics", "analysis", "trend", "growth",
    "forecast", "predict", "projection",
    "rca", "root cause",
    # Money / currency markers
    "amount", "₹", "rupee", "rupees", "rs.", "rs ", "dollar", "$",
    # Time references that imply a query window
    "today", "yesterday", "this week", "last week",
    "this month", "last month", "this year", "last year",
    "this quarter", "last quarter", "ytd",
    # Operations on data
    "upload", "import", "csv", "xlsx", "excel", "drive",
    "fetch", "show me", "give me", "list ", "find ",
    # Trigger phrases
    "why did", "what caused", "compare", " vs ", "versus",
    "best seller", "top selling", "top ",
    "how much", "how many", "total ",
)

_CHAT_EXACT: frozenset[str] = frozenset({
    "hi", "hii", "hiii", "hello", "helo", "hey", "yo", "sup",
    "hi there", "hey there", "hello there",
    "good morning", "good afternoon", "good evening", "good night",
    "morning", "evening", "afternoon",
    "thanks", "thank you", "thx", "ty",
    "ok", "okay", "cool", "nice", "great", "awesome", "alright", "got it",
    "bye", "goodbye", "see you", "see ya", "cya",
})

_CHAT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^(hi|hii|hello|hey|yo|sup)[\s,!.?]*$", re.I),
    re.compile(r"^how\s+are\s+(you|u|ya|things)\b", re.I),
    re.compile(r"^who\s+are\s+you\b", re.I),
    re.compile(r"^what(?:'s|\s+is)\s+your\s+name\b", re.I),
    re.compile(r"^what\s+can\s+you\s+do\b", re.I),
    re.compile(r"^help\s*$", re.I),
    re.compile(r"^(thank\s*you|thanks|thx|ty)\b", re.I),
    re.compile(r"^(good|nice|great|happy)\s+(morning|afternoon|evening|night|day)\b", re.I),
    re.compile(r"^are\s+(you|u)\s+(there|online|alive|ok|good)\b", re.I),
    re.compile(r"^(bye|goodbye|see\s+(you|ya)|cya)\b", re.I),
)


def _normalize(question: str) -> tuple[str, str]:
    """Returns (lowercased_full, normalized_for_exact_match)."""
    q = (question or "").strip()
    lower = q.lower()
    normalized = re.sub(r"\s+", " ", lower).strip(" .,!?;:'\"")
    return lower, normalized


def classify(
    question: str,
    *,
    has_data: bool = True,
    previous_kind: QueryKind | None = None,
) -> tuple[Mode, str]:
    """Decide CHAT vs AGENTIC. Thin wrapper around `classify_query_kind`.

    Returns (mode, human-readable reason). The 4-way kind from the new
    classifier collapses as: data_query → agentic; everything else → chat.
    External callers (regression tests, docs) keep working unchanged.
    """
    kind, conf, hints = classify_query_kind(
        question, has_data=has_data, previous_kind=previous_kind,
    )
    mode: Mode = "agentic" if kind == "data_query" else "chat"
    return mode, f"kind={kind} conf={conf:.2f} reason={hints.get('reason')}"


# ---------------------------------------------------------------------------
# 1.2 Dispatch — the LLM-orchestrated AgenticLoop replaces the old dispatcher.
# ---------------------------------------------------------------------------
#
# There is no longer a separate "pick a sub-agent" LLM call. run_query_turn
# runs a free deterministic pre-pass (RouteClassifier + IntentAnalyzer) for
# non-binding hints, then hands the turn to AgenticLoop, which is itself the
# orchestrator — the LLM picks capabilities directly. See ADR 0004.


# ---------------------------------------------------------------------------
# 1.3 Chat responder — direct LLM reply for chat-mode turns.
# ---------------------------------------------------------------------------

_chat_log = logging.getLogger("agentic_ai.coordinator.chat")

_CHAT_SYSTEM_PROMPT = """You are Agentic AI — a STRICTLY business-only analytics assistant.

Rules (non-negotiable):
  • This is NOT a general-purpose chatbot.
  • Reply with 1 short sentence to greetings ("hi", "hello", "thanks", "ok").
  • For ANY other question — including general knowledge, science, history,
    geography, programming, entertainment, politics, personal advice,
    tutoring, or random conversation — refuse with EXACTLY this message,
    verbatim, no additions, no suggestions, no educational explanation:

    "This question is not related to your business data or platform
    operations. Please ask questions related to sales, inventory, KPIs,
    reports, analytics, forecasting, or other business modules only."

  • Never explain topics outside the platform's business analytics scope.
  • Never connect the topic back to business — just refuse.
  • Never invent numbers, dates, or any business data.
  • Never apologize at length or hedge. Two-sentence refusal, period.
"""


# Single canonical refusal message used by the deterministic restriction
# handler. Surfaced verbatim on every general_knowledge / off-topic query.
# NEVER adds educational explanation, suggestions, conversational filler,
# or attempts to connect the topic back to business. Two sentences, period.
RESTRICTION_REFUSAL = (
    "This question is not related to your business data or platform "
    "operations. Please ask questions related to sales, inventory, KPIs, "
    "reports, analytics, forecasting, or other business modules only."
)

# Tight whitelist of pure-greeting tokens. Any chat-classified question
# NOT in this set is treated as off-topic and refused deterministically.
# This is the security floor — even if the chat LLM is jailbroken at the
# prompt layer, anything beyond a literal greeting word never reaches it.
_GREETING_TOKENS = frozenset({
    "hi", "hii", "hiii", "hello", "helloo", "hey", "heya",
    "yo", "sup", "hola", "namaste",
    "thanks", "thx", "ty", "thankyou", "thank",
    "ok", "okay", "okk", "okayy", "k",
    "bye", "goodbye", "cya",
    "gm", "gn", "good", "morning", "evening", "night", "afternoon",
})

# Short canned greeting reply — keeps the response deterministic + free.
_GREETING_REPLY = (
    "Hi! I'm your business analytics assistant. Ask me about your sales, "
    "purchases, KPIs, top customers/products, trends, or any other "
    "question about your uploaded data."
)


def _format_currency(v: float | int) -> str:
    """Indian-style currency formatting; falls back to plain numeric."""
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _format_value_for_user(v: Any, output_type: str) -> str:
    if v is None:
        return "no data"
    try:
        if output_type == "currency":
            return _format_currency(v)
        if output_type == "percent":
            return f"{float(v):.2f}%"
        if output_type == "count":
            return f"{int(v):,}"
        if output_type == "ratio":
            return f"{float(v):.2f}"
    except (TypeError, ValueError):
        pass
    return str(v)


def _narrate_kpi(kpi_result) -> str:
    """Executive-style one-to-two-sentence narrative for a KPI result.

    Pure template — no LLM. Sanitizes labels by stripping internal
    parenthetical suffixes (e.g. "(v2)", "(latest dataset day)").

    NEVER mentions formulas, SQL, internal column names, or implementation
    details. Output is what an executive expects to read in a chat bubble.
    """
    from app.kpi.engine import _strip_internal_suffix

    label = _strip_internal_suffix(kpi_result.label or "this metric")
    category = (kpi_result.category or "").lower()
    output_type = (kpi_result.output_type or "").lower()
    v = kpi_result.value
    rows = kpi_result.rows or []

    if kpi_result.error:
        return (
            "I couldn't compute that metric — please make sure your data is "
            "uploaded and try again."
        )

    # --- list output: top-N or distribution ---
    if output_type == "list":
        if not rows:
            # Graceful fallback: surface what IS known from the data instead
            # of a flat "no data" message. The categorization gives the user
            # a clear next step without exposing internals.
            if category == "inventory":
                return (
                    f"No items currently flagged here. Inventory looks healthy "
                    f"for the moment based on recent sales velocity."
                )
            if category == "forecast":
                return (
                    f"No projection available — this becomes accurate after "
                    f"more sales history accumulates."
                )
            if category == "hierarchy_v2":
                return (
                    f"No products in this segment have generated sales yet. "
                    f"Try a different segment or upload broader data."
                )
            return (
                f"No products show activity for this view yet — try a wider "
                f"window or category."
            )
        # Top-N style (label + value rows)
        if "label" in rows[0] and "value" in rows[0]:
            # Filter out entirely-empty rows (zero value).
            non_zero = [r for r in rows if (r.get("value") or 0) != 0]
            if not non_zero:
                return (
                    f"No products show activity for this view yet. "
                    f"Try a wider time range or different segment."
                )
            top = non_zero[0]
            top_label = top["label"] or "(unnamed)"
            top_val_fmt = _format_currency(top["value"])
            if len(non_zero) == 1:
                return f"{label}: {top_label}, contributing {top_val_fmt}."
            second = non_zero[1] if len(non_zero) > 1 else None
            if second:
                return (
                    f"{label}: {top_label} leads with {top_val_fmt}, "
                    f"followed by {second['label']}."
                )
            return f"{label}: {top_label} leads with {top_val_fmt}."
        # Multi-column distribution (e.g. payment methods)
        # Find the column with the largest single value
        best_key = best_val = None
        first = rows[0]
        for k, vv in first.items():
            try:
                fv = float(vv)
                if best_val is None or fv > best_val:
                    best_val = fv
                    best_key = k
            except (TypeError, ValueError):
                continue
        if best_key is not None:
            return (
                f"{label} is led by {best_key} at {_format_currency(best_val)}."
            )
        return f"{label} is available."

    # --- scalar output ---
    formatted = _format_value_for_user(v, output_type)
    if v is None or formatted == "no data":
        return f"No data available for {label} yet."

    # Category-aware framing for the scalar
    if category in ("sales", "orders"):
        return f"{label} stands at {formatted}."
    if category == "customers":
        return f"{label} currently sits at {formatted}."
    if category == "profit":
        if output_type == "percent":
            return f"{label} is currently {formatted}."
        return f"{label} works out to {formatted}."
    if category == "payments":
        return f"{label} is {formatted}."
    if category == "loyalty":
        return f"{label} is {formatted}."
    if category in ("business_health", "time"):
        return f"{label}: {formatted}."

    # Default
    return f"{label}: {formatted}."


def _is_pure_greeting(question: str) -> bool:
    """True when every word in the question is in the greeting whitelist
    (and there are at most 4 words). Anything longer or with off-topic
    tokens fails the check and gets routed to the refusal handler."""
    if not question:
        return False
    import re as _re
    tokens = _re.findall(r"[a-z]+", question.lower())
    if not tokens or len(tokens) > 4:
        return False
    return all(t in _GREETING_TOKENS for t in tokens)


# Deterministic safety net when the Groq client is unreachable / unconfigured.
# Each variant suggests concrete, supported next actions instead of silently
# failing or hallucinating an answer.
_LLM_UNAVAILABLE_FALLBACKS: dict[str, str] = {
    "chat": (
        "Hi! I'm your sales analytics assistant. The conversational AI "
        "model isn't reachable right now, but I can still answer specific "
        "data questions from your uploaded sales — try “top performing "
        "products”, “last 7 days sales”, or “compare this "
        "month vs last month”."
    ),
    "general_knowledge": (
        "I can't reach the AI model right now to answer general business "
        "questions, but the analytics pipeline still works. Ask me a "
        "question about your data — e.g. “top performing products”, "
        "“revenue last 30 days”, or “forecast next month”."
    ),
}


async def respond_chat(
    state: TurnState,
    emit: EventEmitter,
    reason: str,
    *,
    system_prompt: str | None = None,
    mode: str = "chat",
) -> TurnState:
    """Run a single Groq chat call, emit `final`, cache the reply.

    `system_prompt` lets callers swap the persona — e.g. the chat-mode
    greeter prompt vs. the general-knowledge prompt. `mode` is recorded
    on every emitted event + the cache record so SSE consumers can tell
    chat from general_knowledge from missing_data downstream.
    """
    _chat_log.info("chat-mode=%s: question=%r reason=%s", mode, state.question, reason)

    sp = system_prompt or _CHAT_SYSTEM_PROMPT
    groq = get_groq()
    messages = [
        GroqMessage(role="system", content=sp),
        GroqMessage(role="user",   content=state.question),
    ]

    # Stream first; fall back to one-shot on stream error.
    buf: list[str] = []
    stream_error: str | None = None
    async for chunk in groq.complete_stream(
        messages, temperature=0.4, max_tokens=300, force_json=False,
    ):
        if chunk.error:
            stream_error = chunk.error
            break
        if chunk.delta:
            buf.append(chunk.delta)
            await emit.emit("agent.token", {"delta": chunk.delta})
    text = "".join(buf).strip()
    tokens_in = sum(len(m.content) for m in messages) // 4
    tokens_out = max(len(text) // 4, 1)

    if stream_error or not text:
        resp = await groq.complete(
            messages, temperature=0.4, max_tokens=300, force_json=False,
        )
        if resp.error:
            err = f"Chat LLM failed: {resp.error_kind}: {resp.error}"
            _chat_log.warning(err)
            # Surface a deterministic fallback so the user always sees a
            # coherent answer in the chat — never a silent failure.
            fallback = _LLM_UNAVAILABLE_FALLBACKS.get(
                mode, _LLM_UNAVAILABLE_FALLBACKS["chat"],
            )
            await emit.emit("agent.result", {"error": err, "kind": "llm"})
            await emit.emit("final", {
                "answer":     fallback,
                "chart":      None,
                "from_cache": False,
                "mode":       mode,
                "fallback":   True,
            })
            await emit.emit("turn.end", {
                "turn_id":      state.turn_id,
                "errors":       [err],
                "final_answer": fallback,
                "mode":         mode,
            })
            return state.apply(final_answer=fallback).append_error(err)
        text = (resp.content or "").strip()
        tokens_in = resp.tokens_in
        tokens_out = resp.tokens_out

    record = {
        "turn_id":      state.turn_id,
        "cache_key":    state.cache_key,
        "stored_at":    datetime.now().astimezone().isoformat(timespec="seconds"),
        "query":        state.question,
        "mode":         mode,
        "sub_agent":    None,
        "route":        None,
        "sql":          None,
        "rows":         None,
        "aggregates":   None,
        "insights":     None,
        "chart":        None,
        "final_answer": text,
        "router_reason": reason,
    }
    if state.cache_key:
        try:
            put_cached(state.cache_key, record)
        except Exception:
            _chat_log.warning("chat-mode: cache write failed", exc_info=True)

    await emit.emit("final", {
        "answer":     text,
        "chart":      None,
        "from_cache": False,
        "mode":       mode,
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "tokens_in":    tokens_in,
        "tokens_out":   tokens_out,
        "errors":       [],
        "final_answer": text,
        "mode":         mode,
    })
    return state.apply(
        final_answer=text,
        response_record=record,
        tokens_in=state.tokens_in + tokens_in,
        tokens_out=state.tokens_out + tokens_out,
    )


# ---------------------------------------------------------------------------
# 1.4 Missing-data responder — deterministic, never crashes, no LLM needed.
# ---------------------------------------------------------------------------

# Hand-written templates for each known missing dimension. Designed to feel
# professional and helpful — they explicitly state what IS available so the
# user has a productive next step.
_MISSING_TEMPLATES: dict[str, str] = {
    "employee_salary": (
        "The uploaded dataset doesn't include employee or salary information. "
        "It tracks customer-facing sales transactions — date, party name, "
        "product, total amount, and payment type. I can analyse those for "
        "you (try “top performing products” or “sales last "
        "month”), but headcount, payroll, and HR figures aren't part "
        "of the data."
    ),
    "demographics": (
        "Customer demographic data (age, gender, ethnicity, etc.) isn't "
        "available in the current dataset. Each transaction has a customer "
        "name and the product purchased, but no demographic attributes. I "
        "can rank customers by spend or show product-level performance "
        "instead."
    ),
    "location": (
        "The uploaded dataset doesn't include location or geographic "
        "information — there's no city, state, store, or warehouse field. "
        "I can analyse what is there: products, customers, payment types, "
        "and time-based trends."
    ),
    "inventory": (
        "The current dataset only tracks completed sales transactions, not "
        "inventory or stock levels. I can tell you which products sold the "
        "most or how sales are trending, but stock-on-hand and reorder "
        "points aren't available."
    ),
    "cost_profit": (
        "Cost of goods, expenses, and profit margins aren't part of the "
        "uploaded dataset — only revenue, orders, and customer spend. I "
        "can analyse top-line sales, compare periods, or rank products by "
        "revenue, but margin / profit calculations need cost data that "
        "isn't here."
    ),
    "marketing": (
        "Marketing or ad-spend data isn't included in the uploaded dataset. "
        "I can analyse the resulting sales patterns however — try “sales "
        "trend last month” or “top performing products” to see "
        "what moved."
    ),
    "category": (
        "Product category, size, and colour aren't tracked in the dataset — "
        "only product / brand names. I can rank by brand or analyse "
        "brand-level trends instead."
    ),
    "supplier_cost": (
        "Supplier cost or raw-material data isn't part of the uploaded "
        "dataset. The data covers sales transactions only. If you upload a "
        "purchases dataset I can analyse that side too."
    ),
    "returns": (
        "Detailed return-rate or refund-reason analytics aren't tracked in "
        "the dataset. I can see total amounts and payment status (paid / "
        "partial / unpaid), but reasons and return frequency aren't "
        "recorded."
    ),
}

_MISSING_FALLBACK = (
    "The uploaded dataset doesn't include {friendly}. It tracks sales "
    "transactions — date, customer, product, total amount, and payment "
    "type. Try a question about products, customers, revenue, or trends."
)

_missing_log = logging.getLogger("agentic_ai.coordinator.missing")


async def _respond_greeting(
    state: TurnState,
    emit: EventEmitter,
) -> TurnState:
    """Deterministic, LLM-free greeting reply. Fired only when the question
    consists entirely of whitelisted greeting tokens."""
    answer = _GREETING_REPLY
    record = {
        "turn_id":      state.turn_id,
        "cache_key":    state.cache_key,
        "stored_at":    datetime.now().astimezone().isoformat(timespec="seconds"),
        "query":        state.question,
        "mode":         "greeting",
        "final_answer": answer,
    }
    if state.cache_key:
        try:
            put_cached(state.cache_key, record)
        except Exception:
            _chat_log.warning("greeting: cache write failed", exc_info=True)

    await emit.emit("final", {
        "answer":     answer,
        "chart":      None,
        "from_cache": False,
        "mode":       "greeting",
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "errors":       [],
        "final_answer": answer,
        "mode":         "greeting",
    })
    return state.apply(final_answer=answer, response_record=record)


async def _respond_restricted(
    state: TurnState,
    emit: EventEmitter,
    hints: dict[str, Any],
) -> TurnState:
    """Deterministic refusal for off-topic / general-knowledge questions.

    No LLM call. Returns the canonical restriction message verbatim. Cached
    just like every other answer so a repeat of the same off-topic question
    is instant. This is the security floor of the domain-isolation system —
    even if the LLM is bypassed somehow, this handler always wins for
    queries the kind-classifier flagged as general_knowledge.
    """
    answer = RESTRICTION_REFUSAL
    _missing_log.info(
        "restricted: refusing off-topic question=%r matched=%r",
        state.question, hints.get("matched"),
    )

    record = {
        "turn_id":       state.turn_id,
        "cache_key":     state.cache_key,
        "stored_at":     datetime.now().astimezone().isoformat(timespec="seconds"),
        "query":         state.question,
        "mode":          "restricted",
        "sub_agent":     None,
        "route":         None,
        "sql":           None,
        "rows":          None,
        "aggregates":    None,
        "insights":      None,
        "chart":         None,
        "final_answer":  answer,
        "matched_term":  hints.get("matched"),
    }
    if state.cache_key:
        try:
            put_cached(state.cache_key, record)
        except Exception:
            _missing_log.warning("restricted: cache write failed", exc_info=True)

    await emit.emit("restricted", {
        "reason":  "off_topic",
        "matched": hints.get("matched"),
    })
    await emit.emit("final", {
        "answer":     answer,
        "chart":      None,
        "from_cache": False,
        "mode":       "restricted",
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "errors":       [],
        "final_answer": answer,
        "mode":         "restricted",
    })
    return state.apply(final_answer=answer, response_record=record)


async def _respond_missing_data(
    state: TurnState,
    emit: EventEmitter,
    hints: dict[str, Any],
) -> TurnState:
    """Graceful response for out-of-scope dimension queries. Deterministic;
    no LLM, no SQL, no chart. Always returns a usable answer."""
    label = hints.get("missing_label") or ""
    friendly = hints.get("friendly_name") or "this information"
    answer = _MISSING_TEMPLATES.get(
        label, _MISSING_FALLBACK.format(friendly=friendly)
    )
    _missing_log.info(
        "missing-data: question=%r label=%s matched=%r",
        state.question, label, hints.get("matched"),
    )

    record = {
        "turn_id":       state.turn_id,
        "cache_key":     state.cache_key,
        "stored_at":     datetime.now().astimezone().isoformat(timespec="seconds"),
        "query":         state.question,
        "mode":          "missing_data",
        "sub_agent":     None,
        "route":         None,
        "sql":           None,
        "rows":          None,
        "aggregates":    None,
        "insights":      None,
        "chart":         None,
        "final_answer":  answer,
        "missing_label": label,
        "matched_term":  hints.get("matched"),
    }
    if state.cache_key:
        try:
            put_cached(state.cache_key, record)
        except Exception:
            _missing_log.warning("missing-data: cache write failed", exc_info=True)

    await emit.emit("final", {
        "answer":     answer,
        "chart":      None,
        "from_cache": False,
        "mode":       "missing_data",
    })
    await emit.emit("turn.end", {
        "turn_id":       state.turn_id,
        "errors":        [],
        "final_answer":  answer,
        "mode":          "missing_data",
        "missing_label": label,
    })
    return state.apply(final_answer=answer, response_record=record)


# ---------------------------------------------------------------------------
# 1.4 Coordinator loop — single entry point used by /query_stream.
# ---------------------------------------------------------------------------

_loop_log = logging.getLogger("agentic_ai.coordinator.loop")


# Per-intent expected shapes. The diagnostics record below uses these so an
# operator (or a regression test) can see in ONE place what SQL and chart
# kind a given intent should produce — without having to read SqlPlanner or
# ResultAggregator. They MUST stay in sync with `_plan_*` in `SqlPlannerTool`
# and the chart-kind table in `ResultAggregatorTool`.
_INTENT_SHAPES: dict[str, dict[str, str]] = {
    "sales_summary":       {"sql_type": "time_series_summary", "chart": "bar_or_area"},
    "purchase_analysis":   {"sql_type": "time_series_summary", "chart": "bar_or_area"},
    "product_performance": {"sql_type": "grouped_ranking",     "chart": "horizontal_bar"},
    "trend_analysis":      {"sql_type": "time_series_trend",   "chart": "area"},
    "comparison":          {"sql_type": "two_window_compare",  "chart": "bar_or_area"},
    "anomaly_detection":   {"sql_type": "time_series_summary", "chart": "bar_or_area"},
    "root_cause_analysis": {"sql_type": "two_window_compare",  "chart": "bar_or_area"},
    "forecasting":         {"sql_type": "time_series_summary", "chart": "area"},
}


def _build_intent_diagnostics(
    question: str, intent_type: str, intent_hints: dict[str, Any],
) -> dict[str, Any]:
    """Flatten the salient analytics decisions into a single structured
    record. The operator gets one line that says exactly which entity /
    metric / direction / SQL shape / chart kind was chosen.

    Surfaced via the `intent.classified` SSE event so a frontend or log
    pipeline can attach it directly to the turn without re-parsing hints.
    """
    shape = _INTENT_SHAPES.get(intent_type, {})
    if intent_type == "product_performance":
        entity = intent_hints.get("ranking_subject") or "product"
        direction = intent_hints.get("rank_direction") or "desc"
        # entity-ranking ALWAYS produces grouped SQL + a horizontal bar; we
        # tag direction so a UI can flip the legend / colour ramp.
        return {
            "intent_type": "ranking",
            "entity":      entity,
            "metric":      "sales",
            "direction":   direction,
            "top_n":       intent_hints.get("top_n"),
            "chart":       "horizontal_bar",
            "sql_type":    "grouped_ranking",
            "matched":     intent_hints.get("matched"),
        }
    return {
        "intent_type": intent_type,
        "entity":      None,
        "metric":      None,  # IntentAnalyzer fills this once it runs
        "direction":   "desc",
        "top_n":       intent_hints.get("top_n"),
        "chart":       shape.get("chart"),
        "sql_type":    shape.get("sql_type"),
        "matched":     intent_hints.get("matched"),
    }


def _build_kpi_fast_path_chart(kpi_result: Any) -> dict[str, Any]:
    """Synthesize a chart payload for the KPI fast-path response.

    The KPI engine returns either:
      • a single value (e.g. 'total revenue: ₹X')
      • a value + a series breakdown (e.g. daily totals over the KPI window)

    Either way we produce the same SalesChart-compatible shape the frontend
    already renders, so the user gets a visualization for every KPI hit.
    """
    try:
        user_dict = kpi_result.to_user_dict()
    except Exception:
        user_dict = {}

    value = user_dict.get("value")
    try:
        value_f = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        value_f = 0.0

    label = user_dict.get("label") or user_dict.get("name") or "Metric"
    series_in = user_dict.get("series") or user_dict.get("breakdown") or []

    if isinstance(series_in, list) and series_in:
        # Best case — KPI carries a breakdown we can plot as a time series.
        series = []
        for pt in series_in:
            if isinstance(pt, dict):
                bucket = pt.get("bucket") or pt.get("date") or pt.get("x") or ""
                v = pt.get("value") or pt.get("y") or pt.get("sales") or 0
                try:
                    v_f = float(v)
                except (TypeError, ValueError):
                    v_f = 0.0
                series.append({
                    "bucket": str(bucket),
                    "sales":  round(v_f, 2),
                    "orders": 1,
                })
        if series:
            return {
                "kind":        "trend",
                "intent_type": "kpi_fast_path",
                "granularity": "daily",
                "totals":      {
                    "total_sales": round(sum(p["sales"] for p in series), 2),
                    "orders":      len(series),
                    "customers":   0,
                },
                "series":      series,
                "items":       [],
            }

    # Single-value fallback — one bar so the user sees SOMETHING.
    return {
        "kind":             "ranking",
        "intent_type":      "kpi_fast_path",
        "granularity":      "daily",
        "ranking_subject":  "metric",
        "totals":           {
            "total_sales": round(value_f, 2),
            "orders":      1,
            "customers":   0,
        },
        "series":           [],
        "items":            [{
            "name":   str(label)[:80],
            "sales":  round(value_f, 2),
            "orders": 1,
        }],
    }


def _fallback_chart_from_state(state: TurnState) -> dict[str, Any] | None:
    """Last-resort chart builder for the agentic loop's final emit.

    If the agentic LLM finished without populating chart_data (e.g. it
    answered straight from prior context without running query_user_table),
    derive a minimal chart from state.rows or state.aggregates so the user
    still sees a visualization. Returns None when there's nothing plottable
    (no rows, no aggregates).
    """
    if state.chart_data:
        return state.chart_data
    if state.aggregates:
        return state.aggregates
    rows = state.rows or []
    if not rows:
        return None
    # rows is a list of dicts — fall back to the dynamic chart builder.
    try:
        return _build_dynamic_chart(
            rows=rows,
            x_col=None,
            y_col=None,
            kind="ranking" if len(rows) > 1 else "summary",
            question=state.question,
        )
    except Exception:
        return None


async def run_query_turn(state: TurnState, emit: EventEmitter) -> TurnState:
    """Drive a single /query_stream turn end-to-end.

    Flow:
      turn.start
        → cache lookup
            ├── HIT  → cache.hit + final + turn.end (no LLM, no tools)
            └── MISS
                → chat / agentic split
                    ├── "chat"    → chat_responder
                    └── "agentic" → intent classifier
                                       ├── high-confidence → deterministic
                                       │                     mapping → sub-agent
                                       └── low-confidence  → LLM dispatcher
    """
    await emit.emit("turn.start", {
        "turn_id":  state.turn_id,
        "question": state.question,
    })

    # Cache key embeds (data_version, conversation_id, question).
    cache_key = cache_key_for(
        state.question,
        conversation_id=state.conversation_id,
    )
    state = state.apply(cache_key=cache_key)

    # ---- 1. Cache lookup --------------------------------------------------
    cached = get_cached(cache_key)
    if cached:
        cached_mode = cached.get("mode", "agentic")
        await emit.emit("cache.hit", {
            "stored_at": cached.get("stored_at"),
            "mode":      cached_mode,
        })
        # Cache-hit `final` event. Surface the same user-safe `metric`
        # payload that a fresh KPI fast-path would emit, so the frontend
        # contract is identical whether the answer is cached or recomputed.
        final_payload: dict[str, Any] = {
            "answer":     cached.get("final_answer", ""),
            "chart":      cached.get("chart"),
            "from_cache": True,
            "mode":       cached_mode,
        }
        # `aggregates` carries the to_user_dict() shape (no formula/SQL).
        cached_agg = cached.get("aggregates")
        if isinstance(cached_agg, dict) and "format" in cached_agg:
            final_payload["metric"] = cached_agg
        await emit.emit("final", final_payload)
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "from_cache":   True,
            "errors":       [],
            "final_answer": cached.get("final_answer"),
            "mode":         cached_mode,
        })
        return state.apply(
            sub_agent=cached.get("sub_agent"),
            final_answer=cached.get("final_answer"),
            chart_data=cached.get("chart") or cached.get("aggregates"),
            response_record=cached,
        )

    # ---- 1.4. Clarification check (ambiguous branch/store question) ------
    # Fires only when the user has 2+ branches configured. Single-branch
    # deployments (the default) never trigger this.
    try:
        from app.hierarchy import detect_ambiguity
        clar = await detect_ambiguity(state.question)
    except Exception:
        clar = None
    if clar is not None:
        await emit.emit("clarification.needed", clar.to_dict())
        await emit.emit("final", {
            "answer":     clar.prompt,
            "chart":      None,
            "from_cache": False,
            "mode":       "clarification",
            "clarification": clar.to_dict(),
        })
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "from_cache":   False,
            "errors":       [],
            "final_answer": clar.prompt,
            "mode":         "clarification",
        })
        return state.apply(
            sub_agent="ClarificationAgent",
            final_answer=clar.prompt,
            response_record=clar.to_dict(),
        )

    # ---- 1.5. KPI fast-path (deterministic, zero LLM cost) ---------------
    # If the question matches a registered KPI alias at high confidence we
    # short-circuit the whole pipeline and return the exact pre-defined
    # formula result. Falls through silently on no match or low confidence.
    #
    # ADR-0005: skip the KPI fast-path entirely when the user has uploaded
    # dynamic tables. The shipped KPI registry only knows about the legacy
    # sales/purchase + enrichment tables; bypassing the agentic loop would
    # return stale/empty answers when the real data lives in u_* tables.
    # The LLM in the agentic loop can still answer KPI questions correctly
    # via query_user_table — it just costs one LLM call instead of zero.
    try:
        from app.dynamic_ingest import list_dynamic_tables
        _skip_kpi_fast_path = bool(list_dynamic_tables())
    except Exception:
        _skip_kpi_fast_path = False
    try:
        from app.kpi import match_kpi, execute_kpi
        kpi_match = await match_kpi(state.question) if not _skip_kpi_fast_path else None
    except Exception:
        kpi_match = None
    if kpi_match is not None and kpi_match.confidence >= 0.85:
        kpi_result = await execute_kpi(kpi_match.kpi)
        # Emit ONLY a generic "analyzing" event — never leak internal kpi_id
        # or matched_alias to the user-facing stream.
        await emit.emit("analyzing", {"stage": "computing your metric"})

        # Executive-style narrative + user-safe payload.
        answer = _narrate_kpi(kpi_result)

        # ADR-0005: build a chart even on KPI fast-path so the user always
        # gets a visualization. The single KPI value becomes a one-bar
        # ranking chart; if the user-safe dict carries any series we use
        # that as a time-series instead.
        kpi_chart = _build_kpi_fast_path_chart(kpi_result)

        await emit.emit("final", {
            "answer":     answer,
            "chart":      kpi_chart,
            "from_cache": False,
            "mode":       "kpi",
            "metric":     kpi_result.to_user_dict(),   # USER-SAFE — no formula/SQL
        })
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "from_cache":   False,
            "errors":       [] if not kpi_result.error else ["Could not compute the requested metric."],
            "final_answer": answer,
            "mode":         "kpi",
        })
        # Cache the deterministic answer so a repeat question is instant.
        # The cache record keeps the FULL developer dict in `_internal`
        # but only the user-safe one is ever re-streamed to a client.
        try:
            put_cached(cache_key, {
                "mode":         "kpi",
                "sub_agent":    "KPIEngine",
                "final_answer": answer,
                "chart":        kpi_chart,
                "aggregates":   kpi_result.to_user_dict(),
                "_internal":    kpi_result.to_dict(),    # developer-only, not streamed
            })
        except Exception:
            pass
        return state.apply(
            sub_agent="KPIEngine",
            final_answer=answer,
            chart_data=kpi_chart,
            response_record=kpi_result.to_dict(),       # state record is server-side
        )

    # ---- 2. Top-level query-kind guard (deterministic, zero LLM cost) ----
    # Splits the request 4 ways with conversational + has-data context:
    #   data_query        → continue to intent classifier + sub-agent
    #   missing_data      → graceful "we don't track that" template (no SQL)
    #   general_knowledge → LLM with knowledge prompt (no SQL)
    #   chat              → LLM with greeter prompt (no SQL)
    has_data = await _has_any_uploaded_data()
    # Conversation continuity is now per-(user, conversation_id), not global —
    # a second user's "and october" follow-up never inherits the first user's
    # context.
    previous_kind = _get_last_kind(state.conversation_id)
    kind, kind_conf, kind_hints = classify_query_kind(
        state.question,
        has_data=has_data,
        previous_kind=previous_kind,
    )
    _set_last_kind(kind, state.conversation_id)
    _loop_log.info(
        "query.kind: kind=%s conf=%.2f a_score=%.2f c_score=%.2f "
        "has_data=%s prev=%s patterns=%s reason=%s question=%r",
        kind, kind_conf,
        kind_hints.get("analytics_score", 0.0),
        kind_hints.get("chat_score", 0.0),
        has_data, previous_kind,
        kind_hints.get("matched_patterns"),
        kind_hints.get("reason"),
        state.question,
    )
    await emit.emit("query.kind", {
        "kind":       kind,
        "confidence": kind_conf,
        "hints":      kind_hints,
    })
    # Backwards-compat: the legacy `mode.selected` event is still emitted
    # so any SSE consumer keying off it keeps working.
    legacy_mode: Mode = "agentic" if kind == "data_query" else "chat"
    await emit.emit("mode.selected", {
        "mode":   legacy_mode,
        "reason": f"kind={kind}",
    })

    if kind == "missing_data":
        return await _respond_missing_data(state, emit, kind_hints)

    if kind == "general_knowledge":
        # Domain-isolation enforcement: refuse deterministically with the
        # canonical restriction message. No LLM call — saves cost and
        # guarantees the refusal can never be jailbroken at the prompt layer.
        return await _respond_restricted(state, emit, kind_hints)

    if kind == "chat":
        # Deterministic greeting filter: only literal greeting tokens are
        # answered conversationally. Anything else classified as chat by
        # the kind classifier (e.g. "tell me a joke", "who invented AI")
        # is treated as off-topic and refused with the canonical message.
        # This makes refusal independent of the LLM being reachable.
        if _is_pure_greeting(state.question):
            return await _respond_greeting(state, emit)
        return await _respond_restricted(state, emit, kind_hints)

    # data_query — fall through to the existing agentic flow.

    # ---- 3. Cost guard ---------------------------------------------------
    try:
        check_loop_iteration(state)
        check_cost(state)
    except CostGuardError as e:
        await emit.emit("agent.result", {"error": str(e), "kind": "cost_guard"})
        await emit.emit("turn.end", {
            "turn_id": state.turn_id, "errors": [str(e)], "mode": "agentic",
        })
        return state.append_error(str(e))

    # ---- 4. Deterministic pre-pass + agentic loop dispatch ---------------
    # The free deterministic classifiers still run — but their output is now
    # a NON-BINDING hint handed to the LLM, not a routing decision. The LLM
    # itself orchestrates the tools from here on (see ADR 0004).
    intent_type, intent_conf, intent_hints = classify_intent(state.question)
    diagnostics = _build_intent_diagnostics(state.question, intent_type, intent_hints)
    _loop_log.info(
        "intent: type=%s confidence=%.2f hints=%s diagnostics=%s",
        intent_type, intent_conf, intent_hints, diagnostics,
    )
    await emit.emit("intent.classified", {
        "type":        intent_type,
        "confidence":  intent_conf,
        "hints":       intent_hints,
        "diagnostics": diagnostics,
    })

    # Seed state.intent, then run RouteClassifier + IntentAnalyzer as a free
    # pre-pass so the loop opens with populated route + intent hints.
    state = state.apply(
        intent={
            "type":       intent_type,
            "confidence": intent_conf,
            "hints":      intent_hints,
        },
        sub_agent="AgenticLoop",
    )
    registry = get_registry()
    for pre_tool in ("RouteClassifier", "IntentAnalyzer"):
        pre_result = await registry.execute(pre_tool, {}, state)
        if pre_result.ok and pre_result.state_updates:
            state = state.apply(**pre_result.state_updates)

    await emit.emit("sub_agent.dispatched", {
        "sub_agent":  "AgenticLoop",
        "reason":     "LLM orchestrates capabilities directly",
        "intent":     (state.intent or {}).get("type", intent_type),
        "confidence": round(intent_conf, 2),
        "strategy":   "agentic_loop",
    })

    try:
        state = await AgenticLoop().run(state, emit)
    except Exception as e:
        _loop_log.exception("agentic loop crashed")
        await emit.emit("agent.result", {
            "error": f"{type(e).__name__}: {e}",
            "kind":  "internal",
        })
        await emit.emit("turn.end", {
            "turn_id": state.turn_id,
            "errors":  state.errors + [f"{type(e).__name__}: {e}"],
            "mode":    "agentic",
        })
        return state.append_error(f"{type(e).__name__}: {e}")

    if state.errors:
        await emit.emit("agent.result", {
            "error": state.errors[-1], "kind": "internal",
        })
        await emit.emit("turn.end", {
            "turn_id":      state.turn_id,
            "errors":       state.errors,
            "final_answer": state.final_answer,
            "mode":         "agentic",
        })
        return state

    # Success — emit final. ADR-0005: always emit a chart when ANY tabular
    # data is available, falling back to a chart derived from state.rows or
    # state.aggregates when the loop didn't compute chart_data itself.
    final_chart = state.chart_data or _fallback_chart_from_state(state)
    if final_chart is not None and final_chart is not state.chart_data:
        state = state.apply(chart_data=final_chart)
    await emit.emit("final", {
        "answer":     state.final_answer or "",
        "chart":      final_chart,
        "from_cache": False,
        "mode":       "agentic",
    })
    await emit.emit("turn.end", {
        "turn_id":      state.turn_id,
        "iterations":   state.iteration,
        "tokens_in":    state.tokens_in,
        "tokens_out":   state.tokens_out,
        "errors":       [],
        "final_answer": state.final_answer,
        "tool_calls":   [tc.model_dump() for tc in state.tool_calls],
        "mode":         "agentic",
    })
    return state


# ===========================================================================
# 2. AGENTIC LOOP — LLM-orchestrated tool selection (replaces fixed pipelines)
# ===========================================================================
#
# The query path no longer walks a hardcoded tool pipeline. The LLM drives a
# tool-calling loop: it picks a capability, inspects the result, and either
# picks another or emits the final answer. Deterministic shortcuts (response
# cache, KPI fast-path) still sit in front in run_query_turn; only the
# non-shortcut question path reaches this loop. See ADR 0004 — this
# supersedes ADR 0002's fixed deterministic pipelines.

_agents_log = logging.getLogger("agentic_ai.agents")
_loop_agent_log = logging.getLogger("agentic_ai.agentic_loop")

_FORECAST_HORIZON_DAYS = 14


def _project_forecast(series: list[dict]) -> list[dict]:
    if len(series) < 2:
        return series
    xs = list(range(len(series)))
    ys = [float(s.get("sales") or 0) for s in series]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((x - mean_x) ** 2 for x in xs) or 1e-9
    slope = num / den
    intercept = mean_y - slope * mean_x
    last_bucket = series[-1].get("bucket", "")
    try:
        last_dt = datetime.strptime(str(last_bucket), "%Y-%m-%d")
    except ValueError:
        return series
    out = list(series)
    for i in range(1, _FORECAST_HORIZON_DAYS + 1):
        x = (n - 1) + i
        pred = max(slope * x + intercept, 0.0)
        out.append({
            "bucket": (last_dt + timedelta(days=i)).date().isoformat(),
            "sales":  round(pred, 2),
            "orders": 0,
            "predicted": True,
        })
    return out


# ---------------------------------------------------------------------------
# 2.1 Loop prompt + capability tool schemas
# ---------------------------------------------------------------------------

def _schema_summary_text() -> str:
    """Compact dataset description injected into the loop's system prompt.

    Lists the legacy sales/purchase tables AND every dynamic ``u_*`` table
    the user has uploaded (one per sheet of their workbook). The LLM uses
    run_data_query for the legacy tables and query_user_table for the u_*
    tables.
    """
    parts: list[str] = []
    try:
        sd = schema_dict()
        lines = []
        for tname, tinfo in (sd.get("tables") or {}).items():
            cols = ", ".join(c["name"] for c in tinfo.get("columns", []))
            lines.append(f"  - {tname}: {tinfo.get('description', '')}. Columns: {cols}")
        notes = "\n".join(f"  * {n}" for n in sd.get("notes", []))
        body = "Legacy tables (use run_data_query):\n" + "\n".join(lines)
        if notes:
            body += "\nNotes:\n" + notes
        parts.append(body)
    except Exception:
        parts.append(
            'Legacy tables: sales, purchase (identical shape). Key columns: '
            'Date, "Total Amount", "Party Name", "Product Name".'
        )

    # Dynamic tables — added inline so the LLM can write raw SELECTs against
    # them via the query_user_table capability. Compact format: column names
    # only, no types (LLM infers from the data and we're on a TPM budget).
    try:
        from app.dynamic_ingest import list_dynamic_tables
        dyn_tables = list_dynamic_tables()
        if dyn_tables:
            dyn_lines = ["User-uploaded tables (use query_user_table for these):"]
            for t in dyn_tables:
                cols = ", ".join(c["name"] for c in t.get("columns", []))
                dyn_lines.append(
                    f'  - {t["table"]} ({t["row_count"]} rows): {cols}'
                )
            parts.append("\n".join(dyn_lines))
    except Exception:
        pass

    return "\n\n".join(parts)


AGENTIC_LOOP_SYSTEM = """You are the orchestrator for Agentic AI, a business
analytics assistant. A user has asked a question about their uploaded business
data (sales transactions, inventory, product catalog, store transactions,
hierarchy — whichever sheets they've uploaded). Decide which capabilities to
call, in what order, inspect each result, call more if needed — then write the
final answer yourself.

Dataset schema:
{schema}

Available capabilities (call them as tools):
  - resolve_time_window: resolve the date range. Call first whenever the
    question mentions any time period. Dates anchor to the dataset's latest
    date, not today.
  - resolve_entities: map named merchants / customers / products to canonical
    DB values. Call only when the question names a specific entity.
  - run_data_query: pre-built sales analytics over the LEGACY sales /
    purchase tables only. Pick query_type to match the question. Resolve the
    time window first. Use this when the question is about sales/purchase
    activity in the legacy schema.
  - query_user_table: write a raw SELECT against a user-uploaded u_* table
    (u_inventory_master, u_sales_transactions, u_product_hierarchy,
    u_sale_report, u_item_details, u_transaction_hierarchy, etc.). Use this
    for ANY question about inventory, store transactions, product catalog,
    or hierarchy. ALWAYS include a LIMIT and pass chart_x_column +
    chart_y_column matching your SELECT aliases so the chart renders.
  - generate_narrative: write an analyst paragraph over the most recent
    query result. Call only when the user wants explanation/insight, not a
    bare number.
  - google_drive: access the user's Google Drive (action='list' /
    'ingest'). Use only if the user asks to pull from Drive.

Picking the right capability:
  - "low stock", "overstocked", "inventory turnover", "dead stock" →
      query_user_table on u_inventory_master.
  - "top brand", "best category", "subcategory growth", "product hierarchy"
      → query_user_table on u_product_hierarchy or u_sales_transactions.
  - "transactions by region/state/store", "payment method", "peak hours"
      → query_user_table on u_sales_transactions.
  - "Sales last month / total / by Party" on the legacy data →
      run_data_query (sales_summary, product_performance, etc.)

CRITICAL — always produce a chart:
  - run_data_query already builds a chart for sales aggregates.
  - query_user_table builds a chart from your SELECT — but you MUST pass
    chart_x_column (the category/date column) and chart_y_column (the
    numeric value), matching the aliases you used in the SQL exactly. If
    the answer is a single number, write the SQL so it ALSO returns the
    underlying breakdown (e.g. SUM grouped by month) so the chart has
    something to plot.
  - If you find yourself about to answer without having called any data
    capability that produced a chart, call query_user_table FIRST.

Discipline:
  - Call only the capabilities you actually need. A plain "low stock items"
    query needs only one query_user_table call.
  - Always inspect each tool result. If a query reports empty/zero rows,
    decide whether to retry with a different filter or explain the gap.
  - When you have enough to answer, STOP calling tools and reply with the
    final answer as plain text — 2-4 sentences, grounded only in the
    numbers/rows returned. Never invent values. Use the rupee sign for
    Indian currency.
  - Never describe your tool calls or internal steps in the final answer.
"""


def _loop_hints(state: TurnState) -> str:
    """Non-binding deterministic pre-pass signal handed to the LLM."""
    intent = state.intent or {}
    parts: list[str] = []
    if state.route:
        parts.append(f"route={state.route}")
    if intent.get("type"):
        parts.append(f"likely_intent={intent['type']}")
    if intent.get("metric"):
        parts.append(f"metric={intent['metric']}")
    if intent.get("top_n"):
        parts.append(f"top_n={intent['top_n']}")
    return ", ".join(parts) if parts else "none"


def _capability_tool_schemas() -> list[dict[str, Any]]:
    """Build OpenAI-format function schemas for the loop's capabilities,
    sourced from the registered capability tools so they stay in sync."""
    registry = get_registry()
    schemas: list[dict[str, Any]] = []
    for cap_name in QUERY_CAPABILITIES:
        try:
            tool = registry.get(cap_name)
            params = tool.args_model.model_json_schema()
        except Exception:
            continue
        params.pop("title", None)
        schemas.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": params,
            },
        })
    return schemas


def _tool_result_message(tc_id: str, result: ToolResult) -> dict[str, Any]:
    """Render a capability result as a `tool` role message for the LLM.

    Keeps the serialized body under 2000 chars to prevent 413 errors when
    multiple tool rounds accumulate in the context window (llama-3.1-8b has
    an 8K token context window that fills up quickly with large SQL results).
    """
    if result.ok:
        body: dict[str, Any] = {"ok": True, "result": result.output}
    else:
        body = {"ok": False, "error": result.error}
    raw = json.dumps(body, default=str)
    if len(raw) > 1400:
        raw = raw[:1360] + ' ...(truncated)}"}'
    return {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": raw,
    }


# ---------------------------------------------------------------------------
# 2.2 AgenticLoop — the orchestrator
# ---------------------------------------------------------------------------

class AgenticLoop:
    """LLM-orchestrated replacement for the fixed sub-agent pipelines.

    One LLM call per iteration: the model either requests capabilities (we
    execute them and feed the results back) or emits the final answer. The
    cost guard caps iterations + spend; on the ceiling we force one final
    best-effort answer rather than erroring out."""

    name = "AgenticLoop"

    async def run(self, state: TurnState, emit: EventEmitter) -> TurnState:
        groq = get_groq()
        registry = get_registry()

        from app.dynamic_ingest import list_dynamic_tables as _ldt
        _dynamic_tables = _ldt()
        _has_dynamic = bool(_dynamic_tables)

        if _has_dynamic:
            # Dynamic tables present: drop run_data_query and resolve_entities
            # (legacy-only tools). Keep resolve_time_window so the 8b model can
            # still call it — but the tool handler will redirect it to use SQL
            # directly in query_user_table instead.
            _legacy_caps = {"resolve_entities", "run_data_query"}
            tools = [t for t in _capability_tool_schemas() if t["function"]["name"] not in _legacy_caps]
            _dyn_names = ", ".join(t["table"] for t in _dynamic_tables)
            _dyn_prefix = (
                "CRITICAL: the user has uploaded dynamic tables. "
                f"Available tables: {_dyn_names}. "
                "Use ONLY query_user_table for ALL data questions. "
                "Do NOT use resolve_time_window, resolve_entities, or run_data_query "
                "(those only cover the legacy 52-row demo dataset, NOT the user's data).\n\n"
            )
        else:
            tools = _capability_tool_schemas()
            _dyn_prefix = ""

        messages: list[dict[str, Any]] = [
            {"role": "system",
             "content": _dyn_prefix + AGENTIC_LOOP_SYSTEM.format(schema=_schema_summary_text())},
            {"role": "user",
             "content": (
                 f"Question: {state.question}\n"
                 f"Deterministic hints (non-binding): {_loop_hints(state)}"
             )},
        ]

        _total_tool_calls = 0
        _MAX_TOOL_CALLS = 10  # hard cap to prevent 8K context overflow on small models

        while True:
            # Cost guard — on the ceiling, force a best-effort final answer.
            try:
                check_loop_iteration(state)
                check_cost(state)
            except CostGuardError as e:
                _loop_agent_log.info("cost guard hit: %s — forcing final answer", e)
                return await self._force_final(state, emit, messages, groq)

            # Tool-call cap: prevent context overflow on small-context models
            if _total_tool_calls >= _MAX_TOOL_CALLS:
                _loop_agent_log.info(
                    "tool call cap (%d) reached — forcing final answer", _MAX_TOOL_CALLS
                )
                return await self._force_final(state, emit, messages, groq)

            iteration = state.iteration + 1
            state = state.apply(iteration=iteration)

            resp = await groq.complete_with_tools(
                messages, tools=tools, temperature=0.0, max_tokens=900,
            )
            state = state.apply(
                tokens_in=state.tokens_in + resp.tokens_in,
                tokens_out=state.tokens_out + resp.tokens_out,
            )

            if resp.error:
                msg = f"LLM orchestration failed: {resp.error_kind}: {resp.error}"
                _loop_agent_log.warning(msg)
                return state.append_error(msg)

            if not resp.tool_calls:
                # No tool requested — the LLM is satisfied and has answered.
                answer = (resp.content or "").strip() or (
                    "I could not produce an answer for that question."
                )
                return await self._finalize(state, emit, answer, incomplete=False)

            # Record the assistant turn (with its tool_calls) so the tool
            # replies have an anchor to attach to.
            messages.append({
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, default=str),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            })

            for tc in resp.tool_calls:
                await emit.emit("loop.iteration", {
                    "iteration":  iteration,
                    "capability": tc.name,
                    "args":       tc.arguments,
                    "reasoning":  (resp.content or "").strip()[:400],
                })
                await emit.emit("tool.call", {
                    "name": tc.name, "args": tc.arguments, "iteration": iteration,
                })
                if tc.name not in QUERY_CAPABILITIES:
                    result = ToolResult(
                        ok=False,
                        error=(f"unknown capability {tc.name!r}; valid: "
                               f"{', '.join(QUERY_CAPABILITIES)}"),
                    )
                else:
                    result = await registry.execute(tc.name, tc.arguments, state)
                await emit.emit("tool.result", {
                    "name": tc.name, "ok": result.ok, "output": result.output,
                    "error": result.error,
                    "duration_ms": round(result.duration_ms, 2),
                })
                _total_tool_calls += 1
                state = state.append_tool_call(ToolCallRecord(
                    name=tc.name, args=tc.arguments, output=result.output,
                    ok=result.ok, error=result.error,
                    duration_ms=result.duration_ms, iteration=iteration,
                ))
                if result.ok:
                    if result.state_updates:
                        state = state.apply(**result.state_updates)
                    if result.delta_metrics:
                        state = state.apply(
                            tokens_in=state.tokens_in
                            + int(result.delta_metrics.get("tokens_in", 0)),
                            tokens_out=state.tokens_out
                            + int(result.delta_metrics.get("tokens_out", 0)),
                        )
                else:
                    # Feed the error back — the LLM self-corrects next round.
                    _loop_agent_log.info(
                        "capability %s failed: %s", tc.name, result.error
                    )
                # The tool reply goes back into the conversation either way.
                messages.append(_tool_result_message(tc.id, result))

    async def _force_final(
        self,
        state: TurnState,
        emit: EventEmitter,
        messages: list[dict[str, Any]],
        groq: GroqClient,
    ) -> TurnState:
        """Cost-guard ceiling reached. One last tool-free call: answer with
        whatever has been gathered so far."""
        messages.append({
            "role": "user",
            "content": (
                "You have reached the work budget for this question. Do not "
                "request any more tools. Answer now using only what you have "
                "gathered so far. If the picture is incomplete, say so briefly."
            ),
        })
        resp = await groq.complete_with_tools(
            messages, tools=[], temperature=0.0, max_tokens=600,
        )
        state = state.apply(
            tokens_in=state.tokens_in + resp.tokens_in,
            tokens_out=state.tokens_out + resp.tokens_out,
        )
        answer = (resp.content or "").strip() or (
            "I ran out of analysis budget before I could fully answer that. "
            "Please try a narrower question."
        )
        return await self._finalize(state, emit, answer, incomplete=True)

    async def _finalize(
        self,
        state: TurnState,
        emit: EventEmitter,
        answer: str,
        *,
        incomplete: bool,
    ) -> TurnState:
        """Attach the final answer + build/persist the response record.
        run_query_turn emits the `final` / `turn.end` SSE events."""
        if incomplete:
            answer = answer.rstrip() + (
                "\n\n(Note: this answer may be incomplete — the analysis "
                "budget for this question was reached.)"
            )
        aggregates = state.aggregates or {}
        record = {
            "turn_id":         state.turn_id,
            "cache_key":       state.cache_key,
            "stored_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query":           state.question,
            "sub_agent":       "AgenticLoop",
            "route":           state.route,
            "sql":             state.sql_final,
            "rows":            state.rows,
            "aggregates":      aggregates,
            "insights":        state.insights or {},
            "chart":           aggregates or None,
            "final_answer":    answer,
            "response_format": "agentic",
            "incomplete":      incomplete,
        }
        state = state.apply(
            final_answer=answer,
            response_record=record,
            chart_data=state.chart_data or (aggregates or None),
        )
        # Persist for the response cache. An incomplete answer is never cached
        # — a partial result must not later masquerade as a clean cache hit.
        if not incomplete and state.cache_key:
            try:
                put_cached(state.cache_key, record)
            except Exception:
                _loop_agent_log.warning(
                    "response cache persist failed", exc_info=True
                )
        return state


# ---------------------------------------------------------------------------
# 2.7 DashboardAgent — NO LLM. Read-only KPI / series builder for /dashboard.
# ---------------------------------------------------------------------------

_dashboard_log = logging.getLogger("agentic_ai.agents.dashboard")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DashboardAgent:
    """No LLM. Reads sales rows, verifies dates, filters, groups, aggregates.
    Output matches the frontend's DashboardData type exactly:
      { month, kpis: {total_sales, orders, customers}, series: [{bucket, sales, orders}] }
    """

    name = "DashboardAgent"

    async def run(
        self,
        *,
        month: str | None = None,
    ) -> dict[str, Any]:
        """Build the dashboard payload."""
        where_parts: list[str] = []
        params: list[Any] = []
        if month is not None:
            if not (len(month) == 7 and month[4] == "-"):
                raise ValueError(f"invalid month format: {month!r}")
            where_parts.append('"Date" LIKE ?')
            params.append(f"{month}-%")
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = (
            f'SELECT "Date", "Total Amount", "Party Name" '
            f'FROM {quoted("sales")}'
            f'{where_sql}'
        )

        registry = get_registry()
        dummy_state = TurnState(question="<dashboard>")
        result = await registry.execute(
            "Database",
            {"op": "select", "pin": READ_PIN, "sql": sql, "params": params},
            dummy_state,
        )
        if not result.ok:
            raise RuntimeError(f"DashboardAgent read failed: {result.error}")
        raw_rows: list[dict[str, Any]] = list((result.output or {}).get("rows") or [])

        # Step 2 — DateNormalizer (verify, skip non-ISO rows).
        valid: list[dict[str, Any]] = []
        skipped = 0
        for r in raw_rows:
            d = r.get("Date")
            if not isinstance(d, str) or not _ISO_DATE_RE.match(d):
                skipped += 1
                continue
            valid.append(r)
        if skipped:
            _dashboard_log.warning("DashboardAgent skipped %d rows with non-ISO Date", skipped)

        # Step 3 — DataFilter (already done by SQL, but defensive).
        if month:
            valid = [r for r in valid if r["Date"].startswith(month)]

        # Step 4 + 5 — Group by Date + aggregate.
        buckets: dict[str, dict[str, float]] = defaultdict(
            lambda: {"sales": 0.0, "orders": 0}
        )
        total_sales = 0.0
        orders = 0
        customers: set[str] = set()
        for r in valid:
            d = r["Date"]
            amt = float(r.get("Total Amount") or 0)
            buckets[d]["sales"] += amt
            buckets[d]["orders"] = int(buckets[d]["orders"]) + 1
            total_sales += amt
            orders += 1
            party = r.get("Party Name")
            if isinstance(party, str) and party:
                customers.add(party)

        series = [
            {"bucket": d, "sales": round(b["sales"], 2), "orders": int(b["orders"])}
            for d, b in sorted(buckets.items(), key=lambda x: x[0])
        ][:366]

        # Step 6 — Monthly Sales Distribution for the pie chart.
        # Always all-time, regardless of `month` filter — the pie's purpose is
        # cross-month comparison. SQL does the aggregation; Python only formats
        # labels and serializes. NULL/non-ISO dates and NULL amounts are
        # filtered at the SQL layer so a malformed row can never poison a
        # bucket.
        monthly_sales_pie = await self._aggregate_monthly_sales_pie(
            registry, dummy_state,
        )

        return {
            "month": month,
            "kpis": {
                "total_sales": round(total_sales, 2),
                "orders":      orders,
                "customers":   len(customers),
            },
            "series": series,
            "monthly_sales_pie": monthly_sales_pie,
        }

    @staticmethod
    async def _aggregate_monthly_sales_pie(
        registry: Any, dummy_state: TurnState,
    ) -> list[dict[str, Any]]:
        """SQL GROUP BY year-month → SUM(Total Amount). Returns chronologically
        sorted [{"month": "Jan 2025", "sales": 120000.0}, ...]."""
        pie_sql = (
            f'SELECT '
            f'  substr("Date", 1, 7) AS ym, '
            f'  SUM("Total Amount") AS sales '
            f'FROM {quoted("sales")} '
            f'WHERE "Date" IS NOT NULL '
            f'  AND "Date" GLOB \'????-??-??\' '
            f'  AND "Total Amount" IS NOT NULL '
            f'GROUP BY ym '
            f'ORDER BY ym ASC'
        )
        _dashboard_log.info("DashboardAgent monthly_sales_pie aggregation start")
        pie_result = await registry.execute(
            "Database",
            {"op": "select", "pin": READ_PIN, "sql": pie_sql, "params": []},
            dummy_state,
        )
        if not pie_result.ok:
            _dashboard_log.warning(
                "DashboardAgent monthly_sales_pie query failed: %s", pie_result.error,
            )
            return []
        pie_rows = list((pie_result.output or {}).get("rows") or [])

        out: list[dict[str, Any]] = []
        for r in pie_rows:
            ym = r.get("ym")
            if not isinstance(ym, str) or len(ym) != 7 or ym[4] != "-":
                continue
            try:
                label = datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
            except ValueError:
                continue
            sales = float(r.get("sales") or 0)
            if sales <= 0:
                continue
            out.append({"month": label, "sales": round(sales, 2)})

        if not out:
            _dashboard_log.info(
                "DashboardAgent monthly_sales_pie empty — no aggregable rows"
            )
        else:
            _dashboard_log.info(
                "DashboardAgent monthly_sales_pie ok rows=%d span=%s..%s",
                len(out), out[0]["month"], out[-1]["month"],
            )
        return out


# ---------------------------------------------------------------------------
# 2.8 DataCleanAgent — NO LLM. Deterministic ingestion pipeline.
# ---------------------------------------------------------------------------

_dataclean_log = logging.getLogger("agentic_ai.agents.dataclean")


# ---------- DataNormalizer --------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y%m%d",
)


def _parse_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date_cls):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


_AMOUNT_STRIP = (",", "₹", "$", "Rs.", "Rs", " ")


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return None
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    for ch in _AMOUNT_STRIP:
        s = s.replace(ch, "")
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    s = str(value).strip()
    return s if s else None


def normalize_row(raw: dict[str, Any], header_index: dict[str, str]) -> dict[str, Any]:
    """Map raw cells to canonical column dict (canonical → typed value).
    Optional missing → None (NULL in DB)."""
    out: dict[str, Any] = {c: None for c in SCHEMA_COLUMNS}
    for raw_key, canonical in header_index.items():
        v = raw.get(raw_key)
        col_type = COLUMN_TYPES[canonical]
        if canonical == "Date":
            out[canonical] = _parse_date(v)
        elif col_type == "REAL":
            out[canonical] = _parse_amount(v)
        else:
            out[canonical] = _parse_text(v)
    return out


# ---------- RowValidator ----------------------------------------------------

def validate_row(normalized: dict[str, Any]) -> str | None:
    """Return None if valid, else a human-readable rejection reason."""
    for req in REQUIRED_COLUMNS:
        v = normalized.get(req)
        if v is None or (isinstance(v, str) and not v):
            return f"required field '{req}' missing or unparseable"
    if not isinstance(normalized.get("Date"), str) or not _ISO_DATE_RE.match(
        normalized["Date"]
    ):
        return "Date is not ISO YYYY-MM-DD"
    amt = normalized.get("Total Amount")
    if not isinstance(amt, (int, float)):
        return "Total Amount is not numeric"
    return None


# ---------- PostValidator ---------------------------------------------------

async def _validate_post_insert(target: str, batch_id: str) -> dict[str, Any]:
    """Run the dashboard-integrity sanity check on the just-inserted batch.

    Raises UploadError on any anomaly so the upload fails LOUD, never silently.
    """
    row = await fetch_one(
        f'SELECT '
        f'  COUNT(*) AS n, '
        f'  MIN("Date") AS min_date, '
        f'  MAX("Date") AS max_date, '
        f'  COUNT(CASE WHEN "Date" IS NULL OR "Date" = \'\' THEN 1 END) AS null_dates, '
        f'  COUNT(CASE WHEN "Total Amount" IS NULL THEN 1 END) AS null_amts '
        f'FROM {quoted(target)} WHERE batch_id = ?',
        (batch_id,),
    )
    if not row or int(row.get("n") or 0) == 0:
        raise UploadError("post-insert validation: 0 rows persisted from this batch")
    n = int(row["n"])
    null_dates = int(row.get("null_dates") or 0)
    null_amts = int(row.get("null_amts") or 0)
    if null_dates:
        raise UploadError(
            f"post-insert validation: {null_dates}/{n} rows have NULL Date "
            f"— dashboard would break"
        )
    if null_amts:
        raise UploadError(
            f"post-insert validation: {null_amts}/{n} rows have NULL Total Amount "
            f"— dashboard aggregation would break"
        )
    min_d = row.get("min_date") or ""
    max_d = row.get("max_date") or ""
    if not (_ISO_DATE_RE.match(str(min_d)) and _ISO_DATE_RE.match(str(max_d))):
        raise UploadError(
            f"post-insert validation: Date range invalid (min={min_d!r}, max={max_d!r})"
        )
    return {"batch_rows": n, "min_date": min_d, "max_date": max_d}


class DataCleanAgent:
    """No LLM. Orchestrates parsing + normalization + insertion + validation."""

    name = "DataCleanAgent"

    async def run(
        self,
        *,
        tmp_path: Path,
        filename: str,
        target: str,
        batch_id: str | None = None,
        dedup_mode: str = "block",
        preview_only: bool = False,
        source: str = "upload",
    ) -> dict[str, Any]:
        """Parse + validate + (optionally) insert.

        Parameters
        ----------
        dedup_mode : 'block' | 'skip' | 'replace' | 'append'
            How to handle rows whose row_hash already exists in the target.
        preview_only : bool
            When True, runs the full pipeline up to classification and
            returns the breakdown WITHOUT inserting. Used by /upload/preview.
        source : str
            Origin tag stamped on every inserted row + the uploads audit
            row — 'upload' for manual file uploads, 'drive' for Google Drive.
        """
        from app.dedup import (
            DEDUP_MODES, classify_batch, delete_rows_with_hashes,
        )
        if target not in ALLOWED_TABLES:
            raise UploadError(f"target must be one of {ALLOWED_TABLES!r}")
        if dedup_mode not in DEDUP_MODES:
            raise UploadError(
                f"dedup_mode must be one of {DEDUP_MODES!r}, got {dedup_mode!r}"
            )
        suffix = tmp_path.suffix.lower()

        # 1. FileParser — auto header detection.
        sheet_name: str | None = None
        if suffix == ".csv":
            header, header_index, row_iter = stream_parse_csv_with_detection(tmp_path)
        elif suffix == ".xlsx":
            (
                header,
                header_index,
                row_iter,
                sheet_name,
            ) = stream_parse_xlsx_with_detection(tmp_path)
        else:
            raise UploadError(f"unsupported file type: {suffix}")

        # 2. HeaderMapper — verify and compute extras.
        _, missing_required, unmatched_extras = map_headers_strict(header)
        if missing_required:
            raise UploadError(
                f"required column(s) without matching header: {missing_required}; "
                f"file headers: {header}"
            )

        # 3 + 4. Normalize + validate, accumulating errors.
        valid_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        rows_seen = 0
        try:
            for raw in row_iter:
                rows_seen += 1
                normalized = normalize_row(raw, header_index)
                reason = validate_row(normalized)
                if reason:
                    errors.append({"row": rows_seen, "reason": reason})
                    continue
                valid_rows.append(normalized)
        finally:
            close = getattr(row_iter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        if not valid_rows:
            raise UploadError(
                f"no valid rows after parsing — {rows_seen} rows seen, "
                f"{len(errors)} rejected. First reason: "
                f"{errors[0]['reason'] if errors else '(no rows in file)'}"
            )

        # 5. Deduplication classification.
        classification = await classify_batch(target, valid_rows, mode=dedup_mode)
        dedup_summary = classification.summary()
        _dataclean_log.info("dedup classification: %s", dedup_summary)

        # 5a. Apply policy: block / skip / replace / append.
        if dedup_mode == "block" and (
            classification.duplicate_count > 0
            or classification.intra_batch_dupe_count > 0
        ):
            # Sample of which incoming rows collided, for the user message.
            sample = []
            for r in valid_rows[:5]:
                if r in classification.new_rows:
                    continue
                sample.append({
                    "date": r.get("Date"),
                    "order_no": r.get("Order No"),
                    "party": r.get("Party Name"),
                    "amount": r.get("Total Amount"),
                })
            raise UploadError(
                "duplicate data detected: "
                f"{classification.duplicate_count} rows already exist in {target!r} "
                f"and {classification.intra_batch_dupe_count} rows are repeated in the "
                f"file itself (intra-batch). "
                f"Re-upload with dedup_mode=skip / replace / append to override. "
                f"Sample of conflicting rows: {sample}"
            )

        # 5b. Preview-only? Return the classification without inserting.
        if preview_only:
            return {
                "preview":          True,
                "filename":         filename,
                "target":           target,
                "dedup_mode":       dedup_mode,
                "errors":           errors,
                "rows_failed":      len(errors),
                "header_row_used":  header,
                "unmatched_headers": unmatched_extras,
                "sheet_name":       sheet_name,
                "dedup":            dedup_summary,
            }

        # 5c. Replace mode: delete the colliding rows first.
        rows_replaced = 0
        if dedup_mode == "replace" and classification.duplicate_hashes:
            rows_replaced = await delete_rows_with_hashes(
                target, classification.duplicate_hashes,
            )

        # 5d. Pick the rows we'll actually insert based on policy.
        if dedup_mode == "skip":
            rows_to_insert = classification.new_rows
            hashes_to_insert = classification.new_row_hashes
        elif dedup_mode == "replace":
            # After delete, we re-insert ALL parsed rows (post-intra-batch-dedup)
            # so the user's authoritative version wins.
            from app.dedup import compute_row_hash
            seen: set[str] = set()
            rows_to_insert = []
            hashes_to_insert = []
            for r in valid_rows:
                h = compute_row_hash(target, r)
                if h in seen:
                    continue
                seen.add(h)
                rows_to_insert.append(r)
                hashes_to_insert.append(h)
        elif dedup_mode == "append":
            from app.dedup import compute_row_hash
            rows_to_insert = valid_rows
            hashes_to_insert = [compute_row_hash(target, r) for r in valid_rows]
        else:
            # block — but only reaches here when there were no duplicates
            rows_to_insert = classification.new_rows
            hashes_to_insert = classification.new_row_hashes

        # 6. Database (restricted).
        if batch_id is None:
            batch_id = str(uuid4())

        if not rows_to_insert:
            # All rows were duplicates and policy was skip — still record the
            # upload audit row, just with zero inserts.
            await record_upload_meta(
                batch_id=batch_id, filename=filename, target=target,
                rows_inserted=0, rows_failed=len(errors), source=source,
                status="active",
                dedup_mode=dedup_mode,
                rows_skipped_duplicate=classification.duplicate_count
                                     + classification.intra_batch_dupe_count,
                rows_replaced=rows_replaced,
            )
            return {
                "batch_id":         batch_id,
                "filename":         filename,
                "target":           target,
                "rows_inserted":    0,
                "rows_failed":      len(errors),
                "rows_skipped_duplicate": classification.duplicate_count
                                       + classification.intra_batch_dupe_count,
                "rows_replaced":    rows_replaced,
                "dedup_mode":       dedup_mode,
                "errors":           errors,
                "summary":          {"total_sales": 0.0, "min_date": "", "max_date": ""},
                "unmatched_headers": unmatched_extras,
                "sheet_name":       sheet_name,
                "header_row_used":  header,
                "validation":       None,
                "dedup":            dedup_summary,
            }

        registry = get_registry()
        dummy_state = TurnState(question="<dataclean>")
        result = await registry.execute(
            "Database",
            {
                "op":           "insert",
                "pin":          INGESTION_PIN,
                "table":        target,
                "rows":         rows_to_insert,
                "batch_id":     batch_id,
                "source":       source,
                "file_name":    filename,
                "row_hashes":   hashes_to_insert,
            },
            dummy_state,
        )
        if not result.ok:
            raise UploadError(f"database insert failed: {result.error}")
        rows_inserted = int((result.output or {}).get("rows_inserted") or 0)

        # 7. PostValidator — fails loud if dashboard would break.
        try:
            validation = await _validate_post_insert(target, batch_id)
        except UploadError:
            # Roll back this batch_id so a partial insert doesn't pollute the DB.
            try:
                async with get_connection() as conn:
                    await conn.execute(
                        f'DELETE FROM {quoted(target)} WHERE batch_id = ?', (batch_id,)
                    )
                    await conn.commit()
            except Exception:
                _dataclean_log.exception("rollback of failed batch %s failed", batch_id)
            raise

        rows_failed = len(errors)
        await record_upload_meta(
            batch_id=batch_id,
            filename=filename,
            target=target,
            rows_inserted=rows_inserted,
            rows_failed=rows_failed,
            source=source,
            status="active",
            min_date=validation.get("min_date"),
            max_date=validation.get("max_date"),
            dedup_mode=dedup_mode,
            rows_skipped_duplicate=classification.duplicate_count
                                 + classification.intra_batch_dupe_count,
            rows_replaced=rows_replaced,
        )

        summary = {
            "total_sales": round(
                sum(float(r.get("Total Amount") or 0) for r in rows_to_insert), 2
            ),
            "min_date": validation["min_date"],
            "max_date": validation["max_date"],
        }

        return {
            "batch_id":         batch_id,
            "filename":         filename,
            "target":           target,
            "rows_inserted":    rows_inserted,
            "rows_failed":      rows_failed,
            "rows_skipped_duplicate": classification.duplicate_count
                                   + classification.intra_batch_dupe_count,
            "rows_replaced":    rows_replaced,
            "dedup_mode":       dedup_mode,
            "errors":           errors,
            "summary":          summary,
            "unmatched_headers": unmatched_extras,
            "sheet_name":       sheet_name,
            "header_row_used":  header,
            "validation":       validation,
            "dedup":            dedup_summary,
        }


# ===========================================================================
# 3. AGENT REGISTRY
# ===========================================================================

# The natural-language question path is handled by the AgenticLoop (the LLM
# orchestrates capabilities directly — there is no sub-agent to pick). The
# remaining named agents are UI-triggered, deterministic, and never go
# through the loop.
ROUTE_ONLY_AGENTS: tuple[str, ...] = (
    "DashboardAgent",
    "DataCleanAgent",
)

ALL_AGENT_NAMES: tuple[str, ...] = (
    "AgenticLoop",
    "DashboardAgent",
    "DataCleanAgent",
)
