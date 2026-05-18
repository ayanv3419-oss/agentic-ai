"""
Coordinator sub-agents.

A sub-agent is a higher-level Tool that wraps an LLM call (sqlWriter,
insightFmt, rcaReasoner). They look identical to plain tools to the
Coordinator LLM - same OpenAI function-schema, same ToolOutcome.

Only these 3 sub-agents exist:
  * sqlWriter   - turn intent + schema into SQL
  * rcaReasoner - narrate a CausalTree as a root-cause explanation
  * insightFmt  - produce the polished final answer (no SQL allowed)
"""
from __future__ import annotations

from app.coordinator.sub_agents.insight_fmt import InsightFmtAgent
from app.coordinator.sub_agents.rca_reasoner import RcaReasonerAgent
from app.coordinator.sub_agents.sql_writer import SqlWriterAgent


def register_sub_agents(registry) -> None:
    """Register the 3 sub-agents into the given ToolRegistry."""
    for agent in (SqlWriterAgent(), RcaReasonerAgent(), InsightFmtAgent()):
        registry.register(agent)


__all__ = [
    "InsightFmtAgent",
    "RcaReasonerAgent",
    "SqlWriterAgent",
    "register_sub_agents",
]
