"""SubAgent base — fixed deterministic tool sequence per agent.

Each sub-agent declares a `pipeline` — a list of `(tool_name, args_dict_or_callable)`
entries. The base `run()` walks the pipeline, applies tool state_updates,
records every tool call, and emits SSE events via the supplied emitter.
"""
from __future__ import annotations

import logging
from abc import ABC
from typing import Any, Callable, Sequence, Union

from app.state import TurnState, ToolCallRecord
from app.streaming import EventEmitter
from app.tools import get_registry
from app.tools.base import ToolResult

log = logging.getLogger("agentic_ai.agents")


# Each step is either:
#   ("ToolName", {"arg": value})
#   ("ToolName", lambda state: {"arg": ...})  # dynamic args from current state
PipelineStep = tuple[str, Union[dict, Callable[[TurnState], dict]]]


class SubAgent(ABC):
    name: str = ""
    pipeline: Sequence[PipelineStep] = ()

    async def run(
        self,
        state: TurnState,
        emit: EventEmitter,
    ) -> TurnState:
        registry = get_registry()
        for tool_name, args_spec in self.pipeline:
            args = args_spec(state) if callable(args_spec) else dict(args_spec)
            iteration = state.iteration + 1
            state = state.apply(iteration=iteration)
            await emit.emit("tool.call", {
                "name": tool_name, "args": args, "iteration": iteration,
            })
            result: ToolResult = await registry.execute(tool_name, args, state)
            await emit.emit("tool.result", {
                "name": tool_name,
                "ok": result.ok,
                "output": result.output,
                "error": result.error,
                "duration_ms": round(result.duration_ms, 2),
            })
            record = ToolCallRecord(
                name=tool_name,
                args=args,
                output=result.output,
                ok=result.ok,
                error=result.error,
                duration_ms=result.duration_ms,
                iteration=iteration,
            )
            state = state.append_tool_call(record)
            if not result.ok:
                state = state.append_error(f"{tool_name}: {result.error}")
                # Halt the pipeline on first failure — strict mode.
                return state
            if result.state_updates:
                state = state.apply(**result.state_updates)
            if result.delta_metrics:
                state = state.apply(
                    tokens_in=state.tokens_in + int(result.delta_metrics.get("tokens_in", 0)),
                    tokens_out=state.tokens_out + int(result.delta_metrics.get("tokens_out", 0)),
                )
        return state
