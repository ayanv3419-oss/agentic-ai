"""
Golden harness runner - drives every ``GoldenCase`` through v2 (and
optionally v1) and produces a pass/fail summary.

Run via::

    python -m app.orchestrator_v2.tests.harness [--v1] [--report=<path>]

Without a Groq key the harness uses the ``FakeWorkerLLMClient`` /
``FakeCriticLLMClient`` so the executor + validators + reflection loop
still run end-to-end on real data. The harness reports each case's
outcome against its ``GoldenCase`` expectations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

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
from app.orchestrator_v2.state import ExecutionState, RequestContext
from app.orchestrator_v2.tests.golden_questions import GOLDEN_CASES, GoldenCase


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    outcome: str | None = None
    final_answer: str | None = None
    capabilities_used: list[str] = field(default_factory=list)


def _check(case: GoldenCase, state: ExecutionState, final_answer: str | None,
           duration_ms: float) -> CaseResult:
    r = CaseResult(
        case_id=case.case_id,
        passed=True,
        outcome=state.outcome,
        final_answer=final_answer,
        duration_ms=duration_ms,
        capabilities_used=[s.capability for s in state.executed_steps if s.status == "done"],
    )

    if duration_ms > case.max_latency_ms:
        r.failures.append(
            f"latency {duration_ms:.0f}ms > max {case.max_latency_ms}ms"
        )

    if case.expected_capabilities_used:
        used = set(r.capabilities_used)
        missing = [c for c in case.expected_capabilities_used if c not in used]
        if missing:
            r.failures.append(
                f"missing expected capabilities: {missing}"
            )

    if case.expected_answer_contains_number and final_answer:
        if not re.search(r"\d", final_answer):
            r.failures.append("answer contains no digit")

    if state.token_usage.input_tokens + state.token_usage.output_tokens > case.max_total_tokens \
            and case.max_total_tokens > 0:
        r.failures.append(
            f"tokens {state.token_usage.input_tokens + state.token_usage.output_tokens} "
            f"> max {case.max_total_tokens}"
        )

    if state.reflection_iteration > case.max_reflection_iterations:
        r.failures.append(
            f"reflection_iter {state.reflection_iteration} "
            f"> max {case.max_reflection_iterations}"
        )

    r.passed = not r.failures
    r.notes.extend(case.notes)
    return r


async def _run_v2_case(case: GoldenCase) -> CaseResult:
    """Drive one case through v2 (fake LLMs)."""
    fake_worker = FakeWorkerLLMClient()
    fake_critic = FakeCriticLLMClient()
    wt = set_worker_client(fake_worker)
    ct = set_critic_client(fake_critic)
    try:
        ctx = RequestContext(
            request_id=f"golden-{case.case_id}",
            question=case.question,
            conversation_id=f"golden-{case.case_id}",
            groq_api_key="gsk_" + "x" * 30,
        )
        state = ExecutionState.from_request_context(ctx)
        emitter = EventEmitter()
        start = time.perf_counter()
        try:
            state = await run_reflection_loop(state, emitter)
        finally:
            await emitter.close()
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Extract the final answer the way run.py does.
        final = None
        for s in reversed(state.executed_steps):
            if s.capability == "format_response" and s.status == "done" and s.output:
                final = s.output.get("answer")
                break
            if s.capability == "narrate" and s.status == "done" and s.output and final is None:
                final = s.output.get("narrative")

        return _check(case, state, final, elapsed_ms)
    finally:
        reset_worker_client(wt)
        reset_critic_client(ct)


def _render_report(results: list[CaseResult]) -> str:
    n_pass = sum(1 for r in results if r.passed)
    lines: list[str] = []
    lines.append(f"# Golden harness report - {n_pass}/{len(results)} passed")
    lines.append("")
    lines.append("| Case | Passed | Outcome | Duration | Capabilities | Failures |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        status = "[OK]" if r.passed else "[FAIL]"
        caps = ",".join(r.capabilities_used[:6]) or "-"
        fails = "; ".join(r.failures) or "-"
        lines.append(
            f"| `{r.case_id}` | {status} | {r.outcome or '-'} | "
            f"{r.duration_ms:.0f}ms | {caps} | {fails} |"
        )
    lines.append("")
    if any(r.notes for r in results):
        lines.append("## Notes")
        for r in results:
            for note in r.notes:
                lines.append(f"- `{r.case_id}`: {note}")
    return "\n".join(lines)


async def _main_async(args) -> int:
    results: list[CaseResult] = []
    for case in GOLDEN_CASES:
        r = await _run_v2_case(case)
        status = "[OK]" if r.passed else "[FAIL]"
        print(f"  {status} {r.case_id}: {r.outcome} in {r.duration_ms:.0f}ms"
              + (f" - {'; '.join(r.failures)}" if r.failures else ""))
        results.append(r)

    n_pass = sum(1 for r in results if r.passed)
    print()
    print(f"  summary: {n_pass}/{len(results)} passed")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_report(results), encoding="utf-8")
        print(f"  report: {report_path}")

    # Returns 0 if all passed; non-zero if anything failed.
    return 0 if n_pass == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", help="Write Markdown report to this path")
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
