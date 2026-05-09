"""Background-worker abstraction.

Today's deployment runs jobs INLINE inside the request handler — that's
fine for small uploads + low concurrency. The interface below is shaped
like arq / RQ so the day Redis is provisioned, swapping in a real worker
is a single `set_job_queue(ArqJobQueue(redis_url))` call. No call-site
changes.

Public surface:
  - `JobQueue` Protocol        — what every backend implements
  - `InlineJobQueue`           — runs jobs synchronously in-process (today)
  - `ArqJobQueue`              — sketch for a future real worker; raises
                                  NotImplementedError until Redis is wired
  - `enqueue_ingest_upload`    — typed wrapper over the generic enqueue
  - `get_job_queue`/`set_job_queue` — singleton swap
  - `JobStatus`                — payload shape returned by status checks
"""
from app.workers.queue import (
    InlineJobQueue,
    JobQueue,
    JobStatus,
    get_job_queue,
    set_job_queue,
)
from app.workers.tasks import (
    enqueue_ingest_upload,
    register_default_tasks,
)

__all__ = [
    "InlineJobQueue",
    "JobQueue",
    "JobStatus",
    "enqueue_ingest_upload",
    "get_job_queue",
    "register_default_tasks",
    "set_job_queue",
]
