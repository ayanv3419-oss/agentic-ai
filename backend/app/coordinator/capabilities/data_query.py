"""
run_data_query capability.

Collapses the SQL pipeline into one LLM call:
  Schema → sqlWriter(intent) → SqlDryRun → [retry once on failure] → SqlExecutor

Guarantees:
  - Schema is always called before sqlWriter so the LLM sees real columns.
  - SqlDryRun always validates before SqlExecutor — the guard that was in
    hooks.py is now structural (can't be bypassed).
  - One automatic retry: if SqlDryRun fails the first time, sqlWriter is
    called again with the error context, then SqlDryRun again. After two
    failures the capability returns an error without running SqlExecutor.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.schema import SchemaTool
from app.coordinator.tools.sql_dry_run import SqlDryRunTool
from app.coordinator.tools.sql_executor import SqlExecutorTool
from app.coordinator.sub_agents.sql_writer import SqlWriterAgent


_schema_tool   = SchemaTool()
_writer_agent  = SqlWriterAgent()
_dryrun_tool   = SqlDryRunTool()
_executor_tool = SqlExecutorTool()


class RunDataQueryCapability(Tool):
    name = "run_data_query"
    description = (
        "Run a data query end-to-end. Provide a plain-English description "
        "of the desired result (intent). This capability automatically: "
        "reads the schema, writes SQL, validates it, and executes it. "
        "Returns the result rows and a chart payload. "
        "Call understand_question FIRST so entities and time window are ready."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": (
                    "Short description of what you want, e.g. "
                    "'top 10 products by revenue in the resolved time window' "
                    "or 'total sales by month'. Be specific about grouping, "
                    "ordering, and limit."
                ),
            },
            "max_rows": {
                "type": "integer",
                "default": 200,
                "minimum": 1,
                "maximum": 2000,
                "description": "Maximum rows to return. Default 200.",
            },
        },
        "required": ["intent"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        intent   = str(args.get("intent") or "").strip()
        max_rows = max(1, min(2000, int(args.get("max_rows") or 200)))

        if not intent:
            return ToolOutcome(ok=False, error="intent is required")
        if ctx.llm is None:
            return ToolOutcome(ok=False, error="run_data_query requires an LLM client")

        updates: dict[str, Any] = {}
        state = ctx.state

        # ── Step 1: Schema ──────────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        schema_outcome = await _schema_tool.run({"include_user_tables": True}, sub)
        if not schema_outcome.ok:
            return ToolOutcome(
                ok=False,
                error=f"Schema failed: {schema_outcome.error}",
            )
        if schema_outcome.state_updates:
            updates.update(schema_outcome.state_updates)
            state = state.apply(**schema_outcome.state_updates)

        # ── Step 2: sqlWriter + SqlDryRun (with one auto-retry) ─────────
        sql: str | None = None
        dryrun_error: str | None = None

        for attempt in range(2):
            writer_intent = intent
            if attempt == 1 and dryrun_error:
                writer_intent = (
                    f"{intent}\n\n"
                    f"IMPORTANT: Your previous SQL was rejected by SqlDryRun "
                    f"with this error: {dryrun_error!r}. Fix it."
                )

            sub = ToolContext(state=state, llm=ctx.llm)
            writer_outcome = await _writer_agent.run(
                {"intent": writer_intent}, sub,
            )
            if not writer_outcome.ok:
                return ToolOutcome(
                    ok=False,
                    error=f"sqlWriter failed: {writer_outcome.error}",
                    output={"schema": schema_outcome.output},
                )
            if writer_outcome.state_updates:
                updates.update(writer_outcome.state_updates)
                state = state.apply(**writer_outcome.state_updates)

            sql_candidate = str(
                (writer_outcome.output or {}).get("sql") or state.sql_draft or ""
            ).strip()

            if not sql_candidate:
                return ToolOutcome(ok=False, error="sqlWriter returned empty SQL")

            sub = ToolContext(state=state, llm=ctx.llm)
            dry_outcome = await _dryrun_tool.run({"sql": sql_candidate}, sub)
            if dry_outcome.ok:
                sql = sql_candidate
                if dry_outcome.state_updates:
                    updates.update(dry_outcome.state_updates)
                    state = state.apply(**dry_outcome.state_updates)
                break
            else:
                dryrun_error = dry_outcome.error
                if attempt == 0:
                    continue  # retry
                # Both attempts failed
                return ToolOutcome(
                    ok=False,
                    error=(
                        f"SQL validation failed after 2 attempts. "
                        f"Last error: {dryrun_error}"
                    ),
                    output={
                        "schema_summary": state.schema_summary,
                        "last_sql": sql_candidate,
                        "dryrun_error": dryrun_error,
                    },
                )

        if not sql:
            return ToolOutcome(ok=False, error="No valid SQL produced")

        # ── Step 3: SqlExecutor ─────────────────────────────────────────
        sub = ToolContext(state=state, llm=ctx.llm)
        exec_outcome = await _executor_tool.run({"sql": sql, "max_rows": max_rows}, sub)
        if not exec_outcome.ok:
            return ToolOutcome(
                ok=False,
                error=f"SqlExecutor failed: {exec_outcome.error}",
                output={"sql": sql},
            )
        if exec_outcome.state_updates:
            updates.update(exec_outcome.state_updates)
            state = state.apply(**exec_outcome.state_updates)

        exec_out = exec_outcome.output or {}
        row_count = int(exec_out.get("row_count", 0) or 0)
        rows = exec_out.get("rows") or []

        output: dict[str, Any] = {
            "sql": sql,
            "row_count": row_count,
            "rows_sample": rows[:10],
            "chart": exec_out.get("chart"),
            "schema_summary": state.schema_summary,
        }

        if row_count == 0:
            # Zero rows — emit a hard stop signal so the coordinator does
            # not loop by retrying with invented entity filters. The table
            # simply has no data for this period. The coordinator MUST
            # call write_answer next and report "no data available".
            output["NO_DATA"] = (
                "Zero rows returned. The table has no data for the "
                "requested period. STOP — do NOT call run_data_query "
                "again. Call write_answer immediately and tell the user "
                "no data is available for this time range."
            )

        return ToolOutcome(
            ok=True,
            output=output,
            state_updates=updates,
        )


__all__ = ["RunDataQueryCapability"]
