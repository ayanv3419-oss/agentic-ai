"""
Capability: run_data_query
==========================

End-to-end data query: schema retrieval → SQL planning → SQL writing →
SQL validation → SQL execution → result aggregation. This is the core
workhorse for "show me / count / sum / top N / trend" questions.

Internally chains the existing primitives:

    SchemaRetrieverTool → SqlPlannerTool → SqlWriterTool
                       → SqlValidatorTool → SqlExecutorTool
                       → ResultAggregatorTool

The Planner picks ONE call to ``run_data_query`` for any data-shaped
question; it never sees the six internal primitives.

Status
------
P1 — typed contract + stub. Body lands in P2 (delegates to the existing
``RunDataQueryTool`` capability that already chains these primitives).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState, TimeWindow
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


QueryIntent = Literal[
    "trend",
    "ranking",
    "summary",
    "anomaly",
    "purchase_summary",
    "sales_summary",
    "product_performance",
]


class RunDataQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: QueryIntent = Field(
        ...,
        description="Shape of the question — drives the SQL plan template.",
    )
    dimensions: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Group-by dimensions ('product', 'customer', 'date', 'branch', etc.)",
    )
    time_window: TimeWindow | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    top_n: int | None = Field(None, ge=1, le=200)


class AggregateBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: float | int
    extra: dict[str, Any] = Field(default_factory=dict)


class RunDataQueryOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str = ""               # e.g. "totals", "ranking", "trend"
    totals: dict[str, Any] | None = None
    series: tuple[AggregateBucket, ...] = ()
    items: tuple[AggregateBucket, ...] = ()
    empty_reason: str | None = None
    placeholder: bool = True
    note: str = "stub — body implementation lands in P2"


@register_capability
class RunDataQuery(Capability[RunDataQueryArgs, RunDataQueryOutput]):
    name = "run_data_query"
    description = (
        "Run a complete deterministic SQL pipeline: plan, write, validate, "
        "execute, aggregate. Returns totals / series / items + diagnostics. "
        "Use for any 'show me / count / top N / trend' question."
    )
    args_model = RunDataQueryArgs
    output_model = RunDataQueryOutput
    requires: tuple[str, ...] = ()
    pure = False  # DB read

    async def run(
        self,
        state: ExecutionState,
        args: RunDataQueryArgs,
    ) -> RunDataQueryOutput:
        # P2 body: delegate to the v1 ``RunDataQueryTool`` capability via
        # the bridge. v1 maps ``query_type`` onto the same QueryIntent
        # vocabulary we use here.
        from app.analytics_engine import get_registry as v1_registry
        from app.orchestrator_v2.tools._v1_bridge import build_turn_state

        # Map v2 intent → v1 query_type. v1 uses a slightly different
        # vocabulary; the table below is the canonical mapping.
        intent_to_qt = {
            "trend":                "trend_analysis",
            "ranking":              "product_performance",
            "summary":              "sales_summary",
            "anomaly":              "anomaly_detection",
            "purchase_summary":     "purchase_analysis",
            "sales_summary":        "sales_summary",
            "product_performance":  "product_performance",
        }
        query_type = intent_to_qt.get(args.intent, "sales_summary")

        # Construct a synthetic v1 TurnState with the time window we
        # already resolved (if any) so RunDataQueryTool doesn't re-run
        # TimeKPI internally.
        tw_dict = (
            {
                "start_date": args.time_window.start_date,
                "end_date": args.time_window.end_date,
                "granularity": args.time_window.granularity,
            }
            if args.time_window is not None
            else None
        )
        turn = build_turn_state(
            state,
            intent={"type": query_type, "comparison": False},
            time_window=tw_dict,
            granularity=args.time_window.granularity if args.time_window else None,
        )

        # Invoke v1's registered capability through the registry so the
        # Sentry instrumentation + per-tool span wrapping fire too.
        registry = v1_registry()
        v1_args: dict[str, Any] = {"query_type": query_type}
        result = await registry.execute("run_data_query", v1_args, turn)

        if not result.ok:
            return RunDataQueryOutput(
                kind="error",
                empty_reason=result.error,
                placeholder=False,
                note="",
            )

        out = result.output or {}
        from app.orchestrator_v2.tools.capabilities.run_data_query import AggregateBucket  # self-ref

        def _bucket(b: dict[str, Any]) -> AggregateBucket:
            label = str(b.get("label") or b.get("name") or "")
            value_raw = b.get("value")
            try:
                value = float(value_raw) if value_raw is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
            extra = {k: v for k, v in b.items() if k not in ("label", "name", "value")}
            return AggregateBucket(label=label, value=value, extra=extra)

        items_raw = out.get("items") or []
        items = tuple(_bucket(b) for b in items_raw if isinstance(b, dict))

        # v1 returns series_len but not the series itself in the LLM
        # summary (to keep tokens down). Pull the full aggregates from
        # the underlying TurnState's chart_data when present.
        series_raw: list[dict[str, Any]] = []
        # Note: the v1 result doesn't surface series. Downstream nodes
        # (narrate, format_response) only need totals + items for the
        # P2 cut.

        return RunDataQueryOutput(
            kind=out.get("kind") or "summary",
            totals=out.get("totals"),
            series=tuple(_bucket(b) for b in series_raw if isinstance(b, dict)),
            items=items,
            empty_reason=out.get("empty_reason"),
            placeholder=False,
            note="",
        )
