"""
sqlWriter - given the question + schema + resolved time window + entities,
produce a single-statement SELECT for the local Qwen 3 model to validate
and execute downstream.

Output contract (JSON):
  { "sql": "SELECT ...", "rationale": "one short sentence" }
"""
from __future__ import annotations

import re
from typing import Any

from app.coordinator.llm import LLMClient, parse_strict_json
from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.schema_mapping import MetricSqlBuilder, ResolvedSchema


_SYSTEM = """You are sqlWriter, a sub-agent of the Coordinator.

Your single job: produce ONE valid SQLite SELECT statement that answers
the user's question against the database described in the schema. You
NEVER execute SQL. You NEVER produce DDL, INSERT, UPDATE or DELETE.

Rules:
1. Use ONLY tables/columns that appear in the supplied schema. Never
   invent column names. If a column is not listed in the schema, it
   does not exist.
2. Always quote identifiers that contain spaces or special characters
   with double quotes (e.g. "Total Amount", "Party Name").
3. TIME FILTERING: apply the supplied time window (start_iso / end_iso)
   using the date column shown in the SCHEMA for that table. Do NOT
   assume it is called "Date". Look for the date/transaction-date column
   in the schema listing and use its exact name.
   Example: if the schema shows "Transaction Date":TEXT, use
   WHERE "Transaction Date" BETWEEN '<start_iso>' AND '<end_iso>'.
4. ENTITY FILTERING: only add WHERE filters for entities that are
   explicit restrictions in the question (e.g. "show me Adidas sales").
   If the question asks WHICH/WHAT (e.g. "which brand performs best?"),
   do NOT filter — GROUP BY that dimension instead and return all values.
   When you do filter:
   - Multiple values of the SAME kind → use IN (...) — never AND for
     the same column (a row cannot match two values simultaneously).
     Example: brand=Adidas, brand=Nike → WHERE brand_col IN ('Adidas','Nike')
   - Different kinds → combine with AND.
     Example: brand=Adidas, category=Shoes → WHERE brand_col='Adidas' AND category_col='Shoes'
   Use the EXACT value string from the entity — do not approximate.
   If entities look irrelevant to the core question, omit the filter.
5. Prefer SUM / COUNT / GROUP BY for aggregate questions.
6. RANKING / EXTREMUM QUESTIONS - NEVER use LIMIT 1.
   This covers BOTH:
     - "which brand/category/product sold most/best/top" (ranking)
     - "lowest / highest / best / worst <bucket>" where bucket is a day,
       week, month, store, brand, etc. (extremum on a dimension)
   Rules for both:
     - Use GROUP BY <dimension> + ORDER BY <metric> ASC (for lowest/worst)
       or DESC (for highest/best).
     - Return AT LEAST 10 rows (LIMIT 10-15). The downstream layer reads
       the first row as the answer and uses the rest as chart context.
     - LIMIT 1 produces a single-row result which makes the chart
       degenerate to a value card with no comparison — the user always
       wants to SEE the extremum in context, not as a bare number.
   Examples:
     "lowest revenue day"      → GROUP BY DATE(date_col), ORDER BY sum ASC LIMIT 10
     "highest sales month"     → GROUP BY strftime('%Y-%m',...), ORDER BY sum DESC LIMIT 12
     "best performing brand"   → GROUP BY brand, ORDER BY sum DESC LIMIT 15
7. REVENUE / METRIC COLUMNS: use the EXACT column name shown in the schema's
   METRIC DEFINITIONS block. NEVER hardcode column names — the schema layer
   maps revenue/cost/quantity to whatever the workbook uses.
8. NEVER wrap the SQL in markdown fences in the final JSON.
9. DATE RANGE AWARENESS: if the supplied time window would plausibly cover
   a period outside a table's data range, write the SQL without a date
   filter (or with a broader range) and let the results speak. For a
   "growth" or "trend" question, use the full available date range and
   compute period-over-period inside the SQL using GROUP BY strftime().
10. METRIC DEFINITIONS ARE MANDATORY. If the schema (or the MANDATORY
   FORMULAS block) defines a metric the question asks about — margin,
   profit, revenue, units sold, cost of goods — you MUST build your SQL
   from that exact formula: same JOIN, same SUM(...) expressions, same
   column names. Copy it verbatim. NEVER average per-row ratios
   (AVG(margin)) — always compute on SUM totals exactly as written.
   NEVER substitute a formula of your own.
11. TREND CHART RULE: When the user prompt shows a Granularity hint
   (month / week / day / year) AND the time window spans multiple periods,
   you MUST GROUP BY that granularity — NEVER write a bare SUM returning
   one row. One row = no chart visible to the user. Use:
     strftime('%Y-%m', <date_col>) AS month   → monthly
     strftime('%Y-W%W', <date_col>) AS week    → weekly
     DATE(<date_col>) AS day                   → daily
   Always ORDER BY the bucket column ASC. SHAPE (column / table names
   come from THIS turn's schema + time window — do NOT copy the
   identifiers below verbatim, they are placeholders):
     SELECT strftime('%Y-%m', "<date_col>") AS month,
            SUM("<metric_col>") AS total
     FROM "<sales_table>"
     WHERE "<date_col>" BETWEEN '<start_iso>' AND '<end_iso>'
     GROUP BY month ORDER BY month ASC
   Replace every <…> with the actual identifier from the schema and the
   actual ISO date from the supplied time window. Never paste a literal
   date that wasn't in the time window.

Respond with valid JSON only, exactly this shape:
{"sql": "SELECT ...", "rationale": "<one short sentence>"}"""


