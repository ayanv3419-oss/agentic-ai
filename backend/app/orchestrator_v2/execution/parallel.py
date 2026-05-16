"""
Parallel-execution helper for the DAG Executor.

When the Planner emits multiple steps with the same ``parallel_group``
tag and no inter-dependencies, the Executor runs them concurrently via
``asyncio.gather`` — bounded by ``MAX_PARALLEL_CAPABILITIES``.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

from app.orchestrator_v2.run import MAX_PARALLEL_CAPABILITIES

T = TypeVar("T")


async def gather_bounded(
    coros: list[Awaitable[T]],
    *,
    limit: int | None = None,
) -> list[T]:
    """
    Run all ``coros`` concurrently, but with a concurrency cap. Preserves
    input order in the returned list.
    """
    cap = limit if limit is not None else MAX_PARALLEL_CAPABILITIES
    cap = max(1, cap)
    sem = asyncio.Semaphore(cap)

    async def _run_with_sem(coro: Awaitable[T]) -> T:
        async with sem:
            return await coro

    return await asyncio.gather(*(_run_with_sem(c) for c in coros))


__all__ = ["gather_bounded"]
