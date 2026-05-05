"""Sub-agent registry — exactly 7 sub-agents, registered at boot."""
from __future__ import annotations

from app.agents.analytics_agent import AnalyticsAgent
from app.agents.dashboard_agent import DashboardAgent
from app.agents.dataclean_agent import DataCleanAgent
from app.agents.forecast_agent import ForecastAgent
from app.agents.query_agent import QueryAgent
from app.agents.rca_agent import RCAAgent
from app.agents.response_agent import ResponseAgent


# Coordinator-callable analytic sub-agents (LLM picks one of these).
ANALYTIC_AGENTS: dict[str, type] = {
    "QueryAgent":     QueryAgent,
    "AnalyticsAgent": AnalyticsAgent,
    "RCAAgent":       RCAAgent,
    "ForecastAgent":  ForecastAgent,
}

# Sub-agents that the LLM may NOT pick — they have dedicated routes.
ROUTE_ONLY_AGENTS: tuple[str, ...] = (
    "DashboardAgent",
    "DataCleanAgent",
)

# Auto-coda after every analytic agent (informational; the analytic agents
# already include ResponseFormatter+ResponseStored in their pipeline).
CODA_AGENT = "ResponseAgent"

ALL_AGENT_NAMES: tuple[str, ...] = (
    "QueryAgent",
    "AnalyticsAgent",
    "RCAAgent",
    "ForecastAgent",
    "DashboardAgent",
    "DataCleanAgent",
    "ResponseAgent",
)


def get_analytic_agent(name: str):
    cls = ANALYTIC_AGENTS.get(name)
    if cls is None:
        raise KeyError(f"Unknown analytic sub-agent: {name!r}")
    return cls()


__all__ = [
    "ANALYTIC_AGENTS",
    "ROUTE_ONLY_AGENTS",
    "CODA_AGENT",
    "ALL_AGENT_NAMES",
    "get_analytic_agent",
    "AnalyticsAgent",
    "DashboardAgent",
    "DataCleanAgent",
    "ForecastAgent",
    "QueryAgent",
    "RCAAgent",
    "ResponseAgent",
]