# Date/time-function guidance is DIALECT-SPECIFIC. The base _SYSTEM prompt
# above shows SQLite syntax (strftime / DATE() / julianday) in rules 3, 6 and
# 11. That syntax is INVALID on Postgres (prod), so when the runtime engine is
# Postgres we append an authoritative override block that (a) tells the model
# to target Postgres and (b) maps each SQLite date idiom to its Postgres-native
# equivalent, instructing it to use these INSTEAD OF the strftime/DATE/julianday
# examples shown above. The SQLite branch keeps the base prompt unchanged.
_SQLITE_DATE_GUIDANCE = """
ENGINE = SQLite. The date/time syntax in the rules above (strftime, DATE(),
julianday) is correct for this engine — use it as written."""

_POSTGRES_DATE_GUIDANCE = """
ENGINE = PostgreSQL. IMPORTANT — OVERRIDE: the example date functions in the
rules above (strftime(...), DATE(...), julianday(...)) are SQLite-only and are
INVALID on PostgreSQL. You MUST use these PostgreSQL-native equivalents instead:
  - Month bucket:  to_char("<date_col>"::timestamptz, 'YYYY-MM') AS month
  - Week bucket:   to_char("<date_col>"::timestamptz, 'IYYY-IW') AS week
  - Day bucket:    to_char("<date_col>"::timestamptz, 'YYYY-MM-DD') AS day
                   (or date_trunc('day', "<date_col>"::timestamptz))
  - Year bucket:   to_char("<date_col>"::timestamptz, 'YYYY') AS year
  - "today":       CURRENT_DATE
  - relative windows: use INTERVAL, e.g.
                   "<date_col>"::timestamptz >= CURRENT_DATE - INTERVAL '30 days'
  - period truncation: date_trunc('month'|'week'|'day', "<date_col>"::timestamptz)
Do NOT emit strftime, DATE(...) or julianday(...) anywhere in the SQL. Use the
EXACT date column name from the schema in place of <date_col>."""


def _system_prompt() -> str:
    """The sqlWriter system prompt with engine-aware date-syntax guidance
    appended. Reads the runtime engine via app.db_engine.is_postgres() so prod
    (Postgres) gets Postgres-native date functions and local/SQLite keeps the
    strftime/DATE/julianday guidance."""
    from app.db_engine import is_postgres
    guidance = _POSTGRES_DATE_GUIDANCE if is_postgres() else _SQLITE_DATE_GUIDANCE
    return _SYSTEM + "\n" + guidance


def _build_user_prompt(ctx: ToolContext, args: dict[str, Any]) -> str:
    state = ctx.state
    parts: list[str] = []
    parts.append(f"Question: {state.question!r}")
    if state.route:
        parts.append(f"Route: {state.route}")
    if state.granularity:
        parts.append(f"Granularity: {state.granularity}")
    if state.time_window:
        parts.append(f"Time window: {state.time_window}")
    if state.entities:
        parts.append(
            "Entities resolved by EntityLoc — add a WHERE filter for each:\n"
            + str(state.entities[:20])
        )
    if state.qualifiers:
        parts.append(
            f"Qualifiers detected: {state.qualifiers}. Use these to inform "
            f"ORDER BY direction (low/lowest/worst → ASC; "
            f"high/highest/best → DESC) and threshold filters."
        )
    schema = args.get("schema_summary") or state.schema_summary
    if schema:
        # Surface metric definitions FIRST and emphatically - otherwise the
        # model improvises margin/profit math and answers inconsistently.
        marker = "## METRIC DEFINITIONS"
        if marker in schema:
            parts.append(
                "MANDATORY FORMULAS - if the question asks about any metric "
                "below, your SQL MUST copy its formula verbatim (same joins, "
                "same SUM expressions):\n"
                + schema[schema.index(marker):]
            )
        parts.append(f"\nSchema:\n{schema}")
    intent = args.get("intent") or ""
    if intent:
        parts.append(f"\nDesired output: {intent}")

    # KPI hint — set by run_data_query when the question matches a registered
    # KPI. Provides the correct formula concept and aggregation type so
    # sqlWriter does not invent its own math.
    kpi_hint = getattr(state, "kpi_hint", None)
    if kpi_hint:
        parts.append(
            f"\nKPI CONTEXT (confidence={kpi_hint.get('confidence', 0):.2f}): "
            f"This question is about '{kpi_hint['name']}' "
            f"(output_type={kpi_hint['output_type']}, "
            f"aggregation={kpi_hint['aggregation_type']}).\n"
            f"Formula concept: {kpi_hint['formula']}\n"
            f"Description: {kpi_hint['description']}\n"
            f"IMPORTANT: adapt this formula to the ACTUAL column names in the "
            f"schema above. Do NOT copy column names from the formula literally "
            f"if the schema uses different names. The formula is a concept guide, "
            f"not a literal SQL snippet. For 'percent'/'ratio' KPIs, compute the "
            f"ratio correctly (do not just SUM percent values)."
        )

    parts.append(
        "\nReturn JSON: "
        '{"sql": "SELECT ...", "rationale": "<one short sentence>"}'
    )
    return "\n".join(parts)


