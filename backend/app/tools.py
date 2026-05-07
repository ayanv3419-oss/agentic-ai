"""Tool execution layer + runtime substrate.

This module groups everything that the agent pipelines (`agents.py`) and the
HTTP layer (`api.py`) treat as runtime infrastructure — so any reader can
land here and see how a tool runs, end-to-end, without crossing folder
boundaries.

Sections:
    1.  TurnState + ToolCallRecord       — frozen pydantic models
    2.  SSE EventEmitter + helpers       — pushes named events to the queue
    3.  CostGuard                        — per-turn iteration / USD / scan limits
    4.  Groq LLM client                  — async, never-raise, per-request scoping
    5.  Tool base + ToolResult           — ABC every tool inherits from
    6.  Tool implementations             — exactly 14 tools, in registration order:
            Database, RouteClassifier, IntentAnalyzer, TimeKPI,
            EntityResolver, SchemaRetriever, SqlPlanner, SqlWriter,
            SqlValidator, SqlExecutor, ResultAggregator, InsightEngine,
            ResponseFormatter, ResponseStored
    7.  Tool registry                    — bootstrap + `get_registry()`

Cross-module rule: this module imports from `app.database` only. `agents.py`
and `api.py` import from here; nothing here imports from them.
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

from app.database import (
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
# 2. SSE EVENT EMITTER
# ===========================================================================

_COMMENT_MARKER = "__comment__"


def format_sse(event: str, data: Any) -> str:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n"


class EventEmitter:
    """Tiny asyncio.Queue-backed event emitter feeding the SSE stream.

    Coordinator pushes named events; the FastAPI route drains them as
    `text/event-stream`. A separate marker is used for SSE comment lines so
    heartbeats don't collide with named events.
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def emit(self, event: str, data: Any = None) -> None:
        if self._closed:
            return
        await self.queue.put((event, data if data is not None else {}))

    async def comment(self, text: str = "ping") -> None:
        if self._closed:
            return
        await self.queue.put((_COMMENT_MARKER, text))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.queue.put(self._SENTINEL)

    async def stream(self) -> AsyncIterator[str]:
        while True:
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            if item is self._SENTINEL:
                break
            try:
                event, data = item
            except Exception:
                continue
            try:
                if event == _COMMENT_MARKER:
                    yield format_comment(str(data))
                else:
                    yield format_sse(event, data)
            except Exception:
                yield format_comment("serialization-error")


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
# 4. GROQ CLIENT — async, never-raise, per-request scoping via contextvars.
# ===========================================================================

_groq_log = logging.getLogger("agentic_ai.groq")


class GroqMessage(BaseModel):
    role: str
    content: str


class GroqResponse(BaseModel):
    content: str
    tokens_in: int
    tokens_out: int
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None  # "auth" | "upstream" | "network" | "parse" | "unknown"


class GroqStreamChunk(BaseModel):
    delta: str
    finish_reason: str | None = None
    error: str | None = None
    error_kind: str | None = None


def _err_response(msg: str, kind: str) -> GroqResponse:
    return GroqResponse(content="", tokens_in=0, tokens_out=0, error=msg, error_kind=kind)


