"""SqlWriter — renders state.sql_plan into a SQL string (state.sql_draft).

Deterministic; no LLM. Values for WHERE clauses become parameter placeholders
(`?`) and the corresponding bind list is stored in state.sql_plan['_params']
so SqlExecutor can pass them safely.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.database.schema import quoted, ALLOWED_TABLES
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require


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

        # Stash params on the plan so SqlExecutor can find them.
        new_plan = dict(plan)
        new_plan["_params"] = params

        return ToolResult(
            ok=True,
            output={"sql": sql, "params_count": len(params)},
            state_updates={"sql_draft": sql, "sql_plan": new_plan},
        )
