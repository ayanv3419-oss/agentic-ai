"""
Retry policy for capability invocations.

Only **transient** infrastructure failures are retried — typically a
network blip or a 5xx from the LLM provider. Semantic failures (wrong
SQL, empty result, invalid args) are NOT retried; the reflection loop
handles those at a higher level.

The retry budget is per-capability per plan-step (not per turn).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.orchestrator_v2.execution.timeout_manager import execute_with_timeout
from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability, CapabilityResult

log = logging.getLogger("orchestrator_v2.execution.retry")


# Substrings in the ``error`` field that mark a failure as transient
# (worth retrying). Anything not in this set is treated as terminal.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "network",
    "connection",
    "5xx",
    "rate_limit",
    "429",
    "upstream",
    "temporarily",
)


def _is_transient(result: CapabilityResult) -> bool:
    if result.ok:
        return False
    if "timeout" in result.notes:
        return True
    err = (result.error or "").lower()
    return any(marker in err for marker in _TRANSIENT_MARKERS)


async def execute_with_retry(
    cap: Capability,
    state: ExecutionState,
    raw_args: dict[str, Any],
    *,
    max_attempts: int = 2,
    base_delay_sec: float = 0.4,
) -> CapabilityResult:
    """
    Run ``execute_with_timeout`` up to ``max_attempts`` times. Backoff
    is exponential: 0.4s, 0.8s, 1.6s, ...

    ``max_attempts=1`` means no retry (single try).
    """
    last_result: CapabilityResult | None = None
    for attempt in range(1, max_attempts + 1):
        result = await execute_with_timeout(cap, state, raw_args)
        if result.ok or not _is_transient(result):
            return result
        last_result = result
        if attempt < max_attempts:
            delay = base_delay_sec * (2 ** (attempt - 1))
            log.info(
                "capability %s transient failure (%s); retry %d/%d after %.2fs",
                cap.name, result.error, attempt + 1, max_attempts, delay,
            )
            await asyncio.sleep(delay)
    return last_result or CapabilityResult(
        ok=False,
        error="retry budget exhausted",
        duration_ms=0.0,
        notes=("retry_exhausted",),
    )


__all__ = ["execute_with_retry"]
