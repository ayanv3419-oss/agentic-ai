"""
Timeout enforcement for capability invocations.

Per-capability timeout: ``asyncio.wait_for`` around ``capability.execute``.
A timeout produces a typed ``CapabilityResult(ok=False, error="timeout")``
so the Executor treats it uniformly with any other failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.orchestrator_v2.run import PER_CAPABILITY_TIMEOUT_SEC
from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability, CapabilityResult

log = logging.getLogger("orchestrator_v2.execution.timeout")


async def execute_with_timeout(
    cap: Capability,
    state: ExecutionState,
    raw_args: dict[str, Any],
    *,
    timeout_sec: float | None = None,
) -> CapabilityResult:
    """
    Wrap ``capability.execute`` with ``asyncio.wait_for``. On timeout,
    return a ``CapabilityResult`` with ``ok=False`` and the wall-clock
    elapsed time as ``duration_ms``.
    """
    deadline = timeout_sec if timeout_sec is not None else PER_CAPABILITY_TIMEOUT_SEC
    start = time.perf_counter()
    try:
        return await asyncio.wait_for(cap.execute(state, raw_args), timeout=deadline)
    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.warning(
            "capability %s timed out after %.1fs",
            cap.name, deadline,
        )
        return CapabilityResult(
            ok=False,
            error=f"timeout after {deadline:.1f}s",
            duration_ms=elapsed_ms,
            notes=("timeout",),
        )


__all__ = ["execute_with_timeout"]
