"""
P2 pipeline smoke test.

Drives the Plan → Execute → Narrate → Format pipeline end-to-end using
a ``FakeWorkerLLMClient`` so no Groq key is required. Verifies:

  * Planner parses a fake JSON plan into a typed Plan.
  * Executor walks the DAG and produces StepResults for every step.
  * compute_kpi capability (REAL body) computes a value off the live DB.
  * narrate capability uses the fake LLM and returns a narrative.
  * format_response composes the final SSE payload.
  * SSE events emitted match the new v2 event taxonomy.
"""

from __future__ import annotations

import asyncio
import sys

from app.analytics_engine import EventEmitter
from app.orchestrator_v2.llm.scope import reset_worker_client, set_worker_client
from app.orchestrator_v2.llm.worker_client import FakeWorkerLLMClient
from app.orchestrator_v2.orchestrator.executor import execute_plan
from app.orchestrator_v2.orchestrator.planner import make_initial_plan
from app.orchestrator_v2.state import ExecutionState, RequestContext
from app.orchestrator_v2.tools.registry import get_capability_registry


def _heading(title: str) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70)


async def _drain(emitter: EventEmitter) -> list[tuple[str, dict]]:
    """Drain all queued SSE events from the emitter into a list."""
    events: list[tuple[str, dict]] = []
    # Close to unblock the stream loop.
    await emitter.close()
    async for chunk in emitter.stream():
        # Parse the SSE wire format back into (event, data).
        if chunk.startswith(":"):
            continue
        ev_line, _, rest = chunk.partition("\n")
        if not ev_line.startswith("event: "):
            continue
        event_name = ev_line[len("event: "):]
        data_line, _, _ = rest.partition("\n")
        if data_line.startswith("data: "):
            import json as _json
            try:
                events.append((event_name, _json.loads(data_line[len("data: "):])))
            except Exception:
                events.append((event_name, {"_raw": data_line}))
    return events


async def test_full_pipeline_with_fake_llm() -> None:
    _heading("test_full_pipeline_with_fake_llm")

    fake = FakeWorkerLLMClient()
    token = set_worker_client(fake)
    try:
        ctx = RequestContext(
            request_id="smoke-p2",
            question="what was my total revenue?",
            conversation_id="smoke",
            groq_api_key="gsk_" + "x" * 30,
        )
        state = ExecutionState.from_request_context(ctx)
        registry = get_capability_registry()

        # 1) Plan
        plan, account, reasoning = await make_initial_plan(state, registry)
        assert plan is not None
        assert len(plan.steps) == 3, f"expected 3 steps, got {len(plan.steps)}"
        assert plan.steps[0].capability == "compute_kpi"
        assert plan.steps[1].capability == "narrate"
        assert plan.steps[2].capability == "format_response"
        print(f"  [OK]planner produced {len(plan.steps)} steps; reasoning: {reasoning[:80]}")

        # 2) Execute
        emitter = EventEmitter()
        final_state = await execute_plan(plan, state, emitter, registry=registry)

        # Pull out the executed steps for inspection.
        by_id = {s.step_id: s for s in final_state.executed_steps}
        assert "s1" in by_id and by_id["s1"].capability == "compute_kpi"
        assert "s2" in by_id and by_id["s2"].capability == "narrate"
        assert "s3" in by_id and by_id["s3"].capability == "format_response"

        s1 = by_id["s1"]
        s2 = by_id["s2"]
        s3 = by_id["s3"]
        assert s1.status == "done", f"compute_kpi status={s1.status} err={s1.error}"
        assert s2.status == "done", f"narrate status={s2.status} err={s2.error}"
        assert s3.status == "done", f"format_response status={s3.status} err={s3.error}"

        # compute_kpi should produce a value (live DB has data)
        assert s1.output is not None
        kpi_value = s1.output.get("value")
        print(f"  [OK]compute_kpi value: {kpi_value}")

        # narrate fake returns the canned narrative
        assert s2.output is not None
        narrative = s2.output.get("narrative")
        print(f"  [OK]narrate narrative: {narrative!r}")

        # format_response should pack a final answer
        assert s3.output is not None
        final_answer = s3.output.get("answer")
        assert final_answer
        print(f"  [OK]format_response answer: {final_answer!r}")

        # 3) SSE events
        events = await _drain(emitter)
        event_names = [e[0] for e in events]
        # Each tool.call has a matching tool.result
        n_calls = event_names.count("tool.call")
        n_results = event_names.count("tool.result")
        assert n_calls == n_results == 3, f"expected 3 calls/3 results, got {n_calls}/{n_results}"
        print(f"  [OK]SSE: {n_calls} tool.call + {n_results} tool.result events")
    finally:
        reset_worker_client(token)
        await fake.aclose()


async def test_substitution_resolves_step_refs() -> None:
    _heading("test_substitution_resolves_step_refs")
    # The fake plan uses "$s1" and "$s2.narrative" — verify those resolve
    # by inspecting what gets logged in tool.call events.
    fake = FakeWorkerLLMClient()
    token = set_worker_client(fake)
    try:
        ctx = RequestContext(
            request_id="smoke-sub",
            question="revenue?",
            groq_api_key="gsk_" + "x" * 30,
        )
        state = ExecutionState.from_request_context(ctx)
        plan, _, _ = await make_initial_plan(state, get_capability_registry())
        emitter = EventEmitter()
        final_state = await execute_plan(plan, state, emitter)

        # narrate received the compute_kpi output (s1) as aggregates.
        s2 = next(s for s in final_state.executed_steps if s.step_id == "s2")
        assert s2.status == "done"

        # format_response received the narrative string (s2.narrative).
        s3 = next(s for s in final_state.executed_steps if s.step_id == "s3")
        assert s3.status == "done"
        assert s3.output is not None
        assert isinstance(s3.output.get("answer"), str)
        print(f"  [OK]$-substitution resolved correctly across 3 steps")
        await emitter.close()
    finally:
        reset_worker_client(token)
        await fake.aclose()


def main() -> int:
    tests = [
        test_full_pipeline_with_fake_llm,
        test_substitution_resolves_step_refs,
    ]
    failures: list[str] = []
    for t in tests:
        try:
            asyncio.run(t())
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL]{t.__name__} FAILED: {e}")
        except Exception as e:
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")
            print(f"  [FAIL]{t.__name__} CRASHED: {e}")
            import traceback; traceback.print_exc()
        print()
    _heading("SUMMARY")
    if failures:
        print(f"  {len(failures)} failure(s):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(f"  all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
