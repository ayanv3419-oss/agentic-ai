"""TurnState — frozen, validated state object that flows through every tool.

Mutations return a NEW instance via `state.apply(**updates)`. Tools never
mutate state in place.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    ok: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    iteration: int = 0


class TurnState(BaseModel):
    """Single source of truth for a query turn. Frozen — use `apply()`."""

    model_config = ConfigDict(frozen=True)

    # Identity / input
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str
    cache_key: str | None = None

    # Dispatch
    route: str | None = None              # set by RouteClassifier
    sub_agent: str | None = None          # set by Coordinator dispatcher

    # Intent / extraction
    intent: dict[str, Any] | None = None  # set by IntentAnalyzer
    time_window: dict[str, Any] | None = None  # set by TimeKPI
    granularity: str | None = None        # set by TimeKPI
    kpis: list[dict[str, Any]] = Field(default_factory=list)  # set by TimeKPI
    entities: list[dict[str, Any]] = Field(default_factory=list)  # set by EntityResolver

    # Schema / SQL
    db_schema: dict[str, Any] | None = None
    sql_plan: dict[str, Any] | None = None
    sql_draft: str | None = None
    sql_final: str | None = None

    # Result
    rows: list[dict[str, Any]] | None = None
    aggregates: dict[str, Any] | None = None
    insights: dict[str, Any] | None = None
    chart_data: dict[str, Any] | None = None

    # Output
    final_answer: str | None = None
    response_record: dict[str, Any] | None = None

    # Metrics / housekeeping
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    bytes_scanned: int = 0
    iteration: int = 0

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def apply(self, **updates: Any) -> "TurnState":
        return self.model_copy(update=updates)

    def append_tool_call(self, record: ToolCallRecord) -> "TurnState":
        return self.model_copy(update={"tool_calls": [*self.tool_calls, record]})

    def append_error(self, error: str) -> "TurnState":
        return self.model_copy(update={"errors": [*self.errors, error]})
