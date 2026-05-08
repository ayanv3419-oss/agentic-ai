"""Agent orchestration — coordinator + 7 sub-agents.

This module owns *what* runs: the coordinator picks a sub-agent, the
sub-agent walks a fixed pipeline of tools (declared as `(name, args)`
tuples) and the base class drives that walk.

Sections:
    1.  Coordinator (intent router → dispatcher → loop)
        - intent_router.classify     — chat vs. agentic (deterministic)
        - dispatcher.select_sub_agent — Groq picks ONE analytic sub-agent
        - chat_responder.respond_chat — direct LLM reply, single SSE final
        - loop.run_query_turn        — top-level entry called by /query_stream
    2.  Sub-agent base + the 7 sub-agents
        - QueryAgent      — straightforward lookup
        - AnalyticsAgent  — comparisons / trends (LLM-mode insight)
        - RCAAgent        — root-cause analysis questions
        - ForecastAgent   — projects the metric forward (linear regression)
        - DashboardAgent  — read-only KPI / series builder for /dashboard
        - DataCleanAgent  — deterministic file ingestion pipeline
        - ResponseAgent   — pipeline coda (formatter + persistence)

Cross-module rule: imports go down — `app.tools`, `app.database` — never up.
The HTTP layer (`api.py`) imports `run_query_turn`, `DashboardAgent`,
`DataCleanAgent` from here.
"""
from __future__ import annotations

import logging
import re
from abc import ABC
from collections import defaultdict
from datetime import date as _date_cls, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Sequence, Union
from uuid import uuid4

from app.database import (
    ALLOWED_TABLES,
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
    stream_parse_xlsx_with_detection,
)
from app.tools import (
    CostGuardError,
    EventEmitter,
    GroqMessage,
    INGESTION_PIN,
    READ_PIN,
    ToolCallRecord,
    ToolResult,
    TurnState,
    check_cost,
    check_loop_iteration,
    classify_intent,
    get_groq,
    get_registry,
    parse_strict_json,
)


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
    # but adding `your` here makes the gate explicit).
    r"what(?:\s+is|\s+are|\s+does|'s)\s+(?:a\s+|an\s+|the\s+)?(?!my\b|our\b|your\b)\w+|"
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


def _check_missing_dimension(question_lower: str) -> dict[str, Any] | None:
    """Return a hints dict if the question explicitly references an
    out-of-scope dimension, otherwise None."""
    padded = " " + question_lower + " "
    for label, kws, friendly in _MISSING_DIMENSIONS:
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
    """Quick probe: does the user have any sales/purchase rows on file?

    Used by the intent classifier to bias ambiguous business-flavored
    questions toward agentic mode when data exists. Fails open: any DB
    error is treated as "data exists" so we never silently downgrade a
    real analytics question to chat just because the probe failed.
    """
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


# --- Conversational continuity (single-admin app — module-level state) ----

_LAST_KIND: QueryKind | None = None


def _get_last_kind() -> QueryKind | None:
    return _LAST_KIND


def _set_last_kind(kind: QueryKind) -> None:
    global _LAST_KIND
    _LAST_KIND = kind


def _reset_last_kind() -> None:
    """Test hook so regression tests can pin a known previous_kind."""
    global _LAST_KIND
    _LAST_KIND = None


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
# 1.2 Dispatcher — Groq picks an analytic sub-agent (strict JSON, no fallback).
# ---------------------------------------------------------------------------

_dispatch_log = logging.getLogger("agentic_ai.coordinator.dispatcher")

DISPATCH_SYSTEM_PROMPT = """You are the Agentic AI Coordinator. Your only job is to
select EXACTLY ONE sub-agent to handle the user's question.

Available sub-agents:
  - QueryAgent      — straightforward lookup (totals, counts, simple filters).
  - AnalyticsAgent  — comparisons, trends, growth, period-over-period.
  - RCAAgent        — "why did X drop", root-cause-analysis questions.
  - ForecastAgent   — predictions / projections about the future.

OUTPUT FORMAT — STRICT JSON, NO PROSE, NO MARKDOWN:
  {"sub_agent": "<one of the four names above>", "reason": "<one short sentence>"}

Hard rules:
  1. Choose EXACTLY one sub-agent name from the list above.
  2. Never pick DashboardAgent, DataCleanAgent, or ResponseAgent — those have
     dedicated entrypoints and are not user-query-callable.
  3. Never invent a sub-agent name.
  4. Never produce any text outside the JSON object.
"""


