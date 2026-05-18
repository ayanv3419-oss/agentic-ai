"""
TurnState - the single immutable state object that flows through one
/query_stream turn.

Every mutation returns a NEW state via ``state.apply(**updates)``. Tools
and sub-agents NEVER write to state in place. The Coordinator loop owns
the state transitions.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ToolStatus = Literal["pending", "running", "ok", "error", "skipped"]


class ToolCall(BaseModel):
    """One LLM tool/sub-agent invocation request."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    iteration: int = 0
    reasoning: str | None = None


class ToolResult(BaseModel):
    """Outcome of a single ToolCall."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    status: ToolStatus
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0


class CostLedger(BaseModel):
    """Running cost / iteration tally - the cost guard consults this."""

    model_config = ConfigDict(frozen=True)

    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    bytes_scanned: int = 0

    def with_iter(self, tokens_in: int = 0, tokens_out: int = 0) -> "CostLedger":
        return CostLedger(
            iterations=self.iterations + 1,
            tokens_in=self.tokens_in + max(0, tokens_in),
            tokens_out=self.tokens_out + max(0, tokens_out),
            bytes_scanned=self.bytes_scanned,
        )

    def with_bytes(self, n: int) -> "CostLedger":
        return CostLedger(
            iterations=self.iterations,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            bytes_scanned=self.bytes_scanned + max(0, n),
        )


class TurnState(BaseModel):
    """
    Immutable per-turn state. Threaded through every Coordinator stage.

    Construct once at request boundary, then evolve only via ``apply()``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Identity -----------------------------------------------------------
    turn_id: str = Field(default_factory=lambda: f"turn-{uuid.uuid4().hex[:12]}")
    question: str
    conversation_id: str | None = None
    started_at: float = Field(default_factory=time.time)

    # Routing ------------------------------------------------------------
    route: str | None = None              # set by RouteClass tool
    granularity: str | None = None        # set by Granularity tool
    time_window: dict[str, Any] | None = None  # set by TimeKPI tool
    entities: list[dict[str, Any]] = Field(default_factory=list)
    schema_summary: str | None = None

    # SQL + data ---------------------------------------------------------
    sql_draft: str | None = None
    sql_final: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    chart_payload: dict[str, Any] | None = None

    # Reasoning / output -------------------------------------------------
    causal_tree: dict[str, Any] | None = None
    insights: list[str] = Field(default_factory=list)
    final_answer: str | None = None

    # Loop bookkeeping ---------------------------------------------------
    iteration: int = 0
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    cost: CostLedger = Field(default_factory=CostLedger)
    errors: list[str] = Field(default_factory=list)
    finished: bool = False

    # Idioms -------------------------------------------------------------

    def apply(self, **updates: Any) -> "TurnState":
        """Return a NEW state with the given fields replaced."""
        return self.model_copy(update=updates)

    def with_tool_call(self, call: ToolCall) -> "TurnState":
        return self.apply(tool_calls=[*self.tool_calls, call])

    def with_tool_result(self, result: ToolResult) -> "TurnState":
        return self.apply(tool_results=[*self.tool_results, result])

    def with_error(self, msg: str) -> "TurnState":
        return self.apply(errors=[*self.errors, msg])


__all__ = [
    "CostLedger",
    "ToolCall",
    "ToolResult",
    "ToolStatus",
    "TurnState",
]
