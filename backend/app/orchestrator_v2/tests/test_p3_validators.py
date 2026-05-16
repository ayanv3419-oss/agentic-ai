"""
P3 validator smoke test.

Constructs synthetic ExecutionStates that exercise each validator's
trigger condition, then asserts the validator chain raises the expected
ValidationFailure.
"""

from __future__ import annotations

import sys

from app.orchestrator_v2.state import (
    ExecutionState,
    Plan,
    PlanStep,
    RequestContext,
    StepResult,
)
from app.orchestrator_v2.validators.base import run_validators, get_validator_registry


def _heading(t: str) -> None:
    print("=" * 70)
    print(t)
    print("=" * 70)


def _state(question: str = "test", plan: Plan | None = None, steps: tuple[StepResult, ...] = ()) -> ExecutionState:
    ctx = RequestContext(
        request_id="r",
        question=question,
        groq_api_key="gsk_" + "x" * 30,
    )
    base = ExecutionState.from_request_context(ctx)
    if plan is not None:
        base = base.apply(plan=plan)
    if steps:
        base = base.apply(executed_steps=steps)
    return base


def test_registry_has_all_eight() -> None:
    _heading("test_registry_has_all_eight")
    reg = get_validator_registry()
    expected = {
        "sql_validator",
        "schema_validator",
        "empty_result_validator",
        "timeframe_validator",
        "chart_shape_validator",
        "aggregation_validator",
        "business_rule_validator",
        "grounded_narrative_validator",
    }
    assert set(reg.names) == expected, f"got {reg.names}"
    print(f"  [OK]{len(reg.names)} validators registered: {reg.names}")


def test_passing_state_has_no_failures() -> None:
    _heading("test_passing_state_has_no_failures")
    plan = Plan(
        root_question="revenue?",
        steps=(
            PlanStep(step_id="s1", capability="compute_kpi"),
            PlanStep(step_id="s2", capability="narrate", depends_on=("s1",)),
            PlanStep(step_id="s3", capability="format_response", depends_on=("s2",)),
        ),
    )
    steps = (
        StepResult(step_id="s1", capability="compute_kpi", status="done",
                   output={"value": 100.0, "format": "currency"}),
        StepResult(step_id="s2", capability="narrate", status="done",
                   output={"narrative": "Total revenue is 100.", "grounded_numbers": [100.0]}),
        StepResult(step_id="s3", capability="format_response", status="done",
                   output={"answer": "Total revenue is 100.", "chart": None, "mode": "summary"}),
    )
    report = run_validators(_state(plan=plan, steps=steps))
    assert report.passed, f"expected pass, got failures: {[f.description for f in report.failures]}"
    print(f"  [OK]clean state: 0 failures")


def test_failed_step_detected() -> None:
    _heading("test_failed_step_detected")
    steps = (
        StepResult(step_id="s1", capability="compute_kpi", status="failed",
                   error="kpi not found: nope"),
    )
    report = run_validators(_state(steps=steps))
    assert not report.passed
    aspects = {f.aspect for f in report.failures}
    assert "failed_tool" in aspects, aspects
    print(f"  [OK]failed_tool aspect surfaced from schema_validator")


def test_missing_timeframe_detected() -> None:
    _heading("test_missing_timeframe_detected")
    plan = Plan(
        root_question="revenue last week?",
        steps=(
            PlanStep(step_id="s1", capability="run_data_query", args={"intent": "summary"}),
        ),
    )
    report = run_validators(_state(plan=plan))
    aspects = {f.aspect for f in report.failures}
    assert "missing_timeframe" in aspects, aspects
    print(f"  [OK]missing_timeframe surfaced when run_data_query is used without resolve_time_window")


def test_empty_result_unexplained() -> None:
    _heading("test_empty_result_unexplained")
    steps = (
        StepResult(step_id="s1", capability="run_data_query", status="done",
                   output={"items": [], "series": [], "totals": None, "empty_reason": None}),
    )
    report = run_validators(_state(steps=steps))
    aspects = {f.aspect for f in report.failures}
    assert "empty_result_unexplained" in aspects, aspects
    print(f"  [OK]empty_result_unexplained surfaced for silent zero-rows")


def test_grounded_narrative_catches_hallucination() -> None:
    _heading("test_grounded_narrative_catches_hallucination")
    # compute_kpi says 100; narrative claims 999 (hallucinated)
    steps = (
        StepResult(step_id="s1", capability="compute_kpi", status="done",
                   output={"value": 100.0, "format": "currency"}),
        StepResult(step_id="s2", capability="narrate", status="done",
                   output={"narrative": "Total revenue is 999.", "grounded_numbers": []}),
    )
    report = run_validators(_state(steps=steps))
    aspects = {f.aspect for f in report.failures}
    assert "no_supporting_evidence" in aspects, aspects
    print(f"  [OK]hallucinated 999 (vs ground truth 100) caught by grounded_narrative_validator")


def test_grounded_narrative_passes_when_grounded() -> None:
    _heading("test_grounded_narrative_passes_when_grounded")
    steps = (
        StepResult(step_id="s1", capability="compute_kpi", status="done",
                   output={"value": 46830.5, "format": "currency"}),
        StepResult(step_id="s2", capability="narrate", status="done",
                   output={"narrative": "Total revenue is 46830.5.", "grounded_numbers": [46830.5]}),
    )
    report = run_validators(_state(steps=steps))
    nse = [f for f in report.failures if f.aspect == "no_supporting_evidence"]
    assert not nse, f"unexpected hallucination flags: {[f.description for f in nse]}"
    print(f"  [OK]grounded narrative passes (46830.5 matches s1.value)")


def test_chart_shape_empty_dict_flagged() -> None:
    _heading("test_chart_shape_empty_dict_flagged")
    steps = (
        StepResult(step_id="s1", capability="format_response", status="done",
                   output={"answer": "x", "chart": {}, "mode": "summary"}),
    )
    report = run_validators(_state(steps=steps))
    aspects = {f.aspect for f in report.failures}
    assert "chart_data_invalid" in aspects
    print(f"  [OK]empty chart dict flagged")


def test_business_rule_margin_check() -> None:
    _heading("test_business_rule_margin_check")
    # revenue 100, cost 60 → margin SHOULD be 40; claim 50 → fail
    steps = (
        StepResult(step_id="s1", capability="run_data_query", status="done",
                   output={"totals": {"revenue": 100.0, "cost": 60.0, "margin": 50.0}}),
    )
    report = run_validators(_state(steps=steps))
    aspects = {f.aspect for f in report.failures}
    assert "inconsistent_business_logic" in aspects
    print(f"  [OK]margin = revenue - cost rule fired (50 != 100 - 60)")


def main() -> int:
    tests = [
        test_registry_has_all_eight,
        test_passing_state_has_no_failures,
        test_failed_step_detected,
        test_missing_timeframe_detected,
        test_empty_result_unexplained,
        test_grounded_narrative_catches_hallucination,
        test_grounded_narrative_passes_when_grounded,
        test_chart_shape_empty_dict_flagged,
        test_business_rule_margin_check,
    ]
    failures: list[str] = []
    for t in tests:
        try:
            t()
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
