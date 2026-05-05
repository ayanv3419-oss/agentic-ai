"""AnalyticsAgent — trend / comparison questions; LLM-narrated insight."""
from __future__ import annotations

from app.agents.base import SubAgent


class AnalyticsAgent(SubAgent):
    name = "AnalyticsAgent"
    pipeline = (
        ("RouteClassifier",   {}),
        ("IntentAnalyzer",    {}),
        ("TimeKPI",           {}),
        ("EntityResolver",    {}),
        ("SchemaRetriever",   {}),
        ("SqlPlanner",        {}),
        ("SqlWriter",         {}),
        ("SqlValidator",      {}),
        ("SqlExecutor",       {}),
        ("ResultAggregator",  {}),
        ("InsightEngine",     {"mode": "llm"}),
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )
