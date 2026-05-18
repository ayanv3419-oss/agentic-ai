"""
RouteClass - classify the user question into a high-level route so the
Coordinator can pick the right downstream sub-agent / tool sequence.

Deterministic keyword scoring - no LLM call. Cheap, fast, predictable.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome


_ROUTES: dict[str, tuple[str, ...]] = {
    "RCA": (
        "why", "reason", "root cause", "rca", "explain the drop",
        "what caused", "decline", "fell", "dropped", "decrease",
        "down by", "decreased",
    ),
    "FORECAST": (
        "forecast", "predict", "projection", "next week", "next month",
        "expected", "will be", "going to sell",
    ),
    "TREND": (
        "trend", "over time", "growth", "monthly trend", "weekly trend",
        "year over year", "yoy", "mom",
    ),
    "RANKING": (
        "top", "bottom", "best", "worst", "rank", "leading",
        "best-selling", "best selling",
    ),
    "COMPARISON": (
        "compare", "vs", "versus", "difference", "between",
    ),
    "KPI": (
        "kpi", "metric", "total sales", "total revenue", "average",
        "sum of", "count of", "how many", "how much",
    ),
    "ANALYTICS": (
        "show", "list", "report", "breakdown", "summary",
    ),
    "CHAT": (
        "hello", "hi", "hey", "thanks", "thank you",
    ),
}


class RouteClassTool(Tool):
    name = "RouteClass"
    description = (
        "Classify the user's question into one of: RCA, FORECAST, TREND, "
        "RANKING, COMPARISON, KPI, ANALYTICS, CHAT. Pure keyword scoring. "
        "Use this early so you know whether to draft SQL, run RCA, or "
        "answer conversationally."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The user question to classify. "
                               "Defaults to the current turn's question.",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        q = (args.get("question") or ctx.state.question or "").lower().strip()
        if not q:
            return ToolOutcome(
                ok=False,
                error="No question to classify.",
            )
        scores: dict[str, int] = {}
        for route, kws in _ROUTES.items():
            hits = sum(1 for kw in kws if kw in q)
            if hits:
                scores[route] = hits
        if not scores:
            route = "ANALYTICS"
            confidence = 0.3
        else:
            route = max(scores, key=lambda r: scores[r])
            top = scores[route]
            total = sum(scores.values())
            confidence = round(top / max(1, total), 2)
        return ToolOutcome(
            ok=True,
            output={
                "route": route,
                "confidence": confidence,
                "scores": scores,
            },
            state_updates={"route": route},
        )


__all__ = ["RouteClassTool"]
