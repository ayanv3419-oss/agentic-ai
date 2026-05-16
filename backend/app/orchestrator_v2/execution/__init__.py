"""
execution/
==========

Mechanical execution primitives used by the Executor.

  - ``retry_manager.py``   — per-capability retry budget (transient failures
                             only — never retries semantic errors).
  - ``timeout_manager.py`` — per-capability and per-turn timeout enforcement
                             via ``asyncio.wait_for``.
  - ``parallel.py``        — ``asyncio.gather`` helpers for independent DAG
                             branches; bounded by ``MAX_PARALLEL_CAPABILITIES``.

The DAG runner itself lives in ``orchestrator/executor.py``; this package
provides only the safety primitives it composes.
"""
