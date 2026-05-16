"""
orchestrator_v2
===============

Reflective multi-stage agentic orchestration for Agentic AI.

This package replaces the AgenticLoop + deterministic-intent dispatch tiers
inside ``app.analytics_engine`` with a modular **Worker / Critic / Validator**
architecture that runs a bounded reflection loop. The deterministic front
door (cache, clarification, KPI fast-path) stays unchanged in front of v2.

Layout
------

* ``run.py`` — single entry point ``run_query_turn_v2``; safety constants.
* ``state.py`` — frozen Pydantic state models (``ExecutionState``, ``Plan``,
  ``CriticFeedback``, ``ValidationReport``, ``ConfidenceScores``…).
* ``front_door.py`` — cache + clarification + KPI fast-path short-circuits.
* ``orchestrator/`` — planner, executor, reflection loop, confidence.
* ``agents/`` — Worker (plans + narrates) and Critic (evaluates).
* ``tools/`` — capabilities (Planner-visible) over primitives (hidden).
* ``validators/`` — deterministic post-execution checks.
* ``memory/`` — conversation, execution, context-trimming layers.
* ``execution/`` — retry, timeout, parallelism helpers.
* ``llm/`` — Worker/Critic LLM clients + prompt templates + token ledger.
* ``monitoring/`` — tracing, telemetry, metrics, audit log.
* ``tests/`` — unit tests + golden harness.

Activation is gated by the ``ORCHESTRATOR_VERSION`` env (default ``v1``)
and the per-request ``X-Orchestrator-Version`` header. v1 stays the active
path until v2 passes the golden harness and shadow-traffic gates.
"""

__all__ = ["run_query_turn_v2"]

from app.orchestrator_v2.run import run_query_turn_v2  # noqa: E402
