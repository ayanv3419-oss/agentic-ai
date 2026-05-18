"""
Dispatcher - routes a ToolCall through hooks and the registry, returning
an updated TurnState (immutable apply pattern) and the produced
ToolResult.

This is the single seam between the Coordinator loop and the tools/
sub-agents: every tool invocation goes through dispatch().
"""
from __future__ import annotations

import logging
from typing import Any

from app.coordinator.hooks import run_post_hooks, run_pre_hooks
from app.coordinator.state import ToolCall, ToolResult, TurnState
from app.coordinator.tools.base import ToolContext
from app.coordinator.tools.registry import ToolRegistry


_log = logging.getLogger("coordinator.dispatcher")


async def dispatch(
    state: TurnState,
    call: ToolCall,
    *,
    registry: ToolRegistry,
    llm: Any = None,
    emit=None,
) -> tuple[TurnState, ToolResult]:
    """Run one tool/sub-agent call end-to-end."""
    # Emit loop.iteration BEFORE execution so the UI shows reasoning live.
    if emit is not None:
        try:
            await emit("loop.iteration", {
                "iteration": state.iteration,
                "capability": call.name,
                "args": call.arguments,
                "reasoning": (call.reasoning or "")[:400],
            })
            await emit("tool.call", {
                "name": call.name,
                "args": call.arguments,
                "iteration": state.iteration,
            })
        except Exception:
            _log.warning("emit failed for tool.call", exc_info=True)

    # Pre-hooks (cost guard, restriction).
    pre = run_pre_hooks(state, call)
    if pre.skip and pre.forced_result is not None:
        outcome = pre.forced_result
        result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            status="skipped",
            output=outcome.output,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
        )
        new_state = state.with_tool_call(call).with_tool_result(result)
        run_post_hooks(new_state, call, result)
        if emit is not None:
            try:
                await emit("tool.result", {
                    "name": call.name,
                    "ok": False,
                    "output": outcome.output,
                    "error": outcome.error,
                    "duration_ms": outcome.duration_ms,
                    "skipped_reason": pre.reason,
                })
            except Exception:
                pass
        return new_state, result

    # Execute through the registry.
    ctx = ToolContext(state=state, llm=llm)
    outcome = await registry.execute(call.name, call.arguments, ctx)
    status = "ok" if outcome.ok else "error"
    result = ToolResult(
        call_id=call.call_id,
        name=call.name,
        status=status,
        output=outcome.output,
        error=outcome.error,
        duration_ms=outcome.duration_ms,
    )

    new_state = state.with_tool_call(call).with_tool_result(result)
    # Apply any state updates the tool requested.
    if outcome.state_updates:
        new_state = new_state.apply(**outcome.state_updates)

    run_post_hooks(new_state, call, result)
    if emit is not None:
        try:
            await emit("tool.result", {
                "name": call.name,
                "ok": outcome.ok,
                "output": outcome.output,
                "error": outcome.error,
                "duration_ms": outcome.duration_ms,
            })
        except Exception:
            _log.warning("emit failed for tool.result", exc_info=True)
    return new_state, result


__all__ = ["dispatch"]
