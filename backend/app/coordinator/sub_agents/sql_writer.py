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


_SYSTEM = """You are sqlWriter, a sub-agent of the Coordinator.

Your single job: produce ONE valid SQLite SELECT statement that answers
the user's question against the database described in the schema. You
NEVER execute SQL. You NEVER produce DDL, INSERT, UPDATE or DELETE.

Rules:
1. Use only tables/columns that appear in the supplied schema.
2. Always quote identifiers that contain spaces or special characters
   with double quotes (e.g. "Total Amount", "Party Name").
3. Apply the supplied time window if relevant - filter on the "Date"
   column with start_iso/end_iso (or the analogous date column in a
   user table).
4. Prefer SUM / COUNT / GROUP BY for aggregate questions.
5. Add ORDER BY DESC and LIMIT for ranking questions.
6. NEVER wrap the SQL in markdown fences in the final JSON.
7. METRIC DEFINITIONS ARE MANDATORY. If the schema (or the MANDATORY
   FORMULAS block) defines a metric the question asks about - margin,
   profit, revenue, units sold, cost of goods - you MUST build your SQL
   from that exact formula: same JOIN, same SUM(...) expressions, same
   column names. Copy it verbatim. NEVER average per-row ratios
   (AVG(margin)) - always compute on SUM totals exactly as written.
   NEVER substitute a formula of your own.

Respond with valid JSON only, exactly this shape:
{"sql": "SELECT ...", "rationale": "<one short sentence>"}"""


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
        parts.append(f"Entities (top 20): {state.entities[:20]}")
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
    parts.append(
        "\nReturn JSON: "
        '{"sql": "SELECT ...", "rationale": "<one short sentence>"}'
    )
    return "\n".join(parts)


# --- Deterministic margin path -------------------------------------------
# The LLM writes margin SQL inconsistently (~half the runs use the wrong
# join or average per-row ratios). Margin is too important to leave to
# chance, so when the question is about margin AND the schema defines the
# realized-margin metric, we build the canonical SQL directly - no LLM.

_MARGIN_RE = re.compile(r"\b(margin|profit|profitab\w*)\b", re.I)
_WORST_RE = re.compile(r"\b(worst|lowest|least|bottom|poor|weak|low)\b", re.I)
_METRIC_TABLES_RE = re.compile(
    r'JOIN\s+"([^"]+)"\s+s\s+to\s+"([^"]+)"\s+i', re.I
)
_TOPN_RE = re.compile(r"\b(\d{1,3})\b")


def _deterministic_margin_sql(
    question: str, intent: str, schema: str | None,
) -> str | None:
    """When the question is about margin/profit AND the schema carries the
    realized-margin metric definition, return the canonical SQL directly.
    No LLM, no guessing - the same correct query every time. Returns None
    when this path does not apply (falls through to the LLM writer)."""
    text = f"{question or ''} {intent or ''}"
    if not _MARGIN_RE.search(text):
        return None
    if not schema or "## METRIC DEFINITIONS" not in schema:
        return None
    m = _METRIC_TABLES_RE.search(schema)
    if not m:
        return None
    sales_t, inv_t = m.group(1), m.group(2)
    direction = "ASC" if _WORST_RE.search(text) else "DESC"
    n = _TOPN_RE.search(text)
    limit = max(1, min(int(n.group(1)), 50)) if n else 10
    return (
        "SELECT s.final_product, "
        "ROUND(SUM(s.net_sales), 2) AS total_net_sales, "
        "ROUND(SUM(s.quantity * i.unit_cost), 2) AS total_cogs, "
        "ROUND((SUM(s.net_sales) - SUM(s.quantity * i.unit_cost)) * 100.0 "
        "/ SUM(s.net_sales), 2) AS margin_pct "
        f'FROM "{sales_t}" s JOIN "{inv_t}" i ON s.sku_id = i.sku_id '
        "GROUP BY s.final_product HAVING SUM(s.net_sales) > 0 "
        f"ORDER BY margin_pct {direction} LIMIT {limit}"
    )


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

        # Deterministic path: margin/profit questions get the exact pinned
        # SQL, never the LLM's improvisation.
        state = ctx.state
        schema = args.get("schema_summary") or state.schema_summary
        pinned = _deterministic_margin_sql(
            state.question, str(args.get("intent") or ""), schema,
        )
        if pinned is not None:
            return ToolOutcome(
                ok=True,
                output={
                    "sql": pinned,
                    "rationale": "Realized-margin metric - pinned canonical formula.",
                },
                state_updates={"sql_draft": pinned},
            )

        user = _build_user_prompt(ctx, args)
        resp = await llm.complete(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=600,
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
