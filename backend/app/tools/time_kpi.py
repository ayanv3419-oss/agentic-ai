"""TimeKPI — combined time window + KPI list + granularity, in one step.

Anchor-date policy (root-level fix):

  Relative phrases like "today", "last 2 months", "this week" need a
  reference date. Using the system clock gives wrong results on historical
  datasets — e.g. on a 2026-05 system clock with sales data only in
  2025-05–07, "last 2 months" filters into a window that contains zero
  rows even though plenty of data exists.

  Resolution chain (first match wins):
    1. MAX("Date") from the table implied by state.route
       (PURCHASE_QUERY → purchase, otherwise → sales).
    2. MAX("Date") from the other table.
    3. system clock today() — used only when neither table has any data.

  The chosen anchor is exposed as `time_window.as_of` and the source is
  recorded in `time_window.anchor_source` so downstream tools and the UI
  can see whether they're working from data-relative or wall-clock time.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from app.database import ALLOWED_TABLES, fetch_one, quoted
from app.state import TurnState
from app.tools.base import Tool, ToolResult

log = logging.getLogger("agentic_ai.time_kpi")


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
        log.warning("TimeKPI: MAX(Date) probe failed on %s", table, exc_info=True)
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
        log.info(
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
        """Resolve a relative time phrase to (start, end) using `anchor` as
        the reference date instead of the system clock."""
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
