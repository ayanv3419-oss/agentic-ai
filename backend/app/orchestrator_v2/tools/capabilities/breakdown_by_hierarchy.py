"""
Capability: breakdown_by_hierarchy
==================================

First-class drilldown by a hierarchy level (e.g., revenue by Class →
revenue by Line → revenue by Type). Uses the existing v2 product
hierarchy (``app.hierarchy.v2``) and location hierarchy
(``app.hierarchy.location``).

Currently v1 has no first-class drilldown — a hierarchy breakdown is
expressed as a ``run_data_query`` with a group-by dimension. v2 promotes
this to a dedicated capability so the Critic can flag "missing hierarchy
analysis" deterministically (Critic aspect: ``missing_hierarchy_breakdown``).

Status
------
P1 — typed contract + stub. Real body lands in P3 (depends on
``run_data_query`` P2 body + the v2 hierarchy module's join logic).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState, TimeWindow
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


HierarchyLevel = Literal[
    # product hierarchy v2
    "need", "family", "class", "line", "type", "item",
    # location hierarchy
    "region", "branch",
]


class BreakdownByHierarchyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(
        ...,
        description="Metric to roll up ('revenue', 'orders', 'units_sold').",
    )
    level: HierarchyLevel = Field(
        ...,
        description="Hierarchy level to break down by.",
    )
    time_window: TimeWindow | None = None
    parent_filter: str | None = Field(
        None,
        description=(
            "Optional ancestor node — e.g., drill 'class=Men' down by 'line'. "
            "If unset, breakdown is across all roots at this level."
        ),
    )
    top_n: int | None = Field(None, ge=1, le=50)


class HierarchyBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: float | int
    parent: str | None = None
    pct_of_parent: float | None = None


class BreakdownByHierarchyOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    level: HierarchyLevel
    buckets: tuple[HierarchyBucket, ...] = ()
    placeholder: bool = True
    note: str = "stub — body implementation lands in P3"


@register_capability
class BreakdownByHierarchy(
    Capability[BreakdownByHierarchyArgs, BreakdownByHierarchyOutput]
):
    name = "breakdown_by_hierarchy"
    description = (
        "Roll up a metric by a hierarchy level (product class/line/type, "
        "or location region/branch). Returns ordered buckets with optional "
        "share-of-parent. Use whenever the user asks 'by category', 'across "
        "products', 'per store', or the Critic flags missing breakdown."
    )
    args_model = BreakdownByHierarchyArgs
    output_model = BreakdownByHierarchyOutput
    requires: tuple[str, ...] = ()
    pure = False

    async def run(
        self,
        state: ExecutionState,
        args: BreakdownByHierarchyArgs,
    ) -> BreakdownByHierarchyOutput:
        # Real body: run a product/ranking data query and join the
        # results against the relevant hierarchy table to roll up by
        # the requested level.
        from app.orchestrator_v2.tools.capabilities.run_data_query import (
            RunDataQuery,
            RunDataQueryArgs,
        )

        rdq = RunDataQuery()
        sub_args = RunDataQueryArgs(
            intent="ranking",
            dimensions=("product",),
            time_window=args.time_window,
            filters={},
            top_n=200,
        )
        out = await rdq.run(state, sub_args)

        # If the underlying query failed or returned nothing, surface
        # cleanly rather than fabricating buckets.
        if out.empty_reason or not out.items:
            return BreakdownByHierarchyOutput(
                metric=args.metric,
                level=args.level,
                buckets=(),
                placeholder=False,
                note=out.empty_reason or "no data for breakdown",
            )

        # Join product items against product_sku_master + hierarchy v2
        # so we can roll up by the requested level. Read-only SELECTs
        # via the existing aiosqlite connection factory.
        from app.infrastructure import get_connection

        level = args.level
        is_product_level = level in {"need", "family", "class", "line", "type", "item"}
        is_location_level = level in {"region", "branch"}

        if not is_product_level and not is_location_level:
            return BreakdownByHierarchyOutput(
                metric=args.metric, level=args.level, buckets=(),
                placeholder=False, note=f"unsupported level {args.level}",
            )

        # For location levels there's no per-row branch attribution in
        # the sample data — surface a clear note rather than fabricating.
        if is_location_level:
            return BreakdownByHierarchyOutput(
                metric=args.metric, level=args.level, buckets=(),
                placeholder=False,
                note="location-level breakdown requires per-row branch column "
                     "(not in default schema)",
            )

        # Product-level rollup. Build a product → level-label map.
        async with get_connection() as conn:
            async with conn.execute(
                f"""
                SELECT psm.product_name, h.name
                FROM product_sku_master psm
                LEFT JOIN product_hierarchy_v2 h
                  ON h.id = psm.{level}_id
                """
            ) as cur:
                rows = await cur.fetchall()

        product_to_level: dict[str, str] = {}
        for r in rows:
            pname = r[0]
            lname = r[1] or "(uncategorised)"
            if pname:
                product_to_level[str(pname).lower()] = str(lname)

        # Aggregate items by the level label.
        bucket_totals: dict[str, float] = {}
        for item in out.items:
            pname = (item.label or "").lower()
            label = product_to_level.get(pname, "(uncategorised)")
            bucket_totals[label] = bucket_totals.get(label, 0.0) + float(item.value)

        grand_total = sum(bucket_totals.values()) or 1.0
        buckets_sorted = sorted(bucket_totals.items(), key=lambda kv: kv[1], reverse=True)
        if args.top_n:
            buckets_sorted = buckets_sorted[: args.top_n]

        buckets: list[HierarchyBucket] = [
            HierarchyBucket(
                label=label,
                value=round(value, 2),
                pct_of_parent=round((value / grand_total) * 100, 2),
            )
            for label, value in buckets_sorted
        ]

        return BreakdownByHierarchyOutput(
            metric=args.metric,
            level=args.level,
            buckets=tuple(buckets),
            placeholder=False,
            note="",
        )
