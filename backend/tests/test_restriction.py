"""Domain-isolation tests — prove off-topic questions are refused with the
canonical restriction message, while business queries still flow through
the analytics pipeline.

Run:
    python backend/tests/test_restriction.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_restriction_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_restriction_test_cache.json"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")


_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:55s} got={got!r:50s} want={want!r}")


def assert_true(label, condition):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")


class CapturingEmitter:
    """Minimal EventEmitter stand-in that just records every event."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: str, data):
        self.events.append((event, data))

    async def comment(self, text: str):
        return

    async def close(self):
        return

    def event_data(self, name: str):
        for n, d in self.events:
            if n == name:
                return d
        return None


async def main():
    # Clean slate
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog
    from app.hierarchy import seed_default_business
    from app.analytics_engine import (
        RESTRICTION_REFUSAL, TurnState, run_query_turn, classify_query_kind,
    )
    from app.time_engine import invalidate_cache
    from app.hierarchy import sync_product_master_from_data, sync_product_sku_master

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # Need some data so _has_any_uploaded_data() returns True (otherwise
    # general business words get routed to chat instead of general_knowledge).
    insert_rows("sales", [
        {"Date": "2026-05-19", "Total Amount": 1000.0, "Party Name": "X",
         "Order No": "O1", "Product Name": "Nike Running Shoes"},
        {"Date": "2026-05-18", "Total Amount": 500.0, "Party Name": "Y",
         "Order No": "O2", "Product Name": "Bata Slippers"},
    ], batch_id="b1")
    bump_data_version()
    invalidate_cache()
    # Real /upload triggers these — mirror that flow so hierarchy-dependent
    # KPIs (top_brand etc.) can JOIN through populated master tables.
    await sync_product_master_from_data()
    await sync_product_sku_master()

    # ----------------------------------------------------------------------
    # 1. Classifier never routes off-topic queries to data_query.
    #    (Whether it picks 'general_knowledge' or 'chat' is implementation
    #    detail — what matters is the BEHAVIOR proven in section 2.)
    # ----------------------------------------------------------------------
    print("[1] Classifier never routes off-topic to data_query")
    off_topic = [
        "what is the sun",
        "who invented AI",
        "teach me python",
        "what is world history",
        "who is elon musk",
        "explain quantum physics",
        "tell me a joke",
    ]
    for q in off_topic:
        kind, _conf, _hints = classify_query_kind(q, has_data=True)
        assert_true(
            f"{q!r} classified as {kind} (not data_query)",
            kind != "data_query",
        )

    # ----------------------------------------------------------------------
    # 2. End-to-end: off-topic queries return the canonical refusal message.
    # ----------------------------------------------------------------------
    print()
    print("[2] /query_stream returns RESTRICTION_REFUSAL for EVERY off-topic query")
    for q in off_topic:   # all of them, not just first 4
        state = TurnState(question=q)
        emitter = CapturingEmitter()
        result = await run_query_turn(state, emitter)
        check(f"final_answer for {q!r}", result.final_answer, RESTRICTION_REFUSAL)
        final_evt = emitter.event_data("final")
        check(f"final.mode for {q!r}", (final_evt or {}).get("mode"), "restricted")
        restricted_evt = emitter.event_data("restricted")
        assert_true(f"restricted event fired for {q!r}", restricted_evt is not None)

    # ----------------------------------------------------------------------
    # 3. Business KPI questions still pass through and return real numbers.
    # ----------------------------------------------------------------------
    print()
    print("[3] Business KPI questions return real analytics answers")
    business_queries = [
        ("what is my profit margin",         "Profit Margin"),
        ("revenue last 7 days",              "Last 7 Days Sales"),
        ("top brand",                        "Top Brand by Revenue"),
        ("total customers",                  "Total Customers"),
    ]
    for q, expected_label_prefix in business_queries:
        state = TurnState(question=q)
        emitter = CapturingEmitter()
        result = await run_query_turn(state, emitter)
        answer = result.final_answer or ""
        assert_true(
            f"answer for {q!r} is NOT the refusal",
            RESTRICTION_REFUSAL not in answer,
        )
        assert_true(
            f"answer for {q!r} mentions the expected KPI label",
            expected_label_prefix.lower() in answer.lower(),
        )

    # ----------------------------------------------------------------------
    # 4. Pure greetings get a brief friendly reply, not the refusal.
    # ----------------------------------------------------------------------
    print()
    print("[4] Pure greetings get a friendly reply (mode='greeting', not refused)")
    for q in ["hi", "hello", "thanks", "ok"]:
        state = TurnState(question=q)
        emitter = CapturingEmitter()
        result = await run_query_turn(state, emitter)
        assert_true(
            f"answer for {q!r} is NOT the refusal",
            (result.final_answer or "") != RESTRICTION_REFUSAL,
        )
        final_evt = emitter.event_data("final")
        check(f"mode for {q!r}", (final_evt or {}).get("mode"), "greeting")

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