# --- Deterministic profit/margin RANKING path ----------------------------
# The LLM writes margin/profit ranking SQL inconsistently (~half the runs
# use the wrong COGS join or average per-row ratios). Those product
# RANKING queries are pinned here - the same correct SQL every time.
#
# This path is deliberately NARROW. It fires ONLY for a clear product
# ranking by margin-% or by profit-amount. Everything else - total/gross
# profit, profit trends, profit grouped by month/category, or anything
# carrying an explicit time window - falls through to the LLM (guided by
# the METRIC DEFINITIONS block in the schema). A naive `\b(margin|profit)\b`
# match used to hijack all of those and answer the wrong shape.

_PROFIT_METRIC_RE = re.compile(r"\b(margin|profit|profitab\w*)\b", re.I)
_MARGIN_WORD_RE = re.compile(r"\bmargin\b", re.I)
_WORST_RE = re.compile(r"\b(worst|lowest|least|bottom|poor|weak|low)\b", re.I)
# Prefer a number adjacent to a ranking word ("top 5"); fall back to the
# first bare 1-3 digit number only if there is no ranking-anchored one.
_TOPN_RE = re.compile(
    r"\b(?:top|bottom|best|worst|highest|lowest|first|last)\s+(\d{1,3})\b",
    re.I,
)
_BARE_NUM_RE = re.compile(r"\b(\d{1,3})\b")

# Ranking evidence - the question must clearly want a product leaderboard.
_RANKING_RE = re.compile(
    r"\b(top|bottom|best|worst|highest|lowest|leading|most|least"
    r"|rank|ranked|ranking|leaderboard)\b"
    r"|\bby\s+(margin|profit)\b",
    re.I,
)

# Disqualifiers - any of these means the question is NOT a flat product
# ranking (it wants a total, a trend, a different grouping, or a time
# slice the pinned SQL cannot express). On any hit, fall through to the
# LLM. Over-disqualifying is safe: the LLM still answers correctly.
_DISQUALIFY_RE = re.compile(
    # grouping by something other than product
    r"\bby\s+(month|months|year|years|day|days|week|weeks|quarter|quarters"
    r"|date|dates|category|categories|brand|brands|region|regions|location"
    r"|locations|department|departments|segment|segments|store|stores"
    r"|customer|customers)\b"
    r"|\b(monthly|weekly|daily|yearly|quarterly)\b"
    r"|\bper\s+(month|year|day|week|quarter|date|category|brand|region"
    r"|location|store|customer)\b"
    # ranking by a NON-product dimension - the pinned SQL groups by
    # product, so a category / brand / region ranking is the wrong shape
    r"|\b(categor(?:y|ies)|subcategor(?:y|ies)|brands?|regions?"
    r"|departments?|segments?|locations?|stores?|customers?)\b"
    # trend
    r"|\b(trend|trends|over\s+time|time\s+series|growth)\b"
    # totals / aggregates
    r"|\b(total|overall|gross|combined|aggregate|sum|average|avg|mean)\b"
    # explicit time window
    r"|\b(today|yesterday|tomorrow)\b"
    r"|\b(this|last|past|next|previous)\s+(week|month|quarter|year|day)s?\b"
    r"|\blast\s+\d+\s+(day|days|week|weeks|month|months|year|years)\b"
    r"|\b(19|20)\d{2}\b"
    r"|\b(january|february|march|april|june|july|august|september"
    r"|october|november|december)\b",
    re.I,
)


