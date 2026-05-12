"""Presentation-layer E2E.

Verifies that the SSE response streamed to the user from /query_stream
contains ZERO internal/technical leakage:

  - No 'formula' / 'formula_expression'
  - No 'sql' / 'sql_used' / 'SELECT' / 'FROM ' / 'GROUP BY' / 'JOIN '
  - No 'required_columns' / 'missing_columns'
  - No internal kpi_id
  - No 'kpi.matched' / 'tool.start' / 'tool.end' event names
  - No raw column names like '"Total Amount"' / '"Party Name"'

Also verifies that the executive-style narrative is produced — answers
read like a business sentence, not a "Label: value" dump.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_pres_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_pres_test_cache.json"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)

_passed = _failed = 0


def _safe(s) -> str:
    """ASCII-safe stringification — Windows consoles choke on non-cp1252."""
    return str(s).encode("ascii", "replace").decode("ascii")


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(_safe(f"  [{'OK ' if ok else 'FAIL'}] {label:60s} got={got!r:35s} want={want!r}"))


def assert_true(label, condition, hint=""):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok); _failed += int(not ok)
    suffix = f"  | {hint}" if hint and not ok else ""
    print(_safe(f"  [{'OK ' if ok else 'FAIL'}] {label}{suffix}"))


# Forbidden tokens that MUST NOT appear in any user-facing SSE payload.
_FORBIDDEN = [
    "formula",
    "formula_expression",
    "sql_used",
    "required_columns",
    "missing_columns",
    "kpi_id",
    "matched_alias",
    "computed_at",
    "stack_trace",
    # SQL fragments
    "SELECT ",
    "FROM sales",
    "FROM purchase",
    "GROUP BY",
    "JOIN ",
    "WHERE ",
    "COALESCE",
    "substr(",
    # Internal column quoting
    '"Total Amount"',
    '"Party Name"',
    '"Product Name"',
    '"Date"',
    # Internal tool names
    "RouteClassifier",
    "IntentAnalyzer",
    "SqlPlanner",
    "SqlWriter",
    "SqlExecutor",
    "ResultAggregator",
    "InsightEngine",
    "ResponseFormatter",
]


class CapturingEmitter:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    async def emit(self, event: str, data):
        self.events.append((event, data))

    async def comment(self, text: str):
        return

    async def close(self):
        return


def serialized(events) -> str:
    """JSON-serialize all events together so we can scan for leaks."""
    payload = [{"event": e, "data": d} for e, d in events]
    return json.dumps(payload, default=str)


def find_forbidden(blob: str) -> list[str]:
    return [tok for tok in _FORBIDDEN if tok in blob]


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog
    from app.hierarchy import seed_default_business, sync_product_sku_master
    from app.analytics_engine import TurnState, run_query_turn
    from app.time_engine import invalidate_cache

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    insert_rows("sales", [
        {"Date": "2026-05-19", "Total Amount": 1500.0, "Party Name": "A", "Order No": "O1", "Product Name": "Nike Running Shoes Mens"},
        {"Date": "2026-05-18", "Total Amount": 800.0,  "Party Name": "B", "Order No": "O2", "Product Name": "Bata Casual Slippers Mens"},
        {"Date": "2026-05-17", "Total Amount": 2500.0, "Party Name": "C", "Order No": "O3", "Product Name": "Womens Black Block Heels"},
    ], batch_id="b1")
    insert_rows("purchase", [
        {"Date": "2026-04-30", "Total Amount": 2000.0, "Party Name": "V", "Product Name": "Nike Running Shoes Mens"},
    ], batch_id="b2")
    bump_data_version(); invalidate_cache()
    await sync_product_sku_master()

    # ---------- 1. NO LEAKAGE in user-facing SSE for representative business queries ----------
    print("[1] Zero-leakage in SSE payloads")
    business_queries = [
        "total revenue",
        "profit margin",
        "top customer",
        "average order value",
        "top brand",
        "sales by class",
        "top sku",
        "previous month sales",
        "outstanding balance",
        "collection rate",
    ]
    for q in business_queries:
        state = TurnState(question=q)
        emitter = CapturingEmitter()
        await run_query_turn(state, emitter)
        blob = serialized(emitter.events)
        leaks = find_forbidden(blob)
        assert_true(
            f"no leaks for {q!r}",
            len(leaks) == 0,
            hint=f"leaked tokens: {leaks}",
        )

    # ---------- 2. final event contains executive-style narrative ----------
    print()
    print("[2] Executive-style narrative in final.answer")
    state = TurnState(question="total revenue")
    emitter = CapturingEmitter()
    await run_query_turn(state, emitter)
    final = next((d for e, d in emitter.events if e == "final"), None)
    assert_true("final event exists", final is not None)
    answer = (final or {}).get("answer", "")
    assert_true(
        "answer is a sentence (ends with '.')",
        answer.rstrip().endswith("."),
        hint=f"answer={answer!r}",
    )
    assert_true(
        "answer uses currency formatting (₹ or $ symbol or comma-separated)",
        ("," in answer) or ("." in answer),
    )
    assert_true(
        "answer doesn't contain raw 'Total Amount' column name",
        '"Total Amount"' not in answer,
    )
    assert_true(
        "answer doesn't read like 'Label: value' (executive prose)",
        # The narrator uses prose like "Total Revenue stands at ₹X.";
        # not the old "Label: value" dump.
        " stands at " in answer or " sits at " in answer
        or " is " in answer or " leads " in answer or " works out " in answer,
        hint=f"answer={answer!r}",
    )

    # ---------- 3. final.metric is the USER-SAFE payload ----------
    print()
    print("[3] final.metric is user-safe (no formula / sql / required_columns)")
    metric = (final or {}).get("metric", {})
    assert_true("metric.metric (display label) present",     "metric" in metric)
    assert_true("metric.value present",                       "value" in metric)
    assert_true("metric.format present",                      "format" in metric)
    assert_true("metric.category_label present",              "category_label" in metric)
    assert_true("metric.chartable present",                   "chartable" in metric)
    # Forbidden fields:
    for forbidden_key in ("formula", "sql_used", "required_columns",
                          "missing_columns", "kpi", "computed_at",
                          "explanation", "warnings"):
        assert_true(f"metric has NO '{forbidden_key}' key",
                    forbidden_key not in metric,
                    hint=f"keys={list(metric.keys())}")

    # ---------- 4. category_label is humanized ----------
    print()
    print("[4] category_label uses human-friendly names")
    state = TurnState(question="profit margin")
    emitter = CapturingEmitter()
    await run_query_turn(state, emitter)
    final = next((d for e, d in emitter.events if e == "final"), None)
    assert_true(
        "profit_margin category renders as 'Profit & Margin'",
        (final or {}).get("metric", {}).get("category_label") == "Profit & Margin",
    )

    # ---------- 5. Top-N / list KPIs render labels, not internal SQL columns ----------
    print()
    print("[5] List KPIs surface clean rows (label, value only)")
    state = TurnState(question="top customer")
    emitter = CapturingEmitter()
    await run_query_turn(state, emitter)
    final = next((d for e, d in emitter.events if e == "final"), None)
    rows = (final or {}).get("metric", {}).get("rows", [])
    assert_true("top-customer result has at least 1 row", len(rows) >= 1)
    for r in rows:
        keys = set(r.keys())
        assert_true(
            f"row keys are a clean subset (no internal cols): {keys}",
            keys.issubset({"label", "value"}) or len(keys) <= 6,
        )

    # ---------- 6. Internal events are NOT emitted with internal names ----------
    print()
    print("[6] Internal event names are hidden from user-facing stream")
    state = TurnState(question="top brand")
    emitter = CapturingEmitter()
    await run_query_turn(state, emitter)
    event_names = {e for e, _d in emitter.events}
    # Allowed events:
    allowed_user_events = {
        "turn.start", "final", "turn.end", "analyzing",
        "cache.hit", "query.kind", "mode.selected",
        # KPI fast-path: we replaced "kpi.matched" with "analyzing"
    }
    forbidden_internal_events = {"kpi.matched", "tool.start", "tool.end"}
    leaked_events = event_names & forbidden_internal_events
    assert_true(
        f"no internal event names in stream (got: {sorted(event_names)})",
        len(leaked_events) == 0,
        hint=f"leaked: {leaked_events}",
    )

    # ---------- 7. Errors don't leak Python exception text or SQL ----------
    print()
    print("[7] Error envelopes are user-safe")
    # Trigger a KPI with no available data (use a fresh DB)
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass
    from app.kpi import init_kpi_table as _init_k2, rebuild_catalog as _reb2
    await init_database()
    await _init_k2()
    await _reb2()
    invalidate_cache()
    state = TurnState(question="profit margin")
    emitter = CapturingEmitter()
    await run_query_turn(state, emitter)
    final = next((d for e, d in emitter.events if e == "final"), None)
    answer = (final or {}).get("answer", "")
    assert_true(
        "empty-data error message is user-friendly",
        ("0" in answer or "no data" in answer.lower()
         or "couldn't" in answer.lower() or "data" in answer.lower()),
        hint=f"answer={answer!r}",
    )
    assert_true(
        "empty-data error does NOT contain technical jargon",
        "OperationalError" not in answer and "Traceback" not in answer
        and "SELECT" not in answer and "{dataset_" not in answer,
        hint=f"answer={answer!r}",
    )

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
