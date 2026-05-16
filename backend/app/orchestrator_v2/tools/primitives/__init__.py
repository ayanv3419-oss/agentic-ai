"""
Internal primitives — re-exported from the legacy ``app.analytics_engine``
module.

During P1 the 14 v1 tool classes stay physically in ``analytics_engine.py``
to keep v1 fully untouched (zero regression risk). This package re-exports
them so v2 code can import from the **architectural** location:

    from app.orchestrator_v2.tools.primitives import SqlPlannerTool

regardless of where the class actually lives. The physical move into
per-tool modules happens incrementally during the sunset phase (P9) —
deleting the re-exports and updating ``app.analytics_engine``'s imports
is then a mechanical change.

This approach satisfies the plan's "no duplication" constraint while
deferring the disruptive physical move to a phase that's safe to do.
"""

from __future__ import annotations

# 14 primitives — order mirrors the v1 ``_bootstrap`` registration order
# in ``analytics_engine.py`` for traceability.
from app.analytics_engine import (
    Tool,                       # ABC reused by primitive subclasses
    ToolResult,                 # envelope type
    DatabaseTool,
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
)

__all__ = [
    "Tool",
    "ToolResult",
    "DatabaseTool",
    "RouteClassifierTool",
    "IntentAnalyzerTool",
    "TimeKPITool",
    "EntityResolverTool",
    "SchemaRetrieverTool",
    "SqlPlannerTool",
    "SqlWriterTool",
    "SqlValidatorTool",
    "SqlExecutorTool",
    "ResultAggregatorTool",
    "InsightEngineTool",
    "ResponseFormatterTool",
    "ResponseStoredTool",
]

PRIMITIVE_NAMES: tuple[str, ...] = (
    "DatabaseTool",
    "RouteClassifierTool",
    "IntentAnalyzerTool",
    "TimeKPITool",
    "EntityResolverTool",
    "SchemaRetrieverTool",
    "SqlPlannerTool",
    "SqlWriterTool",
    "SqlValidatorTool",
    "SqlExecutorTool",
    "ResultAggregatorTool",
    "InsightEngineTool",
    "ResponseFormatterTool",
    "ResponseStoredTool",
)