async def select_sub_agent(question: str) -> tuple[str, str, dict]:
    """Returns (sub_agent_name, reason, llm_metrics).

    Raises ValueError on any dispatch failure (LLM error, invalid JSON,
    invalid sub-agent name). The caller emits the structured error event.
    """
    if not question or not question.strip():
        raise ValueError("empty question")

    groq = get_groq()
    resp = await groq.complete(
        [
            GroqMessage(role="system", content=DISPATCH_SYSTEM_PROMPT),
            GroqMessage(role="user", content=question.strip()),
        ],
        temperature=0.0,
        max_tokens=200,
        force_json=True,
    )
    metrics = {"tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out}
    if resp.error:
        raise ValueError(f"LLM dispatch failed: {resp.error_kind}: {resp.error}")
    try:
        parsed = parse_strict_json(resp.content)
    except ValueError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}") from e
    name = str(parsed.get("sub_agent") or "").strip()
    reason = str(parsed.get("reason") or "")
    if name not in ANALYTIC_AGENTS:
        raise ValueError(
            f"LLM picked unknown sub-agent {name!r}; allowed: {list(ANALYTIC_AGENTS)}"
        )
    return name, reason, metrics


# ---------------------------------------------------------------------------
# 1.3 Chat responder — direct LLM reply for chat-mode turns.
# ---------------------------------------------------------------------------

_chat_log = logging.getLogger("agentic_ai.coordinator.chat")

_CHAT_SYSTEM_PROMPT = """You are Agentic AI — a friendly assistant for a small-business analytics product.

Reply briefly and warmly to greetings, small talk, and general questions.

If the user asks about their sales, purchases, dashboard, customers, orders,
or any other business data, gently say "Ask me a specific data question and
I'll run it through the analytics pipeline." Do NOT invent numbers or
fabricate any business data.

Keep responses to 1–3 short sentences unless explicitly asked for more.
"""


# Used when the user asks a general business / definitions / how-to
# question that isn't a data lookup. The LLM is allowed to answer
# substantively with its general knowledge — but is NEVER allowed to
# invent numbers about the user's specific business.
_GENERAL_KNOWLEDGE_PROMPT = """You are Agentic AI — an analytics assistant
for a small-business owner. The user has asked a general business
question (definition, advice, or how-to) that is NOT a data lookup.

Answer concisely (3–5 sentences) using your general knowledge. Be
practical and grounded. Use plain language; assume the reader is a
busy shopkeeper, not an MBA student.

CRITICAL RULES:
  • Do NOT invent or estimate any numbers about the user's specific
    business. You do not have access to their data here.
  • Do NOT pretend to compute results for them.
  • If the answer would benefit from looking at their actual data,
    end with one short suggestion such as: "If you'd like, I can also
    analyse your sales for X."
  • Stay focused on the question asked — no marketing pitch.
"""


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


# Deterministic intent → sub-agent mapping. Used when the classifier hits a
# rule with confidence ≥ DETERMINISTIC_CONFIDENCE; below that threshold the
# LLM dispatcher takes over.
INTENT_TO_AGENT: dict[str, str] = {
    "sales_summary":        "QueryAgent",
    "purchase_analysis":    "QueryAgent",
    "product_performance":  "AnalyticsAgent",
    "trend_analysis":       "AnalyticsAgent",
    "comparison":           "AnalyticsAgent",
    "anomaly_detection":    "AnalyticsAgent",
    "root_cause_analysis":  "RCAAgent",
    "forecasting":          "ForecastAgent",
}

# An LLM-picked sub-agent name maps back to the authoritative intent type
# so downstream tools always see a populated state.intent.type.
AGENT_TO_INTENT: dict[str, str] = {
    "QueryAgent":     "sales_summary",
    "AnalyticsAgent": "trend_analysis",
    "RCAAgent":       "root_cause_analysis",
    "ForecastAgent":  "forecasting",
}

