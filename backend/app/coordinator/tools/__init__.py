"""
Coordinator tools — Phase 2: capability-collapsed registry.

The LLM sees FOUR capabilities instead of 11 raw tools:

  understand_question  — RouteClass + TimeKPI + Granularity + EntityLoc
  run_data_query       — Schema + sqlWriter + SqlDryRun + SqlExecutor
  explain_change       — CausalTree + rcaReasoner
  write_answer         — insightFmt

Each capability runs its sub-tools in a guaranteed, code-enforced order.
The LLM cannot skip or mis-sequence them.

The raw tool classes are still importable for testing and for use as
internal implementation by the capabilities — they are simply not
registered in the default registry.
"""
from __future__ import annotations

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.registry import ToolRegistry, get_registry

# Raw tools — available for import/testing; not in the default registry.
from app.coordinator.tools.causal_tree import CausalTreeTool
from app.coordinator.tools.entity_loc import EntityLocTool
from app.coordinator.tools.granularity import GranularityTool
from app.coordinator.tools.route_class import RouteClassTool
from app.coordinator.tools.schema import SchemaTool
from app.coordinator.tools.sql_dry_run import SqlDryRunTool
from app.coordinator.tools.sql_executor import SqlExecutorTool
from app.coordinator.tools.time_kpi import TimeKPITool

# Phase 2 capabilities — these ARE registered.
from app.coordinator.capabilities import (
    UnderstandQuestionCapability,
    RunDataQueryCapability,
    ExplainChangeCapability,
    WriteAnswerCapability,
)


def build_default_registry() -> ToolRegistry:
    """Build the registry with the 4 Phase-2 capabilities.

    Sub-agents (sqlWriter, rcaReasoner, insightFmt) are NOT registered here —
    they are called internally by the capabilities, never directly by the LLM.
    """
    reg = ToolRegistry()
    for cap in (
        UnderstandQuestionCapability(),
        RunDataQueryCapability(),
        ExplainChangeCapability(),
        WriteAnswerCapability(),
    ):
        reg.register(cap)
    return reg


__all__ = [
    "Tool",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "build_default_registry",
    "get_registry",
    # Raw tools (for tests / internal use)
    "CausalTreeTool",
    "EntityLocTool",
    "GranularityTool",
    "RouteClassTool",
    "SchemaTool",
    "SqlDryRunTool",
    "SqlExecutorTool",
    "TimeKPITool",
    # Capabilities
    "UnderstandQuestionCapability",
    "RunDataQueryCapability",
    "ExplainChangeCapability",
    "WriteAnswerCapability",
]
