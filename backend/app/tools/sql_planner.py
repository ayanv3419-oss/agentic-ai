"""SqlPlanner — produces a structured SQL plan from intent + time + entities.

Output state.sql_plan = {
    table:    "sales" | "purchase",
    select:   [{expr, alias}, ...],
    where:    [{col, op, value}, ...],
    group_by: [col, ...],
    order_by: [{col, dir}, ...],
    limit:    int,
}

SqlWriter consumes this plan deterministically; no LLM is involved.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.state import TurnState
from app.tools.base import Tool, ToolResult, require


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
    description = "Plans the SQL query (table, select, where, group_by, order_by, limit)."
    args_model = SqlPlannerArgs
    independent = False

    async def run(self, state: TurnState, args: SqlPlannerArgs) -> ToolResult:
        miss = require(state, "intent", "time_window")
        if miss:
            return miss

        intent = state.intent or {}
        time_window = state.time_window or {}
        granularity = state.granularity or "daily"
        route = state.route or "SALES_QUERY"

        # Table choice — purchase if explicitly purchase route, else sales.
        table = "purchase" if route == "PURCHASE_QUERY" else "sales"
        bucket_expr = _BUCKET_BY_GRAN.get(granularity, '"Date"')

        # Select clause — by intent.
        metric = intent.get("metric") or "total_amount"
        select: list[dict[str, str]] = []
        if metric in ("total_amount", "revenue"):
            select.append({"expr": 'SUM("Total Amount")', "alias": "sales"})
        if metric == "orders" or True:  # always include orders for chart context
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

        # Always project the bucket so we can chart trends.
        select.insert(0, {"expr": bucket_expr, "alias": "bucket"})

        # Where clause — Date window + optional Party filter from entities.
        where: list[dict] = [
            {"col": "Date", "op": ">=", "value": time_window.get("start_date")},
            {"col": "Date", "op": "<=", "value": time_window.get("end_date")},
        ]
        for ent in state.entities or []:
            canonical = ent.get("canonical")
            if canonical:
                where.append({
                    "col": "Party Name",
                    "op": "LIKE",
                    "value": f"%{canonical}%",
                })

        plan = {
            "table":    table,
            "select":   select,
            "where":    where,
            "group_by": ["bucket"],
            "order_by": [{"col": "bucket", "dir": "ASC"}],
            "limit":    1000,
        }

        # Single-row aggregate variants don't need group_by — but for chart
        # consistency we always group by bucket. ResultAggregator handles
        # totals separately.
        return ToolResult(ok=True, output=plan, state_updates={"sql_plan": plan})
