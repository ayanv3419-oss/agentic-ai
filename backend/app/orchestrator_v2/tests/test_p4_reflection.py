"""
P4 reflection loop smoke test.

Drives the full Plan → Execute → Validate → Critic → (delta plan) loop
with fake LLM clients so no Groq key is required.

Tests:

  1. Critic accepts on first pass → loop returns ``accepted`` after 1
     iteration with no delta plan.
  2. Critic rejects first pass, accepts after delta → loop returns
     ``accepted`` after 2 iterations.
  3. Critic always rejects → loop returns ``escalated`` after
     MAX_REFLECTION_LOOPS iterations.
  4. Validator-blocking failure with retries remaining → reflect.
"""

from __future__ import annotations

import asyncio
import sys

from app.analytics_engine import EventEmitter
from app.orchestrator_v2.llm.critic_client import FakeCriticLLMClient
from app.orchestrator_v2.llm.scope import (
    reset_critic_client,
    reset_worker_client,
    set_critic_client,
    set_worker_client,
)
from app.orchestrator_v2.llm.worker_client import FakeWorkerLLMClient
from app.orchestrator_v2.orchestrator.reflection_loop import run_reflection_loop
from app.orchestrator_v2.run import MAX_REFLECTION_LOOPS
from app.orchestrator_v2.state import ExecutionState, RequestContext


def _heading(t: str) -> None:
    print("=" * 70)
    print(t)
    print("=" * 70)


def _new_state() -> ExecutionState:
    ctx = RequestContext(
        request_id="r",
        question="what was my total revenue?",
        conversation_id="c1",
        groq_api_key="gsk_" + "x" * 30,
    )
    return ExecutionState.from_request_context(ctx)


async def test_critic_accepts_first_pass() -> None:
    _heading("test_critic_accepts_first_pass")
    worker = FakeWorkerLLMClient()
    critic = FakeCriticLLMClient()  # default accept
    wt = set_worker_client(worker)
    ct = set_critic_client(critic)
    try:
        emitter = EventEmitter()
        state = await run_reflection_loop(_new_state(), emitter)
        await emitter.close()
        assert state.outcome == "accepted", f"outcome={state.outcome}"
        # exactly one critic verdict in history
        assert len(state.critic_history) == 1
        assert state.critic_history[0].is_acceptable is True
        # no delta plan iteration
        assert state.reflection_iteration == 0
        print(f"  [OK]accepted on first pass; "
              f"confidence={state.confidence.overall:.3f}")
    finally:
        reset_worker_client(wt)
        reset_critic_client(ct)


async def test_critic_rejects_then_accepts() -> None:
    _heading("test_critic_rejects_then_accepts")
    # Critic rejects with one blocking issue on first call, accepts after.
    rejecting = FakeCriticLLMClient(verdict={
        "is_acceptable": False,
        "confidence": 0.4,
        "summary": "missing comparison",
        "issues": [{
            "aspect": "missing_comparison",
            "severity": "blocking",
            "description": "user asked for vs last month but no comparison ran",
            "target_capability": "compare_periods",
        }],
    })

    # We need the critic to flip its verdict after the first call.
    class FlippingCritic:
        def __init__(self) -> None:
            self.count = 0
        async def complete_json(self, sys, user, *, temperature=0.0, max_tokens=1024):
            from app.orchestrator_v2.llm.token_ledger import CallAccount
            self.count += 1
            if self.count == 1:
                return rejecting.verdict, CallAccount.from_counts(50, 50)
            return FakeCriticLLMClient.ACCEPT, CallAccount.from_counts(50, 30)
        async def aclose(self) -> None: return None

    # Worker produces the same plan twice (fake doesn't keep state); the
    # delta plan from FakeWorker re-uses DEFAULT_PLAN, so the executor
    # will see steps already done and skip re-running them.
    worker = FakeWorkerLLMClient()
    critic = FlippingCritic()
    wt = set_worker_client(worker)
    ct = set_critic_client(critic)
    try:
        emitter = EventEmitter()
        state = await run_reflection_loop(_new_state(), emitter)
        await emitter.close()
        # The critic flipped at iteration 1 → accepted then.
        assert state.outcome == "accepted", f"outcome={state.outcome}"
        assert len(state.critic_history) == 2
        assert state.critic_history[0].is_acceptable is False
        assert state.critic_history[1].is_acceptable is True
        print(f"  [OK]rejected first pass, accepted after delta; "
              f"iterations={state.reflection_iteration + 1}")
    finally:
        reset_worker_client(wt)
        reset_critic_client(ct)


async def test_critic_always_rejects_escalates() -> None:
    _heading("test_critic_always_rejects_escalates")
    persistent_reject = FakeCriticLLMClient(verdict={
        "is_acceptable": False,
        "confidence": 0.2,
        "summary": "still missing comparison",
        "issues": [{
            "aspect": "missing_comparison",
            "severity": "blocking",
            "description": "still no comparison",
            "target_capability": "compare_periods",
        }],
    })
    worker = FakeWorkerLLMClient()
    critic = persistent_reject
    wt = set_worker_client(worker)
    ct = set_critic_client(critic)
    try:
        emitter = EventEmitter()
        state = await run_reflection_loop(_new_state(), emitter)
        await emitter.close()
        # MAX_REFLECTION_LOOPS critic verdicts; outcome should be escalated.
        assert state.outcome == "escalated", f"outcome={state.outcome}"
        assert len(state.critic_history) == MAX_REFLECTION_LOOPS
        print(f"  [OK]escalated after {MAX_REFLECTION_LOOPS} reflection iterations; "
              f"confidence={state.confidence.overall:.3f}")
    finally:
        reset_worker_client(wt)
        reset_critic_client(ct)


async def test_confidence_scoring_present_on_accept() -> None:
    _heading("test_confidence_scoring_present_on_accept")
    worker = FakeWorkerLLMClient()
    critic = FakeCriticLLMClient()
    wt = set_worker_client(worker)
    ct = set_critic_client(critic)
    try:
        emitter = EventEmitter()
        state = await run_reflection_loop(_new_state(), emitter)
        await emitter.close()
        assert state.confidence is not None
        for dim in ("completeness", "data", "tool", "validation", "reasoning"):
            v = getattr(state.confidence, dim)
            assert 0.0 <= v <= 1.0, f"{dim}={v}"
        ovr = state.confidence.overall
        assert 0.0 <= ovr <= 1.0
        print(f"  [OK]all 5 confidence dimensions in [0,1]; overall={ovr:.3f}")
    finally:
        reset_worker_client(wt)
        reset_critic_client(ct)


def main() -> int:
    tests = [
        test_critic_accepts_first_pass,
        test_critic_rejects_then_accepts,
        test_critic_always_rejects_escalates,
        test_confidence_scoring_present_on_accept,
    ]
    failures: list[str] = []
    for t in tests:
        try:
            asyncio.run(t())
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL]{t.__name__}: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")
        print()
    _heading("SUMMARY")
    if failures:
        print(f"  {len(failures)} failure(s):")
        for f in failures: print(f"    - {f}")
        return 1
    print(f"  all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
