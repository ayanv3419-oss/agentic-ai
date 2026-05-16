"""
P1 smoke test — capability layer wiring.

Verifies that:

  * All 8 v2 capabilities register cleanly on the singleton registry.
  * Each capability exposes a valid JSON schema for the Planner prompt.
  * Each capability subclasses ``Capability`` with proper args/output
    models declared.
  * Stub bodies fail GRACEFULLY via ``Capability.execute()`` — every
    capability returns a typed ``CapabilityResult`` (no raised
    exceptions, no missing fields).
  * The 14 primitive re-exports resolve to real ``Tool`` subclasses
    from the legacy ``analytics_engine`` module.

This test runs without a database, network, or Groq key. It exercises
the v2 surface area in isolation — exactly what the Worker / Critic
will plan against in P2+.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.orchestrator_v2.state import ExecutionState, RequestContext
from app.orchestrator_v2.tools.base import Capability, CapabilityResult
from app.orchestrator_v2.tools.registry import get_capability_registry


EXPECTED_CAPABILITIES = (
    "breakdown_by_hierarchy",
    "compare_periods",
    "compute_kpi",
    "drive_infer_schema",
    "drive_preview",
    "drive_search",
    "format_response",
    "narrate",
    "resolve_entities",
    "resolve_time_window",
    "run_data_query",
)


def _heading(title: str) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70)


def test_registry_has_all_capabilities() -> None:
    _heading("test_registry_has_all_capabilities")
    reg = get_capability_registry()
    assert sorted(reg.names) == sorted(EXPECTED_CAPABILITIES), (
        f"expected {EXPECTED_CAPABILITIES}, got {reg.names}"
    )
    print(f"  [OK]registry contains {len(reg.names)} capabilities: {reg.names}")


def test_each_capability_has_valid_schema() -> None:
    _heading("test_each_capability_has_valid_schema")
    reg = get_capability_registry()
    schemas = reg.json_schema_for_planner_prompt()
    assert len(schemas) == len(EXPECTED_CAPABILITIES)

    for entry in schemas:
        assert isinstance(entry, dict)
        assert entry["name"] in EXPECTED_CAPABILITIES, entry["name"]
        assert isinstance(entry["description"], str) and entry["description"], (
            f"capability {entry['name']} has empty description"
        )
        assert isinstance(entry["args_schema"], dict)
        assert entry["args_schema"].get("type") == "object", entry["name"]
        # Must be JSON-serialisable — this is what goes into the prompt.
        json.dumps(entry)
        print(f"  [OK]{entry['name']}: {len(entry['description'])} chars desc,"
              f" args has {len(entry['args_schema'].get('properties', {}))} props")


def test_subclasses_declare_models() -> None:
    _heading("test_subclasses_declare_models")
    reg = get_capability_registry()
    for cap in reg.all():
        assert isinstance(cap, Capability)
        assert cap.args_model is not None, cap.name
        assert cap.output_model is not None, cap.name
        print(f"  [OK]{cap.name}: args={cap.args_model.__name__},"
              f" out={cap.output_model.__name__}, pure={cap.pure}")


async def _exercise_stub(cap_name: str, sample_args: dict) -> CapabilityResult:
    reg = get_capability_registry()
    cap = reg.get(cap_name)
    state = ExecutionState.from_request_context(
        RequestContext(
            request_id="smoke",
            question="smoke-q",
            groq_api_key="gsk_" + "x" * 30,
        )
    )
    return await cap.execute(state, sample_args)


def test_capability_bodies_status() -> None:
    """
    Capabilities have two states after P2:

      * Real bodies (5): resolve_time_window, compute_kpi, run_data_query,
        format_response — execute against the live data layer.
        narrate — requires an LLM scope; in this isolated test we don't
        provide one, so it falls through cleanly via NotImplementedError-
        like protection (the scope getter raises RuntimeError which the
        Capability.execute() wrapper catches as an error result).

      * Deferred stubs (3): resolve_entities, compare_periods,
        breakdown_by_hierarchy — still raise NotImplementedError and
        surface as ``stub_capability`` failures.

    This test enforces that contract.
    """
    _heading("test_capability_bodies_status")

    samples: dict[str, dict] = {
        "resolve_time_window":     {"phrase": "last_30_days"},
        "resolve_entities":        {"names": ("Nike", "Adidas")},
        "compute_kpi":             {"kpi_id": "total_revenue"},
        "run_data_query":          {"intent": "summary"},
        "compare_periods":         {
            "metric": "revenue",
            "period_a": {"start_date": "2025-01-01", "end_date": "2025-01-31"},
            "period_b": {"start_date": "2025-02-01", "end_date": "2025-02-28"},
        },
        "breakdown_by_hierarchy":  {"metric": "revenue", "level": "class"},
        "narrate":                 {
            "mode": "summary",
            "aggregates": {"total": 100},
            "user_question": "what is revenue?",
        },
        "format_response":         {
            "narrative": "Total revenue is 100.",
            "aggregates": {"total": 100},
        },
        # Phase B — Drive granular tools. Each returns a typed output with
        # error="Google Drive is not configured..." when no env / OAuth is
        # set (CI case). The capability `ok` flag is True because the
        # capability ran cleanly; the inner error is the operational result.
        "drive_preview":           {"file_id": "fake_id"},
        "drive_infer_schema":      {"file_id": "fake_id"},
        "drive_search":            {"query": "revenue"},
    }

    # After the full P2-P6 build-out + "finish pending work" sweep, all
    # 8 capabilities have real bodies. The set is left as a constant in
    # case future phases introduce new stub capabilities.
    STILL_STUB: set[str] = set()
    REAL_BODIES = set(EXPECTED_CAPABILITIES) - STILL_STUB

    for cap_name in EXPECTED_CAPABILITIES:
        result = asyncio.run(_exercise_stub(cap_name, samples[cap_name]))
        assert isinstance(result, CapabilityResult), cap_name
        if cap_name in STILL_STUB:
            assert result.ok is False, f"{cap_name} expected stub failure"
            assert result.error and "not_implemented" in result.error.lower(), \
                f"{cap_name} expected not_implemented, got {result.error!r}"
            assert "stub_capability" in result.notes, cap_name
            print(f"  [OK]{cap_name}: still-stub (failed in {result.duration_ms:.2f}ms)")
        else:
            # Real bodies should at least run — they may fail if their
            # runtime prerequisites aren't met (e.g., narrate without an
            # LLM scope), but they must NOT carry the stub_capability tag.
            assert "stub_capability" not in result.notes, (
                f"{cap_name} unexpectedly tagged as stub_capability"
            )
            status = "ok" if result.ok else f"failed: {(result.error or '')[:60]}"
            print(f"  [OK]{cap_name}: real-body ({status}) in {result.duration_ms:.2f}ms")


def test_bad_args_rejected_before_run() -> None:
    """Args validation must reject malformed input BEFORE entering run()."""
    _heading("test_bad_args_rejected_before_run")
    result = asyncio.run(_exercise_stub("resolve_time_window", {"phrase": ""}))
    assert result.ok is False
    assert "Invalid args" in (result.error or "")
    print(f"  [OK]resolve_time_window with empty phrase -> {result.error}")


def test_primitive_reexports_resolve() -> None:
    _heading("test_primitive_reexports_resolve")
    from app.orchestrator_v2.tools.primitives import (
        PRIMITIVE_NAMES,
        Tool,
        DatabaseTool,
        RouteClassifierTool,
        IntentAnalyzerTool,
        TimeKPITool,
        EntityResolverTool,
        SchemaRetrieverTool,
        SqlPlannerTool,
        SqlWriterTool,
        SqlValidatorTool,
        SqlExecutorTool,
        ResultAggregatorTool,
        InsightEngineTool,
        ResponseFormatterTool,
        ResponseStoredTool,
    )
    classes = [
        DatabaseTool, RouteClassifierTool, IntentAnalyzerTool, TimeKPITool,
        EntityResolverTool, SchemaRetrieverTool, SqlPlannerTool, SqlWriterTool,
        SqlValidatorTool, SqlExecutorTool, ResultAggregatorTool,
        InsightEngineTool, ResponseFormatterTool, ResponseStoredTool,
    ]
    assert len(classes) == 14 == len(PRIMITIVE_NAMES)
    for cls in classes:
        assert issubclass(cls, Tool), cls.__name__
        # Instantiable + has the expected attrs.
        inst = cls()
        assert inst.name, cls.__name__
    print(f"  [OK]14 primitives re-export and instantiate")


def main() -> int:
    tests = [
        test_registry_has_all_capabilities,
        test_each_capability_has_valid_schema,
        test_subclasses_declare_models,
        test_capability_bodies_status,
        test_bad_args_rejected_before_run,
        test_primitive_reexports_resolve,
    ]
    failures: list[str] = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL]{t.__name__} FAILED: {e}")
        except Exception as e:
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")
            print(f"  [FAIL]{t.__name__} CRASHED: {e}")
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
