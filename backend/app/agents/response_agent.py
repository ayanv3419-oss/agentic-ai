"""ResponseAgent — pipeline coda for the analytic sub-agents.

Always invoked at the end of QueryAgent / AnalyticsAgent / RCAAgent /
ForecastAgent — but those agents already include `ResponseFormatter` and
`ResponseStored` as their last two steps, so this class exists for
catalog completeness and to give a single named place where the response
phase is defined (per the architecture diagram).
"""
from __future__ import annotations

from app.agents.base import SubAgent


class ResponseAgent(SubAgent):
    name = "ResponseAgent"
    pipeline = (
        ("ResponseFormatter", {}),
        ("ResponseStored",    {}),
    )
