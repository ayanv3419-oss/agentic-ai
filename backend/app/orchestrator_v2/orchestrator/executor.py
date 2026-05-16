"""
Executor — runs a typed ``Plan`` against the registered capabilities.

Semantics:

  * Steps are run in declared order subject to ``depends_on``.
  * Steps sharing a ``parallel_group`` and all whose dependencies are
    already satisfied run concurrently (bounded by
    ``MAX_PARALLEL_CAPABILITIES``).
  * Each step's args are resolved by substituting ``$<step_id>`` /
    ``$<step_id>.<key>`` references with the corresponding executed
    step's output.
  * Each step produces a ``StepResult`` (success or failure). Results
    are appended to the ``ExecutionState`` in execution order.
  * The Executor does NOT raise — every failure is captured on a
    StepResult and surfaced via SSE so downstream Validators + Critic
    can react.

The Executor is idempotent across reflection iterations: it skips any
step whose ``step_id`` already has a successful StepResult on the state.
That is how delta plans run only their NEW nodes without re-executing
upstream steps.
"""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from app.orchestrator_v2.execution.parallel import gather_bounded
from app.orchestrator_v2.execution.retry_manager import execute_with_retry
from app.orchestrator_v2.state import (
    ExecutionState,
    Plan,
    PlanStep,
    StepResult,
)
from app.orchestrator_v2.tools.registry import (
    CapabilityRegistry,
    get_capability_registry,
)

if TYPE_CHECKING:
    from app.analytics_engine import EventEmitter

log = logging.getLogger("orchestrator_v2.executor")


# ---------------------------------------------------------------------------
# Argument substitution
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, results: dict[str, StepResult]) -> Any:
    """
    Resolve a ``$<step_id>`` or ``$<step_id>.<dotted.path>`` reference to
    the corresponding step's output. Missing references resolve to None
    so downstream Pydantic validation surfaces the issue cleanly.
    """
    body = ref[1:]  # strip the leading '$'
    head, _, rest = body.partition(".")
    step = results.get(head)
    if step is None or step.output is None:
        return None
    if not rest:
        return step.output
    cur: Any = step.output
    for part in rest.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _substitute(value: Any, results: dict[str, StepResult]) -> Any:
    """Recursively substitute $ref values inside args."""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        # Special-case: don't substitute literal SQL placeholders or
        # currency markers. The convention is $<identifier-starts-with-letter>.
        first = value[1].lower()
        if first.isalpha() or first == "_":
            return _resolve_ref(value, results)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, results) for v in value]
    return value


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _ready_steps(
    plan: Plan,
    done_ids: set[str],
    pending_ids: set[str],
) -> list[PlanStep]:
    """Return all steps whose dependencies are satisfied and which
    haven't run / aren't already in-flight."""
    ready: list[PlanStep] = []
    for step in plan.steps:
        if step.step_id in done_ids or step.step_id in pending_ids:
            continue
        if all(dep in done_ids for dep in step.depends_on):
            ready.append(step)
    return ready


def _split_parallel(ready: list[PlanStep]) -> list[list[PlanStep]]:
    """
    Group consecutive ready steps by ``parallel_group`` so an entire
    group runs concurrently. Steps without a group run one-at-a-time.
    """
    groups: list[list[PlanStep]] = []
    by_group: dict[str, list[PlanStep]] = {}
    for step in ready:
        if step.parallel_group:
            by_group.setdefault(step.parallel_group, []).append(step)
        else:
            groups.append([step])
    for steps in by_group.values():
        groups.append(steps)
    return groups


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


async def _run_one(
    step: PlanStep,
    state: ExecutionState,
    results: dict[str, StepResult],
    registry: CapabilityRegistry,
    emit: "EventEmitter",
) -> StepResult:
    cap = registry.get(step.capability)
    resolved_args = _substitute(step.args, results)

    # Frontend renders ``tool.call`` / ``tool.result`` for any name —
    # using the capability name here is the v1-compatible event payload
    # the existing UI already knows how to display.
    started = time.perf_counter()
    await emit.emit("tool.call", {
        "name": step.capability,
        "step_id": step.step_id,
        "args": resolved_args,
        "reasoning": step.rationale,
    })

    result = await execute_with_retry(cap, state, resolved_args)

    duration_ms = (time.perf_counter() - started) * 1000.0
    status: str
    if result.ok:
        status = "done"
    elif "stub_capability" in result.notes:
        status = "skipped"
    else:
        status = "failed"

    step_result = StepResult(
        step_id=step.step_id,
        capability=step.capability,
        status=status,
        output=result.output,
        error=result.error,
        started_at=started,
        duration_ms=duration_ms,
    )

    await emit.emit("tool.result", {
        "name": step.capability,
        "step_id": step.step_id,
        "ok": result.ok,
        "duration_ms": round(duration_ms, 2),
        "error": result.error,
        "output": result.output,
    })
    return step_result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_plan(
    plan: Plan,
    state: ExecutionState,
    emit: "EventEmitter",
    *,
    registry: CapabilityRegistry | None = None,
) -> ExecutionState:
    """
    Walk the plan DAG. Returns an updated state with every step's
    StepResult appended.
    """
    registry = registry or get_capability_registry()

    # Seed the result map with any already-executed steps so delta plans
    # transparently re-use upstream outputs without re-running them.
    results: dict[str, StepResult] = {
        s.step_id: s for s in state.executed_steps if s.status == "done"
    }
    done_ids: set[str] = set(results.keys())

    current = state.apply(plan=plan)

    # Bounded loop with progress check to avoid an infinite hang if the
    # DAG has a circular dependency the parser missed.
    safety_iterations = max(1, len(plan.steps) * 2)
    for _ in range(safety_iterations):
        ready = _ready_steps(plan, done_ids, pending_ids=set())
        if not ready:
            break
        groups = _split_parallel(ready)
        for group in groups:
            if len(group) == 1:
                step_result = await _run_one(group[0], current, results, registry, emit)
                current = current.append_step_result(step_result)
                results[step_result.step_id] = step_result
                done_ids.add(step_result.step_id)
            else:
                coros = [_run_one(s, current, results, registry, emit) for s in group]
                parallel_results = await gather_bounded(coros)
                for step_result in parallel_results:
                    current = current.append_step_result(step_result)
                    results[step_result.step_id] = step_result
                    done_ids.add(step_result.step_id)

    # Surface any steps that were declared in the plan but never ran
    # (typically because a dependency failed).
    pending_after: list[PlanStep] = [
        s for s in plan.steps if s.step_id not in done_ids
    ]
    if pending_after:
        log.warning(
            "executor finished with %d step(s) un-executed: %s",
            len(pending_after), [s.step_id for s in pending_after],
        )
        for s in pending_after:
            current = current.append_step_result(
                StepResult(
                    step_id=s.step_id,
                    capability=s.capability,
                    status="skipped",
                    error="dependency_failed_or_unreachable",
                    duration_ms=0.0,
                )
            )

    return current


__all__ = ["execute_plan"]
