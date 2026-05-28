"""
Coordinator tools — Phase 3: fine-grained tool registry.

The LLM sees ELEVEN tools directly and orchestrates the sequence itself.
Capabilities are still defined (for backward compat / tests) but no
longer registered.

  Perception:  RouteClass, TimeKPI, Granularity, EntityLoc
  Schema:      Schema
  SQL:         sqlWriter, SqlDryRun, SqlExecutor
  Causal:      CausalTree, rcaReasoner
  Answer:      insightFmt        ← TERMINAL (turn ends when this succeeds)

Two hooks enforce critical invariants the small LLMs tend to violate:
  - SqlExecutor blocked until SqlDryRun has passed on the EXACT SQL.
  - Restriction guard whitelists the 11 tool names so hallucinated names fail clean.
"""
from __future__ import annotations

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.registry import ToolRegistry, get_registry

# Raw tools — all 8 are now registered + LLM-visible.
from app.coordinator.tools.causal_tree import CausalTreeTool
from app.coordinator.tools.entity_loc import EntityLocTool
from app.coordinator.tools.granularity import GranularityTool
from app.coordinator.tools.route_class import RouteClassTool
from app.coordinator.tools.schema import SchemaTool
from app.coordinator.tools.sql_dry_run import SqlDryRunTool
from app.coordinator.tools.sql_executor import SqlExecutorTool
from app.coordinator.tools.time_kpi import TimeKPITool

# Sub-agents — also LLM-visible now (they were already Tool subclasses).
from app.coordinator.sub_agents.sql_writer import SqlWriterAgent
from app.coordinator.sub_agents.rca_reasoner import RcaReasonerAgent
from app.coordinator.sub_agents.insight_fmt import InsightFmtAgent

# Phase 2 capabilities — kept importable for back-compat / tests, but no
# longer registered with the LLM. The fine-grained tools above replace them.
from app.coordinator.capabilities import (
    UnderstandQuestionCapability,
    RunDataQueryCapability,
    ExplainChangeCapability,
    WriteAnswerCapability,
)


def build_default_registry() -> ToolRegistry:
    """Build the registry with the 11 fine-grained tools (Phase 3).

    The LLM picks any of these per round and the loop continues until
    `insightFmt` succeeds OR the cost cap is hit.
    """
    reg = ToolRegistry()
    for tool in (
        # Perception / understanding
        RouteClassTool(),
        TimeKPITool(),
        GranularityTool(),
        EntityLocTool(),
        # Schema introspection
        SchemaTool(),
        # SQL pipeline
        SqlWriterAgent(),
        SqlDryRunTool(),
        SqlExecutorTool(),
        # Causal / RCA
        CausalTreeTool(),
        RcaReasonerAgent(),
        # Terminal — writes the final user-facing answer
        InsightFmtAgent(),
    ):
        reg.register(tool)
    return reg


__all__ = [
    "Tool",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "build_default_registry",
    "get_registry",
    # Raw tools
    "CausalTreeTool",
    "EntityLocTool",
    "GranularityTool",
    "RouteClassTool",
    "SchemaTool",
    "SqlDryRunTool",
    "SqlExecutorTool",
    "TimeKPITool",
    # Sub-agents
    "SqlWriterAgent",
    "RcaReasonerAgent",
    "InsightFmtAgent",
    # Capabilities (deprecated but importable)
    "UnderstandQuestionCapability",
    "RunDataQueryCapability",
    "ExplainChangeCapability",
    "WriteAnswerCapability",
]
