"""SqlExecutor — runs `state.sql_final` with parameter binding from sql_plan.

Contract:
  • prerequisite: `state.sql_final` (set by SqlValidator).
  • on success: writes `state.rows = list[dict]` (may be `[]` if SQL matched 0 rows).
  • on DB error: returns ToolResult(ok=False, error=...) — pipeline halts.
  • never silently continues with no result.

Logs:
  EXECUTING SQL   : <sql>  (params=N)
  ROWS RETURNED   : <count>
"""
from __future__ import annotations

import logging
import sqlite3

from pydantic import BaseModel

from app.database import fetch_all
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require

log = logging.getLogger("agentic_ai.sql_executor")


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
            log.warning("SqlExecutor halted — sql_final not set")
            return miss

        plan = state.sql_plan or {}
        params = list(plan.get("_params") or [])
        sql = state.sql_final or ""

        log.info("EXECUTING SQL: %s  (params=%d)", sql, len(params))

        try:
            rows = await fetch_all(sql, tuple(params))
        except sqlite3.Error as e:
            log.warning("SqlExecutor SQL error: %s", e)
            return ToolResult(ok=False, error=f"SQL exec failed: {e}")
        except Exception as e:
            log.exception("SqlExecutor unexpected failure")
            return ToolResult(
                ok=False,
                error=f"DB error: {type(e).__name__}: {e}",
            )

        log.info("ROWS RETURNED: %d", len(rows))

        # rows MAY be []. That is valid (no records matched), not a failure.
        return ToolResult(
            ok=True,
            output={"row_count": len(rows), "rows_preview": rows[:5]},
            state_updates={"rows": rows},
        )
