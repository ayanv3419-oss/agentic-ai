"""Error tracking E2E.

Run:
    python backend/tests/test_errors.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_errors_test.db"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:55s} got={got!r:30s} want={want!r}")


def assert_true(label, condition):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version")):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database
    from app.errors import (
        log_error, list_errors, get_error, resolve_error,
        error_analytics, SEVERITIES,
    )

    await init_database()

    # ---------- 1. Capture a few representative errors ----------
    print("[1] Capture errors of different shapes")
    e1 = log_error(message="csv missing required column", module="upload",
                   severity="high", endpoint="/upload", method="POST",
                   user_facing=True, context={"target": "sales"})
    assert_true("upload error id returned", e1 and e1.startswith("err-"))

    # An actual exception with a traceback
    try:
        raise ValueError("test boom")
    except Exception as exc:
        e2 = log_error(exc=exc, module="kpi", endpoint="/kpi/x/calculate",
                       method="POST")
    assert_true("kpi exception id returned", e2 and e2 != e1)

    e3 = log_error(message="Failed to render dashboard chart",
                   module="frontend", severity="medium",
                   source="ui_system.tsx -> ChartView", user_facing=True,
                   context={"url": "http://localhost:5173/dashboard"})
    assert_true("frontend error id returned", e3 and e3 != e2)

    e4 = log_error(message="connection refused",
                   module="database", severity="critical",
                   source="async_db_connect")
    assert_true("critical error id returned", e4 and e4 != e3)

    # ---------- 2. Listing ----------
    print()
    print("[2] List + filter")
    all_rows = await list_errors()
    check("4 errors persisted", len(all_rows), 4)

    upload_only = await list_errors(module="upload")
    check("module=upload filter", len(upload_only), 1)

    critical_only = await list_errors(severity="critical")
    check("severity=critical filter", len(critical_only), 1)

    unresolved = await list_errors(resolved=False)
    check("resolved=False filter (initially all 4)", len(unresolved), 4)

    # ---------- 3. Auto-classification ----------
    print()
    print("[3] Auto-classification")
    kpi_row = await get_error(e2)
    assert_true("kpi error has stack trace", bool((kpi_row or {}).get("stack_trace")))
    check("ValueError -> medium severity", (kpi_row or {}).get("severity"), "medium")
    assert_true(
        "ValueError suggests a fix",
        bool((kpi_row or {}).get("suggested_fix")),
    )

    upload_row = await get_error(e1)
    check("upload error has suggested_fix?", bool((upload_row or {}).get("suggested_fix")), False)
    # We didn't pass exc on e1, so no canned fix; that's expected behaviour.

    # ---------- 4. Resolve ----------
    print()
    print("[4] Resolve")
    ok = await resolve_error(e1, note="user re-uploaded with corrected header")
    check("resolve returns True for known id", ok, True)
    bad = await resolve_error("err-doesnotexist")
    check("resolve returns False for unknown id", bad, False)

    resolved_rows = await list_errors(resolved=True)
    check("resolved=True filter shows 1", len(resolved_rows), 1)
    unresolved_rows = await list_errors(resolved=False)
    check("resolved=False filter shows 3", len(unresolved_rows), 3)

    # ---------- 5. Analytics ----------
    print()
    print("[5] Analytics")
    a = await error_analytics()
    check("total=4", a["total"], 4)
    check("unresolved=3", a["unresolved"], 3)
    # By module: upload + kpi + frontend + database = 4 distinct
    modules = {row["module"] for row in a["by_module"]}
    expected_modules = {"upload", "kpi", "frontend", "database"}
    check("by_module covers expected", modules, expected_modules)
    sev_keys = {row["severity"] for row in a["by_severity"]}
    assert_true("by_severity includes critical", "critical" in sev_keys)
    assert_true("by_severity includes medium",   "medium"   in sev_keys)
    assert_true("by_severity includes high",     "high"     in sev_keys)

    # Top types
    top_types = {row["error_type"] for row in a["top_types"]}
    assert_true("top_types includes ValueError", "ValueError" in top_types)
    assert_true("top_types includes AppError",   "AppError" in top_types)

    # ---------- 6. log_error NEVER raises ----------
    print()
    print("[6] log_error never raises, even on bad inputs")
    no_exc_id = log_error()       # no args
    assert_true("log_error with no args returns id", no_exc_id and no_exc_id.startswith("err-"))
    bad_payload_id = log_error(
        message="payload should be json-safe",
        request_payload=object(),   # not JSON-serializable
        context={"f": lambda: 1},   # also not JSON-serializable
    )
    assert_true("log_error with non-JSON payload still returns id", bad_payload_id and bad_payload_id.startswith("err-"))

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version")):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
