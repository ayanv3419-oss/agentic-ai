"""
RouteClass - classify the user question into a high-level route so the
Coordinator can pick the right downstream sub-agent / tool sequence.

Deterministic keyword scoring - no LLM call. Cheap, fast, predictable.

Matching uses whole-word / whole-phrase token matching to avoid false
positives like "hi" matching inside "which" or "this".
"""
from __future__ import annotations

import re
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
        # single-word hits
        "top", "bottom", "best", "worst", "rank", "leading",
        # multi-word product performance phrases
        "best-selling", "best selling", "hot selling", "top selling",
        "fast moving", "slow moving", "high demand", "low demand",
        "most popular", "least popular", "popular",
        # "which products" pattern — user is asking about a list/ranking
        "which products", "which items", "which brands",
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
        # Only exact whole-word greetings — no substrings.
        "hello", "hey", "thanks", "thank you",
        # "hi" is kept but matched as a whole word only (see _kw_matches).
        "hi",
    ),
}

# Pre-compile per-keyword patterns so we do whole-word matching for single
# tokens and substring matching for multi-word phrases (a phrase already
# can't accidentally match inside another word).
_KW_PATTERNS: dict[str, list[re.Pattern]] = {}

for _route, _kws in _ROUTES.items():
    _pats: list[re.Pattern] = []
    for _kw in _kws:
        if " " in _kw or "-" in _kw:
            # Multi-word / hyphenated phrase: boundary on the outside is
            # enough — the phrase itself can't sit inside a single token.
            _pats.append(re.compile(re.escape(_kw), re.I))
        else:
            # Single token: require word boundaries so "hi" never matches
            # inside "which", "this", "shipping", etc.
            _pats.append(re.compile(r"\b" + re.escape(_kw) + r"\b", re.I))
    _KW_PATTERNS[_route] = _pats


def _score_route(route: str, question: str) -> int:
    """Count how many of the route's keywords/phrases match the question."""
    return sum(1 for pat in _KW_PATTERNS[route] if pat.search(question))


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
        q = (args.get("question") or ctx.state.question or "").strip()
        if not q:
            return ToolOutcome(
                ok=False,
                error="No question to classify.",
            )
        scores: dict[str, int] = {}
        for route in _ROUTES:
            hits = _score_route(route, q)
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
