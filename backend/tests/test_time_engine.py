"""Time engine tests — dataset-relative time NEVER uses machine clock.

Plain script (no pytest required):

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_time_engine.py

Exits 0 on success, 1 on first failed expectation. Output uses ASCII so
Windows shells don't choke on Unicode arrows.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Sandbox DB so we never touch real data.
_SANDBOX_DB = _BACKEND_DIR / "_time_engine_test.db"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)


_passed = 0
_failed = 0


def check(label: str, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok)
    _failed += int(not ok)
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label:55s} got={got!r:30s} want={want!r}")


async def main():
    # Clean slate.
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name
    from app.time_engine import (
        get_dataset_current_date,
        invalidate_cache,
        resolve_dataset_date_tokens,
        resolve_last_n_days,
        resolve_previous_period,
        resolve_relative_date_range,
        resolve_this_month,
        resolve_this_week,
    )

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()

    # ------------------------------------------------------------------
    # Scenario: latest row date = 2026-05-19 (a Tuesday). Machine clock
    # might be any day — the engine MUST anchor to 2026-05-19.
    # ------------------------------------------------------------------
    rows = [
        # March (previous-previous month — out of every window)
        {"Date": "2026-03-15", "Total Amount": 100.0, "Party Name": "X", "Order No": "O0"},
        # April (previous month relative to dataset)
        {"Date": "2026-04-10", "Total Amount": 200.0, "Party Name": "X", "Order No": "O1"},
        {"Date": "2026-04-25", "Total Amount": 300.0, "Party Name": "Y", "Order No": "O2"},
        # May (current dataset month)
        {"Date": "2026-05-01", "Total Amount": 400.0, "Party Name": "Z", "Order No": "O3"},
        # Last 7-day window (2026-05-13 .. 2026-05-19 inclusive)
        {"Date": "2026-05-13", "Total Amount": 500.0, "Party Name": "X", "Order No": "O4"},
        {"Date": "2026-05-15", "Total Amount": 600.0, "Party Name": "Y", "Order No": "O5"},
        # Yesterday
        {"Date": "2026-05-18", "Total Amount": 700.0, "Party Name": "X", "Order No": "O6"},
        # Today (latest)
        {"Date": "2026-05-19", "Total Amount": 800.0, "Party Name": "Y", "Order No": "O7"},
        {"Date": "2026-05-19", "Total Amount": 900.0, "Party Name": "Z", "Order No": "O8"},
    ]
    insert_rows("sales", rows, batch_id="b-time-test")
    bump_data_version()
    invalidate_cache()

    # ------------------------------------------------------------------
    # 1. Token resolution
    # ------------------------------------------------------------------
    print()
    print("[1] Dataset date detection")
    today = await get_dataset_current_date()
    check("dataset_today",           today, "2026-05-19")

    tokens = await resolve_dataset_date_tokens()
    check("dataset_yesterday",       tokens["dataset_yesterday"],       "2026-05-18")
    check("dataset_month",           tokens["dataset_month"],           "2026-05")
    check("dataset_year",            tokens["dataset_year"],            "2026")
    check("dataset_prev_month",      tokens["dataset_prev_month"],      "2026-04")
    check("dataset_last_7_start",    tokens["dataset_last_7_start"],    "2026-05-13")
    check("dataset_last_30_start",   tokens["dataset_last_30_start"],   "2026-04-20")
    check("dataset_month_start",     tokens["dataset_month_start"],     "2026-05-01")
    check("dataset_month_end",       tokens["dataset_month_end"],       "2026-05-31")
    check("dataset_prev_month_start", tokens["dataset_prev_month_start"], "2026-04-01")
    check("dataset_prev_month_end",  tokens["dataset_prev_month_end"],  "2026-04-30")

    # 2026-05-19 is a Tuesday — ISO week is 2026-05-18 (Mon) .. 2026-05-24 (Sun)
    check("dataset_week_start (Mon)", tokens["dataset_week_start"], "2026-05-18")
    check("dataset_week_end (Sun)",   tokens["dataset_week_end"],   "2026-05-24")

    # ------------------------------------------------------------------
    # 2. Named range resolvers
    # ------------------------------------------------------------------
    print()
    print("[2] resolve_relative_date_range")
    check("today",          await resolve_relative_date_range("today"),         ("2026-05-19", "2026-05-19"))
    check("last_day",       await resolve_relative_date_range("last_day"),      ("2026-05-19", "2026-05-19"))
    check("yesterday",      await resolve_relative_date_range("yesterday"),     ("2026-05-18", "2026-05-18"))
    check("last_7_days",    await resolve_relative_date_range("last 7 days"),   ("2026-05-13", "2026-05-19"))
    check("this_week",      await resolve_relative_date_range("this_week"),     ("2026-05-18", "2026-05-24"))
    check("this_month",     await resolve_relative_date_range("this_month"),    ("2026-05-01", "2026-05-31"))
    check("previous_month", await resolve_relative_date_range("previous month"),("2026-04-01", "2026-04-30"))
    check("last_30_days",   await resolve_relative_date_range("last_30_days"),  ("2026-04-20", "2026-05-19"))
    check("last_3_days (n=3)", await resolve_last_n_days(3),                    ("2026-05-17", "2026-05-19"))
    check("this_week helper", await resolve_this_week(),                        ("2026-05-18", "2026-05-24"))
    check("this_month helper", await resolve_this_month(),                      ("2026-05-01", "2026-05-31"))
    check("prev_period_month", await resolve_previous_period("month"),          ("2026-04-01", "2026-04-30"))

    # ------------------------------------------------------------------
    # 3. KPI engine actually computes dataset-relative values
    # Sales by expected period given our test rows:
    #   2026-05-19: 800 + 900 = 1700                  → sales_today
    #   2026-05-18: 700                               → yesterday_sales
    #   2026-05-13..05-19: 500+600+700+800+900=3500   → last_7_days_sales
    #   2026-05-*: 400+500+600+700+800+900=3900       → sales_this_month
    #   2026-05-18..05-24: 700+800+900=2400           → sales_this_week
    #   2026-04-*: 200+300=500                        → previous_month_sales
    # ------------------------------------------------------------------
    print()
    print("[3] KPI engine uses dataset-relative time")
    cases = [
        ("sales_today",          1700.0),
        ("yesterday_sales",      700.0),
        ("last_7_days_sales",    3500.0),
        ("sales_this_week",      2400.0),
        ("sales_this_month",     3900.0),
        ("previous_month_sales", 500.0),
        # Last 30 window = 2026-04-20..2026-05-19. April 10 row (200) is OUT,
        # April 25 row (300) is IN. So 300 + all May (3900) = 4200.
        ("last_30_days_sales",   4200.0),
        ("sales_this_year",      4500.0),          # all 9 rows total
    ]
    for kpi_id, want in cases:
        r = await calculate_by_name(kpi_id)
        got = r.value
        check(f"KPI {kpi_id}", got, want)

    # ------------------------------------------------------------------
    # 4. Cache invalidation after a new upload moves the dataset date
    # ------------------------------------------------------------------
    print()
    print("[4] Cache invalidation when dataset shifts")
    later = [{"Date": "2026-06-05", "Total Amount": 1000.0, "Party Name": "Q", "Order No": "O9"}]
    insert_rows("sales", later, batch_id="b-time-test-2")
    bump_data_version()
    invalidate_cache()
    new_today = await get_dataset_current_date()
    check("dataset_today after upload", new_today, "2026-06-05")
    new_tokens = await resolve_dataset_date_tokens()
    check("dataset_month after upload", new_tokens["dataset_month"], "2026-06")
    check("dataset_prev_month after upload", new_tokens["dataset_prev_month"], "2026-05")

    # ------------------------------------------------------------------
    # 5. Empty-dataset behaviour
    # ------------------------------------------------------------------
    print()
    print("[5] Empty-dataset graceful failure")
    # We can't easily wipe the DB mid-test; assert directly via probe of a
    # dataset where sales+purchase are scanned but produce nothing. Use a
    # fresh DB.
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version")):
        try: f.unlink()
        except FileNotFoundError: pass
    await init_database()
    invalidate_cache()
    empty_today = await get_dataset_current_date()
    check("dataset_today with no data", empty_today, None)
    empty_tokens = await resolve_dataset_date_tokens()
    check("tokens with no data",        empty_tokens, {})

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version")):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
