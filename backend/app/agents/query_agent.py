"""QueryAgent — straightforward lookup queries (totals, counts, distincts)."""
from __future__ import annotations

from app.agents.base import SubAgent


class QueryAgent(SubAgent):
    name = "QueryAgent"
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
        ("InsightEngine",     {"mode": "rule"}),
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )
