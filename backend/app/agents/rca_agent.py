"""RCAAgent — root cause analysis questions ('why did sales drop')."""
from __future__ import annotations

from app.agents.base import SubAgent


class RCAAgent(SubAgent):
    name = "RCAAgent"
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
