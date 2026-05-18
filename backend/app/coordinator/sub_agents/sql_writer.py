"""
sqlWriter - given the question + schema + resolved time window + entities,
produce a single-statement SELECT for the local Qwen 3 model to validate
and execute downstream.

Output contract (JSON):
  { "sql": "SELECT ...", "rationale": "one short sentence" }
"""
from __future__ import annotations

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
        parts.append(f"\nSchema:\n{schema}")
    intent = args.get("intent") or ""
    if intent:
        parts.append(f"\nDesired output: {intent}")
    parts.append(
        "\nReturn JSON: "
        '{"sql": "SELECT ...", "rationale": "<one short sentence>"}'
    )
    return "\n".join(parts)


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
