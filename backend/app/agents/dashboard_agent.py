"""DashboardAgent — NO LLM. Reads sales rows, verifies dates, filters, groups,
aggregates. Output matches the frontend's DashboardData type exactly:
  { month, kpis: {total_sales, orders, customers}, series: [{bucket, sales, orders}] }

Steps:
  1. DatabaseReader  — SELECT via the restricted Database tool (READ_PIN).
  2. DateNormalizer  — assert-and-skip rows whose Date is not ISO.
  3. DataFilter      — month filter (already enforced via SQL but
                       re-checked here).
  4. DataGrouper     — by Date.
  5. Aggregator      — SUM(Total Amount) and COUNT(*) per bucket; totals.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from app.database.schema import quoted
from app.tools import get_registry
from app.tools.database import READ_PIN
from app.state import TurnState

log = logging.getLogger("agentic_ai.agents.dashboard")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DashboardAgent:
    name = "DashboardAgent"

    async def run(self, *, month: str | None = None) -> dict[str, Any]:
        # Step 1 — DatabaseReader (read-only via Database tool).
        if month is None:
            sql = (
                f'SELECT "Date", "Total Amount", "Party Name" '
                f'FROM {quoted("sales")}'
            )
            params: list[Any] = []
        else:
            if not (len(month) == 7 and month[4] == "-"):
                raise ValueError(f"invalid month format: {month!r}")
            sql = (
                f'SELECT "Date", "Total Amount", "Party Name" '
                f'FROM {quoted("sales")} '
                f'WHERE "Date" LIKE ?'
            )
            params = [f"{month}-%"]

        registry = get_registry()
        dummy_state = TurnState(question="<dashboard>")
        result = await registry.execute(
            "Database",
            {"op": "select", "pin": READ_PIN, "sql": sql, "params": params},
            dummy_state,
        )
        if not result.ok:
            raise RuntimeError(f"DashboardAgent read failed: {result.error}")
        raw_rows: list[dict[str, Any]] = list((result.output or {}).get("rows") or [])

        # Step 2 — DateNormalizer (verify, skip non-ISO rows).
        valid: list[dict[str, Any]] = []
        skipped = 0
        for r in raw_rows:
            d = r.get("Date")
            if not isinstance(d, str) or not _ISO_RE.match(d):
                skipped += 1
                continue
            valid.append(r)
        if skipped:
            log.warning("DashboardAgent skipped %d rows with non-ISO Date", skipped)

        # Step 3 — DataFilter (already done by SQL, but defensive).
        if month:
            valid = [r for r in valid if r["Date"].startswith(month)]

        # Step 4 + 5 — Group by Date + aggregate.
        buckets: dict[str, dict[str, float]] = defaultdict(
            lambda: {"sales": 0.0, "orders": 0}
        )
        total_sales = 0.0
        orders = 0
        customers: set[str] = set()
        for r in valid:
            d = r["Date"]
            amt = float(r.get("Total Amount") or 0)
            buckets[d]["sales"] += amt
            buckets[d]["orders"] = int(buckets[d]["orders"]) + 1
            total_sales += amt
            orders += 1
            party = r.get("Party Name")
            if isinstance(party, str) and party:
                customers.add(party)

        series = [
            {"bucket": d, "sales": round(b["sales"], 2), "orders": int(b["orders"])}
            for d, b in sorted(buckets.items(), key=lambda x: x[0])
        ][:366]

        return {
            "month": month,
            "kpis": {
                "total_sales": round(total_sales, 2),
                "orders":      orders,
                "customers":   len(customers),
            },
            "series": series,
        }
