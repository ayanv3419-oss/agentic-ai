"""
Hooks - cross-cutting concerns wrapped around every tool dispatch.

Hooks fire in this order:
  1. pre_dispatch  - validate the call, optionally short-circuit (skip).
  2. dispatch      - the actual tool execution.
  3. post_dispatch - log, emit SSE, persist audit.

A hook returning ``HookOutcome(skip=True)`` short-circuits the dispatch
with a synthesized ToolOutcome (used by the cost guard).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.coordinator.state import ToolCall, ToolResult, TurnState
from app.coordinator.tools.base import ToolOutcome


_log = logging.getLogger("coordinator.hooks")

# Hard caps tied to the user's spec.
# MAX_ITERATIONS bounds the number of LLM rounds (each round may emit
# multiple tool calls). MAX_TOOL_CALLS bounds total tool dispatches per
# turn, catching the "single round emits 50 tools" failure mode.
# MAX_ITERATIONS was 10 - too tight: multi-step questions (compare /
# RCA / "hot sellers with margins") burned all 10 rounds before
# reaching insightFmt, so the formatter was skipped and the answer
# desynced from the chart. 16 leaves headroom for the final answer.
MAX_ITERATIONS = 16
MAX_TOOL_CALLS = 28


@dataclass
class HookOutcome:
    skip: bool = False
    forced_result: ToolOutcome | None = None
    reason: str | None = None


def cost_guard(state: TurnState, call: ToolCall) -> HookOutcome:
    """Short-circuit when iteration / tool-call budget is exhausted."""
    if state.cost.iterations >= MAX_ITERATIONS:
        return HookOutcome(
            skip=True,
            reason=f"iteration limit ({MAX_ITERATIONS}) reached",
            forced_result=ToolOutcome(
                ok=False,
                error=f"Iteration cap {MAX_ITERATIONS} exceeded - emit final answer.",
            ),
        )
    if state.iteration >= MAX_TOOL_CALLS:
        return HookOutcome(
            skip=True,
            reason=f"tool-call limit ({MAX_TOOL_CALLS}) reached",
            forced_result=ToolOutcome(
                ok=False,
                error=f"Tool-call cap {MAX_TOOL_CALLS} exceeded - emit final answer.",
            ),
        )
    return HookOutcome()


def restriction_guard(state: TurnState, call: ToolCall) -> HookOutcome:
    """Block any tool name we don't expose. Defense in depth - the
    OpenAI tools list already limits choices, but the LLM could
    hallucinate a name."""
    _ALLOWED = {
        "Schema", "RouteClass", "Granularity", "TimeKPI", "EntityLoc",
        "SqlDryRun", "SqlExecutor", "CausalTree",
        "sqlWriter", "rcaReasoner", "insightFmt",
    }
    if call.name not in _ALLOWED:
        return HookOutcome(
            skip=True,
            reason=f"tool {call.name!r} is not in the allowed set",
            forced_result=ToolOutcome(
                ok=False,
                error=(
                    f"Tool {call.name!r} is not registered. "
                    f"Allowed: {sorted(_ALLOWED)}"
                ),
            ),
        )
    return HookOutcome()


def sql_dryrun_guard(state: TurnState, call: ToolCall) -> HookOutcome:
    """SqlExecutor must be preceded by a SqlDryRun that PASSED this turn.
    The system prompt mandates it; this enforces it so a hallucinating
    LLM cannot run SQL that was never dry-run validated."""
    if call.name != "SqlExecutor":
        return HookOutcome()
    if any(r.name == "SqlDryRun" and r.status == "ok"
           for r in state.tool_results):
        return HookOutcome()
    return HookOutcome(
        skip=True,
        reason="SqlExecutor called before a passing SqlDryRun",
        forced_result=ToolOutcome(
            ok=False,
            error=(
                "SqlExecutor is blocked: you must call SqlDryRun first and "
                "only run SqlExecutor after SqlDryRun reports the SQL valid."
            ),
        ),
    )


def log_call(state: TurnState, call: ToolCall) -> None:
    _log.info(
        "tool.call turn=%s iter=%d name=%s args=%s",
        state.turn_id, state.iteration, call.name,
        list(call.arguments.keys()),
    )


def log_result(state: TurnState, call: ToolCall, result: ToolResult) -> None:
    _log.info(
        "tool.result turn=%s iter=%d name=%s status=%s duration_ms=%.1f",
        state.turn_id, state.iteration, call.name, result.status,
        result.duration_ms,
    )


PreHook = Callable[[TurnState, ToolCall], HookOutcome]
PostHook = Callable[[TurnState, ToolCall, ToolResult], None]


PRE_HOOKS: list[PreHook] = [restriction_guard, cost_guard, sql_dryrun_guard]
POST_HOOKS: list[PostHook] = [log_result]


def run_pre_hooks(state: TurnState, call: ToolCall) -> HookOutcome:
    log_call(state, call)
    for h in PRE_HOOKS:
        outcome = h(state, call)
        if outcome.skip:
            return outcome
    return HookOutcome()


def run_post_hooks(state: TurnState, call: ToolCall, result: ToolResult) -> None:
    for h in POST_HOOKS:
        try:
            h(state, call, result)
        except Exception:
            _log.warning("post-hook failed", exc_info=True)


__all__ = [
    "HookOutcome",
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS",
    "cost_guard",
    "log_call",
    "log_result",
    "restriction_guard",
    "run_post_hooks",
    "run_pre_hooks",
    "sql_dryrun_guard",
]
