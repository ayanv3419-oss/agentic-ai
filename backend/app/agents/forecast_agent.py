"""ForecastAgent — projects the metric forward via simple linear regression
on the bucketed series. Runs standard tool steps, splices a forecast
post-step that augments state.aggregates.series, then runs the rest.

No external ML deps. Algorithm: least-squares on (i, sales).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.agents.base import SubAgent, PipelineStep
from app.state import TurnState
from app.streaming import EventEmitter
from app.tools import get_registry
from app.tools.base import ToolResult
from app.state import ToolCallRecord

log = logging.getLogger("agentic_ai.agents.forecast")

_HORIZON_DAYS = 14


def _project(series: list[dict]) -> list[dict]:
    if len(series) < 2:
        return series
    xs = list(range(len(series)))
    ys = [float(s.get("sales") or 0) for s in series]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((x - mean_x) ** 2 for x in xs) or 1e-9
    slope = num / den
    intercept = mean_y - slope * mean_x
    last_bucket = series[-1].get("bucket", "")
    try:
        last_dt = datetime.strptime(str(last_bucket), "%Y-%m-%d")
    except ValueError:
        return series
    out = list(series)
    for i in range(1, _HORIZON_DAYS + 1):
        x = (n - 1) + i
        pred = max(slope * x + intercept, 0.0)
        out.append({
            "bucket": (last_dt + timedelta(days=i)).date().isoformat(),
            "sales":  round(pred, 2),
            "orders": 0,
            "predicted": True,
        })
    return out


_PRE: tuple[PipelineStep, ...] = (
    ("RouteClassifier",   {}),
    ("IntentAnalyzer",    {}),
    ("TimeKPI",           {}),
    ("EntityResolver",    {}),
    ("SchemaRetriever",   {}),
    ("SqlPlanner",        {}),
    ("SqlWriter",         {}),
    ("SqlValidator",      {}),
    ("SqlExecutor",       {}),
    ("ResultAggregator",  {}),
)
_POST: tuple[PipelineStep, ...] = (
    ("InsightEngine",     {"mode": "llm"}),
    ("ResponseFormatter", {}),
    ("ResponseStored",    {}),
)


class ForecastAgent(SubAgent):
    name = "ForecastAgent"
    pipeline = _PRE + _POST  # informational; actual run() splits at the seam

    async def run(self, state: TurnState, emit: EventEmitter) -> TurnState:
        state = await self._run_steps(state, emit, _PRE)
        if state.errors:
            return state
        state = self._inject_forecast(state)
        await emit.emit("tool.result", {
            "name": "ForecastProjector",
            "ok": True,
            "output": {"horizon_days": _HORIZON_DAYS},
            "error": None,
            "duration_ms": 0.0,
        })
        return await self._run_steps(state, emit, _POST)

    @staticmethod
    def _inject_forecast(state: TurnState) -> TurnState:
        aggregates = dict(state.aggregates or {})
        series = list(aggregates.get("series") or [])
        projected = _project(series)
        aggregates["series"] = projected
        aggregates["forecast_horizon_days"] = _HORIZON_DAYS
        return state.apply(aggregates=aggregates, chart_data=aggregates)

    async def _run_steps(
        self, state: TurnState, emit: EventEmitter, steps: tuple[PipelineStep, ...]
    ) -> TurnState:
        registry = get_registry()
        for tool_name, args_spec in steps:
            args = args_spec(state) if callable(args_spec) else dict(args_spec)
            iteration = state.iteration + 1
            state = state.apply(iteration=iteration)
            await emit.emit("tool.call", {
                "name": tool_name, "args": args, "iteration": iteration,
            })
            result: ToolResult = await registry.execute(tool_name, args, state)
            await emit.emit("tool.result", {
                "name": tool_name, "ok": result.ok, "output": result.output,
                "error": result.error, "duration_ms": round(result.duration_ms, 2),
            })
            state = state.append_tool_call(ToolCallRecord(
                name=tool_name, args=args, output=result.output,
                ok=result.ok, error=result.error,
                duration_ms=result.duration_ms, iteration=iteration,
            ))
            if not result.ok:
                state = state.append_error(f"{tool_name}: {result.error}")
                return state
            if result.state_updates:
                state = state.apply(**result.state_updates)
            if result.delta_metrics:
                state = state.apply(
                    tokens_in=state.tokens_in + int(result.delta_metrics.get("tokens_in", 0)),
                    tokens_out=state.tokens_out + int(result.delta_metrics.get("tokens_out", 0)),
                )
        return state