def _err_chunk(msg: str, kind: str) -> GroqStreamChunk:
    return GroqStreamChunk(delta="", finish_reason="error", error=msg, error_kind=kind)


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_model
        self.base_url = settings.groq_base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            _groq_log.warning("groq client close failed", exc_info=True)

    def _headers_or_error(self) -> tuple[dict[str, str] | None, GroqResponse | None]:
        if not self.api_key:
            return None, _err_response("GROQ_API_KEY not configured", "auth")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }, None

    def _payload(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float,
        max_tokens: int,
        force_json: bool,
        stream: bool,
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if force_json:
            p["response_format"] = {"type": "json_object"}
        return p

    async def complete(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        force_json: bool = True,
    ) -> GroqResponse:
        try:
            return await self._complete_inner(messages, temperature, max_tokens, force_json)
        except Exception as e:
            _groq_log.exception("groq.complete unexpected failure")
            return _err_response(f"{type(e).__name__}: {e}", "unknown")

    async def _complete_inner(
        self,
        messages: list[GroqMessage],
        temperature: float,
        max_tokens: int,
        force_json: bool,
    ) -> GroqResponse:
        headers, err = self._headers_or_error()
        if err is not None:
            return err
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens,
                                force_json=force_json, stream=False)
        url = f"{self.base_url}/chat/completions"
        last_status: int | None = None
        last_body = ""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                last_err = e
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code in (401, 403):
                return _err_response(f"Groq auth rejected: HTTP {resp.status_code}", "auth")
            if resp.status_code >= 400:
                last_status = resp.status_code
                try:
                    last_body = (resp.text or "")[:300]
                except Exception:
                    last_body = ""
                last_err = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                if resp.status_code >= 500 or resp.status_code == 429:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                return _err_response(
                    f"Groq error: HTTP {resp.status_code} {last_body}", "upstream"
                )
            try:
                data = resp.json()
                choice = data["choices"][0]
                usage = data.get("usage", {}) or {}
                return GroqResponse(
                    content=choice["message"].get("content") or "",
                    tokens_in=int(usage.get("prompt_tokens", 0)),
                    tokens_out=int(usage.get("completion_tokens", 0)),
                    finish_reason=choice.get("finish_reason"),
                )
            except (KeyError, IndexError, ValueError, TypeError) as e:
                return _err_response(f"Malformed Groq response: {e}", "parse")
        if last_status is not None:
            return _err_response(
                f"Groq retries exhausted (last HTTP {last_status}): {last_body}", "upstream"
            )
        return _err_response(f"Groq retries exhausted: {last_err}", "network")

    async def complete_stream(
        self,
        messages: list[GroqMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        force_json: bool = True,
    ) -> AsyncIterator[GroqStreamChunk]:
        headers, err = self._headers_or_error()
        if err is not None:
            yield _err_chunk(err.error or "auth error", err.error_kind or "auth")
            return
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens,
                                force_json=force_json, stream=True)
        url = f"{self.base_url}/chat/completions"
        try:
            async with self._client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    try:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    except Exception:
                        body = ""
                    kind = "auth" if resp.status_code in (401, 403) else "upstream"
                    yield _err_chunk(f"HTTP {resp.status_code}: {body}", kind)
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        choice = chunk["choices"][0]
                        delta = (choice.get("delta") or {}).get("content") or ""
                        finish = choice.get("finish_reason")
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        _groq_log.debug("dropping malformed stream chunk: %r", raw[:120])
                        continue
                    if delta or finish:
                        yield GroqStreamChunk(delta=delta, finish_reason=finish)
        except httpx.RequestError as e:
            yield _err_chunk(f"Network error: {e}", "network")
        except Exception as e:
            _groq_log.exception("groq.complete_stream unexpected failure")
            yield _err_chunk(f"{type(e).__name__}: {e}", "unknown")


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_strict_json(content: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output, tolerant of fences and prose."""
    if not content or not content.strip():
        raise ValueError("Empty LLM output")
    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse JSON: {e}") from e
    raise ValueError("LLM output contained no parseable JSON")


_groq_singleton: GroqClient | None = None
_request_groq: contextvars.ContextVar[GroqClient | None] = contextvars.ContextVar(
    "request_groq", default=None
)


def set_request_groq(client: GroqClient | None) -> contextvars.Token:
    return _request_groq.set(client)


def reset_request_groq(token: contextvars.Token) -> None:
    try:
        _request_groq.reset(token)
    except Exception:
        _groq_log.warning("reset_request_groq failed", exc_info=True)


def get_groq() -> GroqClient:
    cur = _request_groq.get()
    if cur is not None:
        return cur
    global _groq_singleton
    if _groq_singleton is None:
        _groq_singleton = GroqClient()
    return _groq_singleton


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
        "highest revenue brand", "highest selling product",
        "best customer", "top customer", "biggest customer",
        "best vendor", "top vendor", "biggest vendor",
        "best buyer", "top buyer", "biggest buyer",
        "best parties", "top parties", "biggest parties",
        "highest sales by", "lowest sales by",
        "by customer", "by party", "by vendor", "by supplier",
        "by product", "by item", "by brand",
        "which product", "which item", "which brand", "which shoe",
        "what product", "what item", "what brand",
        "top n ", "rank by ",
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


def classify_intent(question: str) -> tuple[str, float, dict[str, Any]]:
    """Deterministic intent classifier — returns (type, confidence, hints).

    Confidence is a float in [0.0, 0.99]. Treat ≥ 0.7 as high-confidence
    (use deterministic routing); below that, callers may invoke a
    semantic fallback.
    """
    if not question or not question.strip():
        return "sales_summary", 0.0, {"matched": None, "reason": "empty"}

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
        "against the synonyms memory store. Sets state.entities."
    )
    args_model = EntityResolverArgs
    independent = True

    async def run(self, state: TurnState, args: EntityResolverArgs) -> ToolResult:
        if not state.question:
            return ToolResult(ok=False, error="empty question")
        entities = resolve_entities(state.question)
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
        # Forecast plan forces daily granularity (ForecastAgent's projector
        # only handles ISO YYYY-MM-DD buckets); reflect that in state so
        # ResultAggregator + chart payload stay consistent.
        if intent_type == "forecasting":
            state_updates["granularity"] = "daily"
        return ToolResult(ok=True, output=plan, state_updates=state_updates)

    # ---- planners ---------------------------------------------------------

    @staticmethod
    def _entity_filters(state: TurnState) -> list[dict]:
        out: list[dict] = []
        for ent in state.entities or []:
            canonical = ent.get("canonical")
            if canonical:
                out.append({
                    "col": "Party Name",
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
        # ForecastAgent's projector expects daily ISO buckets to extrapolate;
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

        where = self._date_window_filters(state) + self._entity_filters(state)

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
            "table":           table,
            "select":          select,
            "where":           where,
            "group_by":        [group_col],
            "order_by":        [{"col": "sales", "dir": "DESC"}],
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
    "forecast",     # daily series ready for ForecastAgent's projector
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
# 7. TOOL REGISTRY
# ===========================================================================

# Authoritative list of tool names — must equal the registered set at boot.
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
        try:
            tool = self.get(name)
        except KeyError as e:
            return ToolResult(ok=False, error=f"Unknown tool: {e}")
        return await tool.execute(state, args)


_registry: ToolRegistry | None = None


def _bootstrap(registry: ToolRegistry) -> None:
    """Register all 14 tools. Order matters only for determinism."""
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
