"""Job queue protocol + in-process inline implementation.

Design:
  - `JobQueue.enqueue(name, **kwargs) -> job_id`
       schedules a job. The InlineJobQueue runs it immediately in the
       calling task; the future ArqJobQueue ships it to a Redis-backed
       worker pool and returns a tracking id.
  - `JobQueue.status(job_id) -> JobStatus`
       inspect — InlineJobQueue keeps a small in-memory ring buffer; the
       Redis impl reads from a Redis hash.
  - Tasks are registered by name via a decorator; the queue dispatches by
       name so call sites don't import worker modules directly.

Inline mode is fine for:
  - ≤ 50 MB uploads (the current cap)
  - low concurrency (one or two simultaneous uploads)
  - dev / single-machine deployments

When you need real concurrency / non-blocking uploads:
  - install `arq` + provision Redis
  - implement `ArqJobQueue(JobQueue)` (sketch below)
  - call `set_job_queue(ArqJobQueue(redis_url))` from FastAPI startup
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import uuid4


_log = logging.getLogger("agentic_ai.workers.queue")


@dataclass
class JobStatus:
    """Snapshot of a queued job. Returned by `JobQueue.status`."""
    job_id: str
    name: str
    state: str  # "queued" | "running" | "ok" | "failed"
    enqueued_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    progress: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        elapsed = None
        if self.started_at:
            end = self.finished_at or time.time()
            elapsed = round(end - self.started_at, 3)
        return {
            "job_id":       self.job_id,
            "name":         self.name,
            "state":        self.state,
            "enqueued_at":  self.enqueued_at,
            "started_at":   self.started_at,
            "finished_at":  self.finished_at,
            "elapsed_s":    elapsed,
            "result":       self.result,
            "error":        self.error,
            "progress":     dict(self.progress),
        }


# ---- Task registry ------------------------------------------------------

# Module-level so `@task("name")` decorators in tasks.py register at import
# time. Task callable signature: `async def fn(**kwargs) -> dict | None`.
_TASKS: dict[str, Callable[..., Awaitable[Optional[dict[str, Any]]]]] = {}


def task(name: str) -> Callable[
    [Callable[..., Awaitable[Optional[dict[str, Any]]]]],
    Callable[..., Awaitable[Optional[dict[str, Any]]]],
]:
    """Decorator to register a task by name. The queue dispatcher looks up
    by this name when a job is dequeued."""
    def deco(fn):
        if name in _TASKS:
            raise RuntimeError(f"task already registered: {name!r}")
        _TASKS[name] = fn
        return fn
    return deco


def get_task(name: str) -> Callable[..., Awaitable[Optional[dict[str, Any]]]]:
    if name not in _TASKS:
        raise KeyError(f"unknown task: {name!r}")
    return _TASKS[name]


# ---- Protocol -----------------------------------------------------------

class JobQueue(Protocol):
    backend_kind: str

    async def enqueue(self, name: str, /, **kwargs: Any) -> str: ...   # noqa: E704
    def status(self, job_id: str) -> Optional[JobStatus]: ...   # noqa: E704
    async def close(self) -> None: ...   # noqa: E704


# ---- Inline (synchronous) impl -----------------------------------------

class InlineJobQueue:
    """Runs jobs synchronously in the calling task. Perfect for dev /
    small deployments. The job_id is stable so a client can poll status
    even though it'll always come back ok-or-failed immediately."""

    backend_kind = "inline"

    _MAX_HISTORY = 256  # ring-buffer cap so memory doesn't grow unbounded

    def __init__(self) -> None:
        self._history: OrderedDict[str, JobStatus] = OrderedDict()
        self._lock = threading.Lock()

    async def enqueue(self, name: str, /, **kwargs: Any) -> str:
        fn = get_task(name)  # raises KeyError if unknown
        job_id = str(uuid4())
        now = time.time()
        status = JobStatus(
            job_id=job_id, name=name, state="running",
            enqueued_at=now, started_at=now,
        )
        with self._lock:
            if len(self._history) >= self._MAX_HISTORY:
                self._history.popitem(last=False)
            self._history[job_id] = status
        try:
            result = await fn(**kwargs)
        except Exception as exc:
            status.state = "failed"
            status.error = f"{type(exc).__name__}: {exc}"
            status.finished_at = time.time()
            _log.exception("inline job %s failed: %s", name, exc)
            raise
        else:
            status.state = "ok"
            status.result = result if isinstance(result, dict) else (
                {"result": result} if result is not None else None
            )
            status.finished_at = time.time()
            return job_id

    def status(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            return self._history.get(job_id)

    async def close(self) -> None:
        pass


# ---- Sketch: Arq-backed real queue --------------------------------------

class ArqJobQueue:
    """Future real-worker backend. Today this is a stub — instantiation
    raises NotImplementedError until the migration is performed.

    Implementation outline (left as a recipe for the migration):

      1. `pip install arq`
      2. Create `arq_settings.py` with a WorkerSettings class enumerating
         the same task names as `_TASKS`.
      3. Replace this body with:
            from arq import create_pool
            self._pool = await create_pool(redis_url)
            ...
            await self._pool.enqueue_job(name, **kwargs)
      4. Status: arq writes results to Redis under known keys; pull from
         there into a JobStatus.
      5. From FastAPI startup, call set_job_queue(ArqJobQueue(redis_url)).
      6. Run the worker process: `arq backend.app.workers.arq_settings.WorkerSettings`
    """

    backend_kind = "arq"

    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise NotImplementedError(
                "ArqJobQueue requires a Redis URL. Set REDIS_URL and provision "
                "Redis. See app/workers/queue.py for the migration recipe."
            )
        # Real implementation will live here when REDIS_URL is provisioned.
        raise NotImplementedError(
            "ArqJobQueue is a sketch — install arq + implement per the recipe in "
            "app/workers/queue.py"
        )

    async def enqueue(self, name: str, /, **kwargs: Any) -> str:  # pragma: no cover
        raise NotImplementedError

    def status(self, job_id: str) -> Optional[JobStatus]:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


# ---- Singleton ---------------------------------------------------------

_QUEUE: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = InlineJobQueue()
        _log.info("job queue initialized: %s", _QUEUE.backend_kind)
    return _QUEUE


def set_job_queue(queue: JobQueue) -> None:
    """Swap the active backend. Tests + the future ArqJobQueue migration
    use this. The inline impl is the safe default."""
    global _QUEUE
    _QUEUE = queue
    _log.info("job queue swapped: %s", queue.backend_kind)
