"""
Capability: compute_kpi
=======================

Execute a single named KPI from the registry against the current dataset.
Wraps the existing zero-LLM KPI engine (``app.kpi.engine.calculate_by_name``).

The front door (``front_door.py``) already short-circuits high-confidence
KPI matches before the Planner runs. This capability exists for queries
the Planner DOES route to v2 but where it judges a known KPI is a
sub-component of the answer (e.g., "revenue grew last week — by how
much?" → compute_kpi(total_revenue) + compare_periods + narrate).

Status
------
P1 — typed contract + stub. Body lands in P2 (calls
``app.kpi.calculate_by_name`` directly — no TurnState needed).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState, TimeWindow
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


class ComputeKpiArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kpi_id: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Registry KPI identifier (e.g., 'total_revenue').",
    )
    time_window: TimeWindow | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class ComputeKpiOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    kpi_id: str
    value: float | int | None = None
    format: str = "number"      # "currency" | "count" | "percentage" | "number"
    rows: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    placeholder: bool = True
    note: str = "stub — body implementation lands in P2"


@register_capability
class ComputeKpi(Capability[ComputeKpiArgs, ComputeKpiOutput]):
    name = "compute_kpi"
    description = (
        "Run a single named KPI from the registry; returns the deterministic "
        "value + chart-ready rows. Use for any well-known metric (revenue, "
        "orders, margin, AOV, etc.) where the Planner knows the KPI id."
    )
    args_model = ComputeKpiArgs
    output_model = ComputeKpiOutput
    requires: tuple[str, ...] = ()
    pure = False  # reads the DB

    async def run(
        self,
        state: ExecutionState,
        args: ComputeKpiArgs,
    ) -> ComputeKpiOutput:
        # P2 body: delegate to the deterministic KPI engine. The engine
        # NEVER raises — errors land in result.error.
        from app.kpi import calculate_by_name

        result = await calculate_by_name(args.kpi_id)
        # Use the user-safe payload — we deliberately do not expose the
        # raw SQL / formula to the Planner's prompt.
        user_view = result.to_user_dict()
        return ComputeKpiOutput(
            kpi_id=args.kpi_id,
            value=user_view.get("value"),
            format=user_view.get("format") or "number",
            rows=tuple(user_view.get("rows") or ()),
            error=result.error,
            placeholder=False,
            note="",
        )