DETERMINISTIC_CONFIDENCE = 0.7


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

    cache_key = cache_key_for(state.question)
    state = state.apply(cache_key=cache_key)

    # ---- 1. Cache lookup --------------------------------------------------
    cached = get_cached(cache_key)
    if cached:
        cached_mode = cached.get("mode", "agentic")
        await emit.emit("cache.hit", {
            "cache_key": cache_key,
            "stored_at": cached.get("stored_at"),
            "mode":      cached_mode,
        })
        await emit.emit("final", {
            "answer":     cached.get("final_answer", ""),
            "chart":      cached.get("chart") or cached.get("aggregates"),
            "from_cache": True,
            "mode":       cached_mode,
        })
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

    # ---- 2. Top-level query-kind guard (deterministic, zero LLM cost) ----
    # Splits the request 4 ways with conversational + has-data context:
    #   data_query        → continue to intent classifier + sub-agent
    #   missing_data      → graceful "we don't track that" template (no SQL)
    #   general_knowledge → LLM with knowledge prompt (no SQL)
    #   chat              → LLM with greeter prompt (no SQL)
    has_data = await _has_any_uploaded_data()
    previous_kind = _get_last_kind()
    kind, kind_conf, kind_hints = classify_query_kind(
        state.question,
        has_data=has_data,
        previous_kind=previous_kind,
    )
    _set_last_kind(kind)
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
        return await respond_chat(
            state, emit,
            f"general_knowledge: {kind_hints.get('matched')}",
            system_prompt=_GENERAL_KNOWLEDGE_PROMPT,
            mode="general_knowledge",
        )

    if kind == "chat":
        return await respond_chat(
            state, emit,
            f"chat: {kind_hints.get('matched')}",
            system_prompt=_CHAT_SYSTEM_PROMPT,
            mode="chat",
        )

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

    # ---- 4. Intent classification + sub-agent selection ------------------
    intent_type, intent_conf, intent_hints = classify_intent(state.question)
    _loop_log.info(
        "intent: type=%s confidence=%.2f hints=%s",
        intent_type, intent_conf, intent_hints,
    )
    await emit.emit("intent.classified", {
        "type":       intent_type,
        "confidence": intent_conf,
        "hints":      intent_hints,
    })

    metrics: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0}
    routing_strategy: str
    sa_reason: str

    if intent_conf >= DETERMINISTIC_CONFIDENCE:
        # High-confidence — deterministic agent pick, no LLM call.
        sub_agent_name = INTENT_TO_AGENT.get(intent_type, "QueryAgent")
        routing_strategy = "deterministic"
        sa_reason = (
            f"intent={intent_type} conf={intent_conf:.2f} "
            f"matched={intent_hints.get('matched')!r}"
        )
    else:
        # Low-confidence — fall back to the LLM dispatcher and map its choice
        # back to a canonical intent type so downstream tools still see one.
        try:
            sub_agent_name, sa_reason, dispatch_metrics = await select_sub_agent(
                state.question
            )
        except ValueError as e:
            await emit.emit("agent.result", {"error": str(e), "kind": "dispatch_error"})
            await emit.emit("turn.end", {
                "turn_id": state.turn_id, "errors": [str(e)], "mode": "agentic",
            })
            return state.append_error(str(e))
        metrics = dispatch_metrics
        routing_strategy = "llm_fallback"
        # Override the low-confidence intent with the LLM's pick.
        intent_type = AGENT_TO_INTENT.get(sub_agent_name, intent_type)

    # Seed state.intent so the pipeline tools see a fully-populated dict
    # before IntentAnalyzer runs (it then merges in metric / top_n / etc).
    state = state.apply(
        intent={
            "type":       intent_type,
            "confidence": intent_conf,
            "hints":      intent_hints,
        },
        sub_agent=sub_agent_name,
        tokens_in=state.tokens_in + int(metrics.get("tokens_in", 0)),
        tokens_out=state.tokens_out + int(metrics.get("tokens_out", 0)),
    )
    await emit.emit("sub_agent.dispatched", {
        "sub_agent":  sub_agent_name,
        "reason":     sa_reason,
        "intent":     intent_type,
        "confidence": round(intent_conf, 2),
        "strategy":   routing_strategy,
    })

    agent = get_analytic_agent(sub_agent_name)
    try:
        state = await agent.run(state, emit)
    except Exception as e:
        _loop_log.exception("sub-agent crashed")
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

    # Success — emit final.
    await emit.emit("final", {
        "answer":     state.final_answer or "",
        "chart":      state.chart_data,
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
# 2. SUB-AGENTS
# ===========================================================================

_agents_log = logging.getLogger("agentic_ai.agents")

# Each pipeline step is either:
#   ("ToolName", {"arg": value})
#   ("ToolName", lambda state: {"arg": ...})  # dynamic args from current state
PipelineStep = tuple[str, Union[dict, Callable[[TurnState], dict]]]


# ---------------------------------------------------------------------------
# 2.1 Sub-agent base — fixed deterministic tool sequence per agent.
# ---------------------------------------------------------------------------

class SubAgent(ABC):
    """Walks a fixed tool pipeline; emits SSE events; halts on first error."""

    name: str = ""
    pipeline: Sequence[PipelineStep] = ()

    async def run(
        self,
        state: TurnState,
        emit: EventEmitter,
    ) -> TurnState:
        registry = get_registry()
        for tool_name, args_spec in self.pipeline:
            args = args_spec(state) if callable(args_spec) else dict(args_spec)
            iteration = state.iteration + 1
            state = state.apply(iteration=iteration)
            await emit.emit("tool.call", {
                "name": tool_name, "args": args, "iteration": iteration,
            })
            result: ToolResult = await registry.execute(tool_name, args, state)
            await emit.emit("tool.result", {
                "name": tool_name,
                "ok": result.ok,
                "output": result.output,
                "error": result.error,
                "duration_ms": round(result.duration_ms, 2),
            })
            record = ToolCallRecord(
                name=tool_name,
                args=args,
                output=result.output,
                ok=result.ok,
                error=result.error,
                duration_ms=result.duration_ms,
                iteration=iteration,
            )
            state = state.append_tool_call(record)
            if not result.ok:
                state = state.append_error(f"{tool_name}: {result.error}")
                # Halt the pipeline on first failure — strict mode.
                return state
            if result.state_updates:
                state = state.apply(**result.state_updates)
            if result.delta_metrics:
                state = state.apply(
                    tokens_in=state.tokens_in + int(result.delta_metrics.get("tokens_in", 0)),
                    tokens_out=state.tokens_out + int(result.delta_metrics.get("tokens_out", 0)),
                )
        return state


# ---------------------------------------------------------------------------
# 2.2 QueryAgent — straightforward lookup queries (totals, counts, distincts).
# ---------------------------------------------------------------------------

class QueryAgent(SubAgent):
    name = "QueryAgent"
    pipeline = (
        ("RouteClassifier",   {}),
        ("IntentAnalyzer",    {}),
        ("TimeKPI",           {}),
        ("EntityResolver",    {}),
        ("SchemaRetriever",   {}),
        ("SqlPlanner",        {}),
        ("SqlWriter",         {}),
        ("SqlValidator",      {}),
        ("SqlExecutor",       {}),
        ("ResultAggregator",  {}),
        ("InsightEngine",     {"mode": "rule"}),
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )


# ---------------------------------------------------------------------------
# 2.3 AnalyticsAgent — trend / comparison questions; LLM-narrated insight.
# ---------------------------------------------------------------------------

class AnalyticsAgent(SubAgent):
    name = "AnalyticsAgent"
    pipeline = (
        ("RouteClassifier",   {}),
        ("IntentAnalyzer",    {}),
        ("TimeKPI",           {}),
        ("EntityResolver",    {}),
        ("SchemaRetriever",   {}),
        ("SqlPlanner",        {}),
        ("SqlWriter",         {}),
        ("SqlValidator",      {}),
        ("SqlExecutor",       {}),
        ("ResultAggregator",  {}),
        ("InsightEngine",     {"mode": "llm"}),
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )


# ---------------------------------------------------------------------------
# 2.4 RCAAgent — root-cause analysis questions.
# ---------------------------------------------------------------------------

class RCAAgent(SubAgent):
    name = "RCAAgent"
    pipeline = (
        ("RouteClassifier",   {}),
        ("IntentAnalyzer",    {}),
        ("TimeKPI",           {}),
        ("EntityResolver",    {}),
        ("SchemaRetriever",   {}),
        ("SqlPlanner",        {}),
        ("SqlWriter",         {}),
        ("SqlValidator",      {}),
        ("SqlExecutor",       {}),
        ("ResultAggregator",  {}),
        ("InsightEngine",     {"mode": "llm"}),
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )


# ---------------------------------------------------------------------------
# 2.5 ForecastAgent — projects metric forward via least-squares regression.
# ---------------------------------------------------------------------------

_forecast_log = logging.getLogger("agentic_ai.agents.forecast")
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


_FORECAST_PRE: tuple[PipelineStep, ...] = (
    ("RouteClassifier",   {}),
    ("IntentAnalyzer",    {}),
    ("TimeKPI",           {}),
    ("EntityResolver",    {}),
    ("SchemaRetriever",   {}),
    ("SqlPlanner",        {}),
    ("SqlWriter",         {}),
    ("SqlValidator",      {}),
    ("SqlExecutor",       {}),
    ("ResultAggregator",  {}),
)
_FORECAST_POST: tuple[PipelineStep, ...] = (
    ("InsightEngine",     {"mode": "llm"}),
    ("ResponseFormatter", {}),
    ("ResponseStored",    {}),
)


class ForecastAgent(SubAgent):
    name = "ForecastAgent"
    # Informational — the actual run() splits at the seam to inject the
    # projector step between PRE and POST.
    pipeline = _FORECAST_PRE + _FORECAST_POST

    async def run(self, state: TurnState, emit: EventEmitter) -> TurnState:
        state = await self._run_steps(state, emit, _FORECAST_PRE)
        if state.errors:
            return state
        state = self._inject_forecast(state)
        await emit.emit("tool.result", {
            "name": "ForecastProjector",
            "ok": True,
            "output": {"horizon_days": _FORECAST_HORIZON_DAYS},
            "error": None,
            "duration_ms": 0.0,
        })
        return await self._run_steps(state, emit, _FORECAST_POST)

    @staticmethod
    def _inject_forecast(state: TurnState) -> TurnState:
        aggregates = dict(state.aggregates or {})
        series = list(aggregates.get("series") or [])
        projected = _project_forecast(series)
        aggregates["series"] = projected
        aggregates["forecast_horizon_days"] = _FORECAST_HORIZON_DAYS
        return state.apply(aggregates=aggregates, chart_data=aggregates)

    async def _run_steps(
        self, state: TurnState, emit: EventEmitter, steps: tuple[PipelineStep, ...]
    ) -> TurnState:
        registry = get_registry()
        for tool_name, args_spec in steps:
            args = args_spec(state) if callable(args_spec) else dict(args_spec)
            iteration = state.iteration + 1
            state = state.apply(iteration=iteration)
            await emit.emit("tool.call", {
                "name": tool_name, "args": args, "iteration": iteration,
            })
            result: ToolResult = await registry.execute(tool_name, args, state)
            await emit.emit("tool.result", {
                "name": tool_name, "ok": result.ok, "output": result.output,
                "error": result.error, "duration_ms": round(result.duration_ms, 2),
            })
            state = state.append_tool_call(ToolCallRecord(
                name=tool_name, args=args, output=result.output,
                ok=result.ok, error=result.error,
                duration_ms=result.duration_ms, iteration=iteration,
            ))
            if not result.ok:
                state = state.append_error(f"{tool_name}: {result.error}")
                return state
            if result.state_updates:
                state = state.apply(**result.state_updates)
            if result.delta_metrics:
                state = state.apply(
                    tokens_in=state.tokens_in + int(result.delta_metrics.get("tokens_in", 0)),
                    tokens_out=state.tokens_out + int(result.delta_metrics.get("tokens_out", 0)),
                )
        return state


# ---------------------------------------------------------------------------
# 2.6 ResponseAgent — pipeline coda (formatter + persistence).
# ---------------------------------------------------------------------------

class ResponseAgent(SubAgent):
    """Pipeline coda. The analytic agents already include ResponseFormatter +
    ResponseStored as their last two steps; this exists for catalog
    completeness so the response phase has one named place."""
    name = "ResponseAgent"
    pipeline = (
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )


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

    async def run(self, *, month: str | None = None) -> dict[str, Any]:
        # Step 1 — DatabaseReader (read-only via Database tool).
        if month is None:
            sql = (
                f'SELECT "Date", "Total Amount", "Party Name" '
                f'FROM {quoted("sales")}'
            )
            params: list[Any] = []
        else:
            if not (len(month) == 7 and month[4] == "-"):
                raise ValueError(f"invalid month format: {month!r}")
            sql = (
                f'SELECT "Date", "Total Amount", "Party Name" '
                f'FROM {quoted("sales")} '
                f'WHERE "Date" LIKE ?'
            )
            params = [f"{month}-%"]

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
        monthly_sales_pie = await self._aggregate_monthly_sales_pie(registry, dummy_state)

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
        sorted [{"month": "Jan 2025", "sales": 120000.0}, ...]. Empty list when
        there are no aggregable rows."""
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
    ) -> dict[str, Any]:
        if target not in ALLOWED_TABLES:
            raise UploadError(f"target must be one of {ALLOWED_TABLES!r}")
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

        # 5. Database (restricted).
        if batch_id is None:
            batch_id = str(uuid4())
        registry = get_registry()
        dummy_state = TurnState(question="<dataclean>")
        result = await registry.execute(
            "Database",
            {
                "op":        "insert",
                "pin":       INGESTION_PIN,
                "table":     target,
                "rows":      valid_rows,
                "batch_id":  batch_id,
                "source":    "upload",
                "file_name": filename,
            },
            dummy_state,
        )
        if not result.ok:
            raise UploadError(f"database insert failed: {result.error}")
        rows_inserted = int((result.output or {}).get("rows_inserted") or 0)

        # 6. PostValidator — fails loud if dashboard would break.
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
            source="upload",
            status="active",
            min_date=validation.get("min_date"),
            max_date=validation.get("max_date"),
        )

        summary = {
            "total_sales": round(
                sum(float(r.get("Total Amount") or 0) for r in valid_rows), 2
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
            "errors":           errors,
            "summary":          summary,
            "unmatched_headers": unmatched_extras,
            "sheet_name":       sheet_name,
            "header_row_used":  header,
            "validation":       validation,
        }


# ===========================================================================
# 3. AGENT REGISTRY (after class definitions so dispatcher can validate)
# ===========================================================================

# Coordinator-callable analytic sub-agents (LLM picks one of these).
ANALYTIC_AGENTS: dict[str, type] = {
    "QueryAgent":     QueryAgent,
    "AnalyticsAgent": AnalyticsAgent,
    "RCAAgent":       RCAAgent,
    "ForecastAgent":  ForecastAgent,
}

# Sub-agents that the LLM may NOT pick — they have dedicated routes.
ROUTE_ONLY_AGENTS: tuple[str, ...] = (
    "DashboardAgent",
    "DataCleanAgent",
)

# Auto-coda after every analytic agent (informational; the analytic agents
# already include ResponseFormatter + ResponseStored in their pipeline).
CODA_AGENT = "ResponseAgent"

ALL_AGENT_NAMES: tuple[str, ...] = (
    "QueryAgent",
    "AnalyticsAgent",
    "RCAAgent",
    "ForecastAgent",
    "DashboardAgent",
    "DataCleanAgent",
    "ResponseAgent",
)


def get_analytic_agent(name: str):
    cls = ANALYTIC_AGENTS.get(name)
    if cls is None:
        raise KeyError(f"Unknown analytic sub-agent: {name!r}")
    return cls()
