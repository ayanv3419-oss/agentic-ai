"""
v1 bridge — minimal adapter so v2 capability bodies can delegate to the
existing v1 ``Tool`` primitives without spreading TurnState references
across the v2 codebase.

The bridge builds a synthetic ``TurnState`` from a slice of
``ExecutionState`` (plus per-call args), executes the primitive, and
returns the raw ``ToolResult`` envelope. Capabilities unpack that into
their typed Pydantic output.

Why a bridge instead of a wholesale move?
-----------------------------------------
v1 primitives are TurnState-coupled (they read state.intent, state.rows,
state.time_window, …). Untangling each one would be a large diff against
the 5,693-LOC monolith. The bridge lets P2 ship without touching v1 —
the primitives stay where they are, and the physical move is a
mechanical no-logic refactor during the P9 sunset.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator_v2.state import ExecutionState


def build_turn_state(
    state: ExecutionState,
    *,
    intent: dict[str, Any] | None = None,
    time_window: dict[str, Any] | None = None,
    granularity: str | None = None,
    kpis: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    db_schema: dict[str, Any] | None = None,
    sql_plan: dict[str, Any] | None = None,
    sql_final: str | None = None,
    rows: list[dict[str, Any]] | None = None,
    aggregates: dict[str, Any] | None = None,
    insights: dict[str, Any] | None = None,
    route: str | None = None,
):
    """
    Construct a v1 ``TurnState`` populated with whatever upstream
    capability outputs are available. Late-imports the class so v2
    package load doesn't depend on the monolith.

    Only kwargs that map cleanly to v1 fields are accepted; everything
    else lives on ``ExecutionState`` and is fetched separately.
    """
    from app.analytics_engine import TurnState

    return TurnState(
        turn_id=state.turn_id,
        question=state.question,
        conversation_id=state.conversation_id,
        route=route,
        intent=intent,
        time_window=time_window,
        granularity=granularity,
        kpis=kpis or [],
        entities=entities or [],
        db_schema=db_schema,
        sql_plan=sql_plan,
        sql_final=sql_final,
        rows=rows,
        aggregates=aggregates,
        insights=insights,
    )


__all__ = ["build_turn_state"]
