"""Concrete background tasks.

`enqueue_ingest_upload` is the typed wrapper that the upload route calls.
Today it routes through `InlineJobQueue` and runs `DataCleanAgent` in the
calling task; a future arq-backed deployment runs the same task in a
separate worker process — call site doesn't change.

Task signatures: `async def fn(**kwargs) -> dict | None`. The dict (when
returned) becomes `JobStatus.result`. Tasks should be idempotent because
arq retries failed jobs; today's inline impl doesn't, but that may change.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.workers.queue import get_job_queue, task


_log = logging.getLogger("agentic_ai.workers.tasks")


# ---- ingest_upload ------------------------------------------------------

@task("ingest_upload")
async def _ingest_upload(
    *,
    tmp_path: str,
    filename: str,
    target: str,
    batch_id: str,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Run the DataCleanAgent ingestion pipeline. Wrapped in this task so
    the SAME path is used by both inline + arq workers — when arq is
    enabled, this body runs in the worker process untouched.

    Returns the DataCleanAgent payload (rows_inserted / rows_failed /
    summary / etc) for caller-side display."""
    # Local imports to avoid an import cycle at module load — analytics_engine
    # imports from infrastructure, infrastructure does not import workers.
    from app.analytics_engine import DataCleanAgent
    agent = DataCleanAgent()
    result = await agent.run(
        tmp_path=Path(tmp_path),
        filename=filename,
        target=target,
        batch_id=batch_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return result


async def enqueue_ingest_upload(
    *,
    tmp_path: str,
    filename: str,
    target: str,
    batch_id: str,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Typed enqueue helper. Returns the job_id; today (inline mode) the
    job has already finished by the time this returns + the result is
    queryable via `get_job_queue().status(job_id)`. With arq, this returns
    immediately and a worker picks the job up."""
    queue = get_job_queue()
    return await queue.enqueue(
        "ingest_upload",
        tmp_path=tmp_path,
        filename=filename,
        target=target,
        batch_id=batch_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )


# ---- Idempotent registration ------------------------------------------

def register_default_tasks() -> None:
    """Importing this module already registered the tasks via the @task
    decorator. This function exists so the FastAPI startup can be explicit
    about the registration step (rather than relying on import side
    effects). Idempotent — re-importing tasks doesn't re-register because
    @task raises on duplicate names; we catch + log instead."""
    # The decorators above have already populated the registry. This is a
    # marker so callers know the import succeeded.
    _log.info("workers: default tasks registered")
