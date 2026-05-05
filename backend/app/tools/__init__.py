"""Tool registry — exactly 14 tools, registered explicitly at startup.

If any tool import fails or a tool is missing, the registry raises at
boot time. There is no dynamic discovery; every tool is enumerated below.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.state import TurnState
from app.tools.base import Tool, ToolResult

log = logging.getLogger("agentic_ai.tools.registry")


# Authoritative list of tool names — must equal the registered set at boot.
TOOL_NAMES: tuple[str, ...] = (
    "RouteClassifier",
    "IntentAnalyzer",
    "TimeKPI",
    "EntityResolver",
    "SchemaRetriever",
    "SqlPlanner",
    "SqlWriter",
    "SqlValidator",
    "SqlExecutor",
    "ResultAggregator",
    "InsightEngine",
    "ResponseFormatter",
    "ResponseStored",
    "Database",
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool must have a name")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    @property
    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def schemas_for_prompt(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in self._tools.values():
            try:
                schema = tool.args_model.model_json_schema()
            except Exception:
                schema = {"type": "object", "properties": {}}
            out.append({
                "name": tool.name,
                "description": tool.description,
                "args_schema": schema,
            })
        return out

    async def execute(
        self, name: str, args: dict[str, Any], state: TurnState
    ) -> ToolResult:
        try:
            tool = self.get(name)
        except KeyError as e:
            return ToolResult(ok=False, error=f"Unknown tool: {e}")
        return await tool.execute(state, args)


_registry: ToolRegistry | None = None


def bootstrap(registry: ToolRegistry) -> None:
    """Register all 14 tools. Order matters only for determinism."""
    from app.tools.route_classifier import RouteClassifierTool
    from app.tools.intent_analyzer import IntentAnalyzerTool
    from app.tools.time_kpi import TimeKPITool
    from app.tools.entity_resolver import EntityResolverTool
    from app.tools.schema_retriever import SchemaRetrieverTool
    from app.tools.sql_planner import SqlPlannerTool
    from app.tools.sql_writer import SqlWriterTool
    from app.tools.sql_validator import SqlValidatorTool
    from app.tools.sql_executor import SqlExecutorTool
    from app.tools.result_aggregator import ResultAggregatorTool
    from app.tools.insight_engine import InsightEngineTool
    from app.tools.response_formatter import ResponseFormatterTool
    from app.tools.response_stored import ResponseStoredTool
    from app.tools.database import DatabaseTool

    classes: list[type[Tool]] = [
        RouteClassifierTool,
        IntentAnalyzerTool,
        TimeKPITool,
        EntityResolverTool,
        SchemaRetrieverTool,
        SqlPlannerTool,
        SqlWriterTool,
        SqlValidatorTool,
        SqlExecutorTool,
        ResultAggregatorTool,
        InsightEngineTool,
        ResponseFormatterTool,
        ResponseStoredTool,
        DatabaseTool,
    ]

    registered_names: list[str] = []
    for cls in classes:
        instance = cls()
        registry.register(instance)
        registered_names.append(instance.name)

    expected = set(TOOL_NAMES)
    actual = set(registered_names)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise RuntimeError(
            f"Tool registry mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        bootstrap(_registry)
    return _registry