def _ranking_limit(text: str) -> int:
    """Pull an explicit 'top N' count from the text. Prefers a number
    anchored to a ranking word ('top 5') over a stray number elsewhere
    in the question ('store 7'). Default 10, clamped to 1..50."""
    m = _TOPN_RE.search(text) or _BARE_NUM_RE.search(text)
    if not m:
        return 10
    try:
        return max(1, min(int(m.group(1)), 50))
    except (TypeError, ValueError):
        return 10


def _margin_ranking_intent(
    question: str, intent: str, route: str | None,
) -> str | None:
    """Decide whether the question is a clear product ranking by a
    profit/margin metric. Returns:
      - 'margin'  -> rank products by realized margin %
      - 'profit'  -> rank products by profit amount
      - None      -> not a clear ranking; fall through to the LLM

    Hybrid rule: fire ONLY when there is ranking evidence AND no
    disqualifying cue (other grouping, trend, totals, explicit time
    window). 'margin' wins over 'profit' when both words appear, since
    'profit margin' is a percentage metric."""
    q = question or ""
    combined = f"{q} {intent or ''}"
    if not _PROFIT_METRIC_RE.search(combined):
        return None
    has_ranking = (
        bool(_RANKING_RE.search(combined)) or (route or "").upper() == "RANKING"
    )
    if not has_ranking:
        return None
    # Disqualifiers are vetoes - check the USER's words only. The LLM's
    # free-text `intent` loosely uses 'total' / 'sum' / 'average' even
    # when describing a ranking, and that would wrongly veto it.
    if _DISQUALIFY_RE.search(q):
        return None
    return "margin" if _MARGIN_WORD_RE.search(combined) else "profit"


def _deterministic_ranking_sql(
    question: str,
    intent: str,
    route: str | None,
    resolved: ResolvedSchema | None,
) -> str | None:
    """When the question is a clear product ranking by margin-% or by
    profit-amount AND the dataset's columns resolve to the realized-margin
    concepts, return the canonical SQL directly - no LLM, no guessing.

    The SQL is built by MetricSqlBuilder from the RESOLVED column names,
    so it adapts to whatever the uploaded workbook named its columns.
    Returns None when this path does not apply (falls through to the LLM)."""
    shape = _margin_ranking_intent(question, intent, route)
    if shape is None:
        return None
    if resolved is None or not resolved.can_compute_margin:
        return None
    text = f"{question or ''} {intent or ''}"
    direction = "ASC" if _WORST_RE.search(text) else "DESC"
    limit = _ranking_limit(text)
    builder = MetricSqlBuilder(resolved)
    if shape == "margin":
        return builder.margin_ranking(direction=direction, limit=limit)
    return builder.profit_ranking(direction=direction, limit=limit)


class SqlWriterAgent(Tool):
    name = "sqlWriter"
    description = (
        "Sub-agent that writes a single SQLite SELECT to answer the "
        "question, given the schema and resolved time window. Returns "
        "{sql, rationale}. Always pair with SqlDryRun + SqlExecutor."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": (
                    "Short description of the SELECT you want, e.g. "
                    "'total revenue grouped by Product Name, top 10'."
                ),
            },
            "schema_summary": {
                "type": "string",
                "description": (
                    "Optional override - defaults to the Schema tool's "
                    "summary already stored in state."
                ),
            },
        },
        "required": ["intent"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        llm: LLMClient | None = ctx.llm
        if llm is None:
            return ToolOutcome(ok=False, error="sqlWriter requires an LLM client.")

        # Deterministic path: a clear product ranking by margin-% or
        # profit-amount gets the exact pinned SQL, never the LLM's
        # improvisation. Everything else falls through to the LLM.
        state = ctx.state
        pinned = _deterministic_ranking_sql(
            state.question, str(args.get("intent") or ""), state.route,
            state.resolved_schema,
        )
        if pinned is not None:
            return ToolOutcome(
                ok=True,
                output={
                    "sql": pinned,
                    "rationale": "Pinned canonical SQL for a profit/margin product ranking.",
                },
                state_updates={"sql_draft": pinned},
            )

        user = _build_user_prompt(ctx, args)
        resp = await llm.complete(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=1000,
            force_json=True,
        )
        if resp.error:
            return ToolOutcome(ok=False, error=resp.error)
        try:
            data = parse_strict_json(resp.content)
        except ValueError as e:
            return ToolOutcome(
                ok=False,
                error=f"sqlWriter returned non-JSON: {e}",
                output={"raw": resp.content[:400]},
            )
        sql = str(data.get("sql") or "").strip().rstrip(";")
        rationale = str(data.get("rationale") or "").strip()
        if not sql:
            return ToolOutcome(
                ok=False,
                error="sqlWriter returned empty sql",
                output={"raw": resp.content[:400]},
            )
        return ToolOutcome(
            ok=True,
            output={"sql": sql, "rationale": rationale},
            state_updates={"sql_draft": sql},
        )


__all__ = ["SqlWriterAgent"]
