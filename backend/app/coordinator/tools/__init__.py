"""
Coordinator tools.

The 8 tools the LLM is allowed to call:

  RouteClass    - classify the question into a route
  Granularity   - pick day/week/month/quarter/year
  EntityLoc     - resolve entities (products, parties, branches)
  TimeKPI       - resolve a time window + KPI hints
  Schema        - return the database schema summary
  SqlDryRun     - validate SQL without executing
  SqlExecutor   - execute a SELECT and return rows
  CausalTree    - root-cause reasoning over a metric drop/spike

The registry exposes the OpenAI/Ollama tool-calling JSON schemas via
``registry.tool_specs()`` and ``registry.execute(name, args, state)``.
"""
from __future__ import annotations

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.coordinator.tools.causal_tree import CausalTreeTool
from app.coordinator.tools.entity_loc import EntityLocTool
from app.coordinator.tools.granularity import GranularityTool
from app.coordinator.tools.registry import ToolRegistry, get_registry
from app.coordinator.tools.route_class import RouteClassTool
from app.coordinator.tools.schema import SchemaTool
from app.coordinator.tools.sql_dry_run import SqlDryRunTool
from app.coordinator.tools.sql_executor import SqlExecutorTool
from app.coordinator.tools.time_kpi import TimeKPITool


def build_default_registry() -> ToolRegistry:
    """Build the registry with all 8 production tools."""
    reg = ToolRegistry()
    for tool in (
        SchemaTool(),
        RouteClassTool(),
        GranularityTool(),
        TimeKPITool(),
        EntityLocTool(),
        SqlDryRunTool(),
        SqlExecutorTool(),
        CausalTreeTool(),
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
]
