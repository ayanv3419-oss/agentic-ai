"""ResultAggregator — turns raw rows into chart-ready totals + series.

When SqlExecutor returns zero rows, this tool probes the underlying table to
distinguish two very different "no data" cases:

  • table_empty            — the table holds no records at all. The user
                             needs to upload data.
  • filter_excluded_all    — the table has data, but the query's time filter
                             (or any other WHERE clause) excluded every row.
                             We surface the available date range so the
                             ResponseFormatter can tell the user how to widen
                             the search.

The diagnostic is written into `aggregates['empty_reason']`.

Logs:
  AGGREGATOR INPUT ROW COUNT: <n>
  AGGREGATOR EMPTY DIAGNOSTIC: <reason>  (only when n=0)
  AGGREGATOR TOTALS: <totals>  series=<n>
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.database import fetch_one, quoted, ALLOWED_TABLES
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require

log = logging.getLogger("agentic_ai.result_aggregator")


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
        log.warning("aggregator: empty-state probe failed", exc_info=True)
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


class ResultAggregatorTool(Tool):
    name = "ResultAggregator"
    description = "Normalizes SQL rows into {totals, series, empty_reason?} chart-ready data."
    args_model = ResultAggregatorArgs
    independent = False

    async def run(self, state: TurnState, args: ResultAggregatorArgs) -> ToolResult:
        miss = require(state, "rows")
        if miss:
            log.error("ResultAggregator halted — state.rows is None")
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
        log.info("AGGREGATOR INPUT ROW COUNT: %d (granularity=%s)",
                 len(rows), granularity)

        empty_reason = None
        if len(rows) == 0:
            empty_reason = await _diagnose_empty(state)
            if empty_reason:
                log.info("AGGREGATOR EMPTY DIAGNOSTIC: %s", empty_reason)
            else:
                log.info("AGGREGATOR EMPTY DIAGNOSTIC: probe unavailable")

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
            "granularity": granularity,
            "totals": {
                "total_sales": round(total_sales, 2),
                "orders":      int(total_orders),
                "customers":   int(max_customers),
            },
            "series": series,
        }
        if empty_reason is not None:
            aggregates["empty_reason"] = empty_reason

        log.info(
            "AGGREGATOR TOTALS: %s  series=%d",
            aggregates["totals"], len(series),
        )

        return ToolResult(
            ok=True,
            output=aggregates,
            state_updates={"aggregates": aggregates, "chart_data": aggregates},
        )
