"""
Granularity - choose an appropriate time bucket for any analysis or
chart axis. Deterministic: looks at the question wording first, then
falls back to the time-window span.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


_VALID = ("hour", "day", "week", "month", "quarter", "year")


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _from_keywords(q: str) -> str | None:
    if not q:
        return None
    ql = q.lower()
    if any(k in ql for k in ("hourly", "by hour")):
        return "hour"
    if any(k in ql for k in ("daily", "by day", "each day", "per day")):
        return "day"
    if any(k in ql for k in ("weekly", "by week", "per week", "each week")):
        return "week"
    if any(k in ql for k in ("monthly", "by month", "per month", "each month", "mom")):
        return "month"
    if any(k in ql for k in ("quarterly", "by quarter", "qoq")):
        return "quarter"
    if any(k in ql for k in ("yearly", "annual", "yoy", "year over year")):
        return "year"
    return None


def _from_span(start: date, end: date) -> str:
    days = max(1, (end - start).days + 1)
    if days <= 2:
        return "hour"
    if days <= 31:
        return "day"
    if days <= 90:
        return "week"
    if days <= 730:
        return "month"
    if days <= 1825:
        return "quarter"
    return "year"


class GranularityTool(Tool):
    name = "Granularity"
    description = (
        "Pick the right time bucket (hour, day, week, month, quarter, year) "
        "for an analysis or chart axis. Looks at question wording first, "
        "then falls back to the time-window span. Call this AFTER TimeKPI "
        "so the time_window is available."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "User question (defaults to current turn's question).",
            },
            "start_date": {
                "type": "string",
                "description": "ISO YYYY-MM-DD start of the relevant window.",
            },
            "end_date": {
                "type": "string",
                "description": "ISO YYYY-MM-DD end of the relevant window.",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        q = args.get("question") or ctx.state.question or ""
        kw_pick = _from_keywords(q)
        if kw_pick:
            return ToolOutcome(
                ok=True,
                output={"granularity": kw_pick, "source": "keywords"},
                state_updates={"granularity": kw_pick},
            )

        start = _parse_iso(args.get("start_date"))
        end = _parse_iso(args.get("end_date"))
        if start is None or end is None:
            tw = ctx.state.time_window or {}
            start = start or _parse_iso(tw.get("start_iso"))
            end = end or _parse_iso(tw.get("end_iso"))
        if start and end and end >= start:
            picked = _from_span(start, end)
            return ToolOutcome(
                ok=True,
                output={
                    "granularity": picked,
                    "source": "span",
                    "days": (end - start).days + 1,
                },
                state_updates={"granularity": picked},
            )
        return ToolOutcome(
            ok=True,
            output={"granularity": "month", "source": "default"},
            state_updates={"granularity": "month"},
        )


__all__ = ["GranularityTool", "_VALID"]
