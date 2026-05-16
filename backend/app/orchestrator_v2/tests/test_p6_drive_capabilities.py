"""
P6 drive-capabilities smoke test.

Three new capabilities (drive_preview, drive_infer_schema, drive_search)
were added in Phase B. They sit on top of `app.google_drive` helpers
which require an OAuth-connected Google account at runtime.

These tests verify the capabilities WITHOUT a real Drive connection by:
  - Asserting they're registered under stable names + have valid JSON
    schemas the Planner can embed in its prompt.
  - Confirming each capability handles "Drive not configured" + "Drive
    not connected" gracefully — returns a typed output with an error
    field set (the capability itself succeeds; the operational result
    carries the error). This mirrors how the MCP tools behave so the
    contract is consistent.
  - Smoke-testing the helpers' input validation (rejects bad args).

Live integration against a real Drive account is left to a separate
manual / e2e suite once you connect via the app's Upload page.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

from app.orchestrator_v2.state import ExecutionState, RequestContext
from app.orchestrator_v2.tools.base import CapabilityResult
from app.orchestrator_v2.tools.registry import get_capability_registry


DRIVE_CAPABILITIES = ("drive_preview", "drive_infer_schema", "drive_search")


def _heading(t: str) -> None:
    print("=" * 70); print(t); print("=" * 70)


def _state() -> ExecutionState:
    return ExecutionState.from_request_context(
        RequestContext(request_id="r", question="q", groq_api_key="gsk_" + "x" * 30)
    )


def test_three_drive_capabilities_registered() -> None:
    _heading("test_three_drive_capabilities_registered")
    reg = get_capability_registry()
    for name in DRIVE_CAPABILITIES:
        assert name in reg.names, f"{name} missing from registry"
        cap = reg.get(name)
        assert cap.name == name
        print(f"  [OK] {name} registered as {type(cap).__name__}")


def test_drive_capabilities_have_valid_planner_schemas() -> None:
    _heading("test_drive_capabilities_have_valid_planner_schemas")
    import json as _json
    reg = get_capability_registry()
    for name in DRIVE_CAPABILITIES:
        schema = type(reg.get(name)).json_schema()
        assert schema["name"] == name
        assert schema["args_schema"].get("type") == "object"
        # Must be JSON-serialisable — that's what goes into the planner prompt.
        _json.dumps(schema)
        props = list(schema["args_schema"].get("properties", {}).keys())
        print(f"  [OK] {name}: args props = {props}")


async def _exercise(name: str, args: dict) -> CapabilityResult:
    reg = get_capability_registry()
    return await reg.get(name).execute(_state(), args)


def test_drive_not_configured_returns_typed_error() -> None:
    """When GOOGLE_CLIENT_ID is unset (CI default), each capability
    should run successfully but its typed output should carry an
    ``error`` field. Capability.execute returns ok=True (the capability
    didn't crash); the error is operational data, not exception data."""
    _heading("test_drive_not_configured_returns_typed_error")
    # Force is_configured() to False in case the dev machine has the env set.
    with patch("app.google_drive.is_configured", return_value=False):
        for name, args in (
            ("drive_preview", {"file_id": "fake"}),
            ("drive_infer_schema", {"file_id": "fake"}),
            ("drive_search", {"query": "anything"}),
        ):
            result = asyncio.run(_exercise(name, args))
            assert isinstance(result, CapabilityResult), name
            assert result.ok, f"{name}: execute() crashed: {result.error}"
            assert result.output is not None
            err = result.output.get("error")
            assert err and "not configured" in err.lower(), (
                f"{name}: expected 'not configured' error, got {err!r}"
            )
            print(f"  [OK] {name} -> Drive-not-configured error surfaced cleanly")


def test_drive_not_connected_returns_typed_error() -> None:
    """When Drive is configured but no OAuth token is on disk."""
    _heading("test_drive_not_connected_returns_typed_error")
    with patch("app.google_drive.is_configured", return_value=True), \
         patch("app.google_drive.load_credentials", return_value=None):
        for name, args in (
            ("drive_preview", {"file_id": "fake"}),
            ("drive_infer_schema", {"file_id": "fake"}),
            ("drive_search", {"query": "anything"}),
        ):
            result = asyncio.run(_exercise(name, args))
            assert result.ok, f"{name}: execute() crashed: {result.error}"
            err = (result.output or {}).get("error") or ""
            assert "not connected" in err.lower(), (
                f"{name}: expected 'not connected' error, got {err!r}"
            )
            print(f"  [OK] {name} -> Drive-not-connected error surfaced cleanly")


def test_bad_args_rejected() -> None:
    _heading("test_bad_args_rejected")
    # Empty query is min_length=1 -> rejected by args validation.
    result = asyncio.run(_exercise("drive_search", {"query": ""}))
    assert not result.ok, "empty query should fail args validation"
    assert "Invalid args" in (result.error or "")
    print(f"  [OK] drive_search('') -> {result.error[:60]}")
    # rows=0 is out of range -> rejected.
    result = asyncio.run(_exercise("drive_preview", {"file_id": "x", "rows": 0}))
    assert not result.ok
    print(f"  [OK] drive_preview(rows=0) -> {result.error[:60]}")


def main() -> int:
    tests = [
        test_three_drive_capabilities_registered,
        test_drive_capabilities_have_valid_planner_schemas,
        test_drive_not_configured_returns_typed_error,
        test_drive_not_connected_returns_typed_error,
        test_bad_args_rejected,
    ]
    failures: list[str] = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")
        print()
    _heading("SUMMARY")
    if failures:
        for f in failures: print(f"    - {f}")
        return 1
    print(f"  all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
