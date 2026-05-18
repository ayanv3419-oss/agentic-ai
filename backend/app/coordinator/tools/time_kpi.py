"""
TimeKPI - resolve a relative date expression ("last month", "this week",
"yesterday") into an explicit (start_iso, end_iso) window anchored to
the latest date present in the uploaded data, and surface any KPI hints
the question implies.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.time_engine import resolve_dataset_date_tokens


_RELATIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\byesterday\b", re.I),               "yesterday"),
    (re.compile(r"\btoday\b", re.I),                   "today"),
    (re.compile(r"\bthis\s+week\b", re.I),             "this_week"),
    (re.compile(r"\blast\s+week\b", re.I),             "last_week"),
    (re.compile(r"\bthis\s+month\b", re.I),            "this_month"),
    (re.compile(r"\blast\s+month\b", re.I),            "last_month"),
    (re.compile(r"\bthis\s+quarter\b", re.I),          "this_quarter"),
    (re.compile(r"\blast\s+quarter\b", re.I),          "last_quarter"),
    (re.compile(r"\bthis\s+year\b", re.I),             "this_year"),
    (re.compile(r"\blast\s+year\b", re.I),             "last_year"),
    (re.compile(r"\blast\s+(\d+)\s+days?\b", re.I),    "last_n_days"),
    (re.compile(r"\blast\s+(\d+)\s+weeks?\b", re.I),   "last_n_weeks"),
    (re.compile(r"\blast\s+(\d+)\s+months?\b", re.I),  "last_n_months"),
]

_KPI_HINTS: dict[str, tuple[str, ...]] = {
    "revenue":       ("revenue", "sales", "income", "total amount"),
    "orders":        ("orders", "transactions", "bills", "invoices"),
    "customers":     ("customers", "buyers", "clients", "unique customers"),
    "avg_basket":    ("average basket", "avg basket", "avg order", "average order"),
    "profit":        ("profit", "margin", "gross"),
    "quantity":      ("units", "qty", "quantity", "pieces sold"),
}


def _window_for(token: str, anchor: date, m: re.Match | None = None) -> tuple[date, date]:
    if token == "today":
        return anchor, anchor
    if token == "yesterday":
        y = anchor - timedelta(days=1)
        return y, y
    if token == "this_week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, anchor
    if token == "last_week":
        start = anchor - timedelta(days=anchor.weekday() + 7)
        return start, start + timedelta(days=6)
    if token == "this_month":
        return anchor.replace(day=1), anchor
    if token == "last_month":
        first_this = anchor.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev, last_prev
    if token == "this_quarter":
        q_start_month = ((anchor.month - 1) // 3) * 3 + 1
        return date(anchor.year, q_start_month, 1), anchor
    if token == "last_quarter":
        q_start_month = ((anchor.month - 1) // 3) * 3 + 1
        this_q_start = date(anchor.year, q_start_month, 1)
        last_q_end = this_q_start - timedelta(days=1)
        last_q_start_month = ((last_q_end.month - 1) // 3) * 3 + 1
        last_q_start = date(last_q_end.year, last_q_start_month, 1)
        return last_q_start, last_q_end
    if token == "this_year":
        return date(anchor.year, 1, 1), anchor
    if token == "last_year":
        return date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)
    if token == "last_n_days" and m:
        n = max(1, int(m.group(1)))
        return anchor - timedelta(days=n - 1), anchor
    if token == "last_n_weeks" and m:
        n = max(1, int(m.group(1)))
        return anchor - timedelta(weeks=n) + timedelta(days=1), anchor
    if token == "last_n_months" and m:
        n = max(1, int(m.group(1)))
        y, mth = anchor.year, anchor.month
        for _ in range(n):
            if mth == 1:
                y -= 1; mth = 12
            else:
                mth -= 1
        return date(y, mth, 1), anchor
    return anchor.replace(day=1), anchor


def _detect_kpi_hints(question: str) -> list[str]:
    if not question:
        return []
    q = question.lower()
    hits: list[str] = []
    for kpi, kws in _KPI_HINTS.items():
        if any(kw in q for kw in kws):
            hits.append(kpi)
    return hits


async def _dataset_today() -> date:
    """Anchor = most recent date present in the uploaded data."""
    try:
        tokens = await resolve_dataset_date_tokens()
        latest = tokens.get("dataset_today") or tokens.get("dataset_max")
        if isinstance(latest, str) and len(latest) >= 10:
            from datetime import datetime as _dt
            return _dt.strptime(latest[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    # Fall back to today's calendar date.
    from datetime import datetime as _dt
    return _dt.now().date()


class TimeKPITool(Tool):
    name = "TimeKPI"
    description = (
        "Resolve a relative time expression in the question (yesterday, "
        "this/last week/month/quarter/year, last N days/weeks/months) to "
        "an explicit ISO start/end date window anchored to the most recent "
        "date in the uploaded data. Also detects KPI hints (revenue, "
        "orders, customers, avg_basket, profit, quantity)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Question to scan (defaults to current turn's).",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        question = args.get("question") or ctx.state.question or ""
        anchor = await _dataset_today()
        chosen_token: str | None = None
        chosen_match: re.Match | None = None
        for pat, token in _RELATIVE_PATTERNS:
            m = pat.search(question)
            if m:
                chosen_token = token
                chosen_match = m
                break
        if chosen_token is None:
            # Default to last 30 days when no explicit relative window.
            start = anchor - timedelta(days=29)
            end = anchor
            label = "last 30 days (default)"
        else:
            start, end = _window_for(chosen_token, anchor, chosen_match)
            label = chosen_token.replace("_", " ")

        # Clamp days_in_month edge case
        if end.day > calendar.monthrange(end.year, end.month)[1]:
            end = end.replace(day=calendar.monthrange(end.year, end.month)[1])

        tw = {
            "start_iso": start.isoformat(),
            "end_iso": end.isoformat(),
            "label": label,
            "anchor": anchor.isoformat(),
        }
        kpi_hints = _detect_kpi_hints(question)
        return ToolOutcome(
            ok=True,
            output={"time_window": tw, "kpi_hints": kpi_hints},
            state_updates={"time_window": tw},
        )


__all__ = ["TimeKPITool"]
