"""Tool ABC + ToolResult.

Every tool is pure: takes (state, args), returns a ToolResult. The agent /
coordinator merges `state_updates` into the next TurnState.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from app.state import TurnState

log = logging.getLogger("agentic_ai.tools")


class ToolResult(BaseModel):
    ok: bool
    output: Any = None
    state_updates: dict[str, Any] = {}
    delta_metrics: dict[str, float] = {}
    error: str | None = None
    duration_ms: float = 0.0


class Tool(ABC):
    name: str = ""
    description: str = ""
    args_model: type[BaseModel] = BaseModel
    independent: bool = True

    @abstractmethod
    async def run(self, state: TurnState, args: BaseModel) -> ToolResult:
        ...

    async def execute(self, state: TurnState, raw_args: dict[str, Any]) -> ToolResult:
        """Validate args, run, never raise. Sets duration_ms."""
        start = time.perf_counter()
        try:
            args = self.args_model(**(raw_args or {}))
        except Exception as e:
            log.warning("tool %s arg validation failed: %s", self.name, e)
            return ToolResult(
                ok=False,
                error=f"Invalid args for {self.name}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        try:
            result = await self.run(state, args)
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        except Exception as e:
            log.exception("tool %s failed for turn %s", self.name, state.turn_id)
            return ToolResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - start) * 1000,
            )


_SENTINEL = object()


def require(state: TurnState, *fields: str) -> ToolResult | None:
    """Helper: ensures predecessor TurnState fields exist; returns a failing
    ToolResult on miss, or None on success.

    `None` (or attribute missing) is treated as "tool didn't run yet".
    An empty list / dict / string is treated as a VALID successful result —
    e.g. SqlExecutor legitimately returns `state.rows = []` when the SQL
    matched 0 rows. Downstream tools must handle that case themselves."""
    for f in fields:
        v = getattr(state, f, _SENTINEL)
        if v is _SENTINEL or v is None:
            return ToolResult(
                ok=False,
                error=f"prerequisite '{f}' not set in TurnState",
            )
    return None
