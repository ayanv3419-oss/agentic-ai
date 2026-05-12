"""Mock product-name backfill E2E test.

Verifies:
  • Sales rows with EMPTY Product Name get a deterministic mock name.
  • Sales rows with a REAL Product Name are never touched.
  • Backfill is idempotent — re-running produces identical names.
  • Every mock name belongs to the 50-name footwear pool.
  • is_mock_named flag is correctly set / unset.
  • Downstream hierarchy + inventory + forecast pipelines pick up the
    new mock-named rows automatically.
  • Pre-existing KPIs continue to work; totals stay consistent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB    = _BACKEND_DIR / "_mock_backfill_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_mock_backfill_test_cache.json"
os.environ["FINANCIAL_DB_PATH"]     = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"]   = str(_SANDBOX_CACHE)
os.environ.setdefault("AUTH_TOKEN_SECRET", "x" * 32)


_passed = 0
_failed = 0


def check(label: str, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok)
    _failed += int(not ok)
    mark = "OK " if ok else "FAIL"
    g = repr(got).encode("ascii", "replace").decode()
    w = repr(want).encode("ascii", "replace").decode()
    print(f"  [{mark}] {label:60s} got={g:35s} want={w}")


def assert_true(label: str, condition: bool):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok)
    _failed += int(not ok)
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label}")


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    from app.infrastructure import (
        init_database, insert_rows, bump_data_version, fetch_all, fetch_one,
    )
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name
    from app.hierarchy import (
        seed_default_business, sync_product_master_from_data,
        sync_product_sku_master,
    )
    from app.enrichment import (
        MOCK_FOOTWEAR_POOL,
        backfill_missing_product_names,
        mock_backfill_stats,
        refresh_inventory,
        refresh_forecast,
    )
    from app.time_engine import invalidate_cache

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # ------------------------------------------------------------------
    # 1. Insert a mix: 5 rows with real names, 5 rows with empty names.
    # ------------------------------------------------------------------
    real_rows = [
        {"Date": "2026-05-19", "Total Amount": 1500.0, "Party Name": "P1",
         "Order No": "O1", "Product Name": "Nike Running Shoes Mens"},
        {"Date": "2026-05-18", "Total Amount": 2000.0, "Party Name": "P2",
         "Order No": "O2", "Product Name": "Womens Black Block Heels"},
        {"Date": "2026-05-17", "Total Amount": 800.0,  "Party Name": "P3",
         "Order No": "O3", "Product Name": "Boys Black Velcro School Shoes"},
        {"Date": "2026-05-16", "Total Amount": 1100.0, "Party Name": "P4",
         "Order No": "O4", "Product Name": "Bata Casual Slippers Mens"},
        {"Date": "2026-05-15", "Total Amount": 900.0,  "Party Name": "P5",
         "Order No": "O5", "Product Name": "Kids Disney Character Shoes"},
    ]
    empty_rows = [
        {"Date": "2026-05-19", "Total Amount": 155.0,  "Party Name": "Q1",
         "Order No": "E1", "Product Name": ""},
        {"Date": "2026-05-19", "Total Amount": 250.0,  "Party Name": "Q2",
         "Order No": "E2", "Product Name": None},
        {"Date": "2026-05-18", "Total Amount": 600.0,  "Party Name": "Q3",
         "Order No": "E3", "Product Name": "   "},   # whitespace-only
        {"Date": "2026-05-17", "Total Amount": 1800.0, "Party Name": "Q4",
         "Order No": "E4", "Product Name": ""},
        {"Date": "2026-05-15", "Total Amount": 320.0,  "Party Name": "Q5",
         "Order No": "E5", "Product Name": None},
    ]
    insert_rows("sales", real_rows + empty_rows, batch_id="b-mock-test")
    bump_data_version()
    invalidate_cache()

    print("[1] Backfill runs and fills only empty product-name rows")
    stats = await backfill_missing_product_names()
    check("sales rows filled", stats["sales_filled"], 5)
    check("purchase rows filled (none — table empty)",
          stats.get("purchase_filled", 0), 0)

    # ------------------------------------------------------------------
    # 2. Real rows preserved; mock rows are flagged + named from the pool.
    # ------------------------------------------------------------------
    print()
    print("[2] Real product names preserved; mock rows flagged")
    real_count_row = await fetch_one(
        'SELECT COUNT(*) AS n FROM sales WHERE is_mock_named = 0'
    )
    check("rows with is_mock_named = 0", int(real_count_row["n"]), 5)
    mock_count_row = await fetch_one(
        'SELECT COUNT(*) AS n FROM sales WHERE is_mock_named = 1'
    )
    check("rows with is_mock_named = 1", int(mock_count_row["n"]), 5)

    # The 5 originally-real names must still be present untouched.
    real_names_preserved = await fetch_all(
        'SELECT DISTINCT "Product Name" AS name FROM sales WHERE is_mock_named = 0 '
        'ORDER BY name'
    )
    real_name_set = {r["name"] for r in real_names_preserved}
    expected = {row["Product Name"] for row in real_rows}
    check("real names preserved exactly", real_name_set, expected)

    # All mock-named rows must have a name from the pool.
    mock_named_rows = await fetch_all(
        'SELECT "Product Name" AS name FROM sales WHERE is_mock_named = 1'
    )
    pool_set = set(MOCK_FOOTWEAR_POOL)
    for r in mock_named_rows:
        assert_true(f"mock name {r['name']!r} is from the pool",
                    r["name"] in pool_set)

    # ------------------------------------------------------------------
    # 3. Backfill is deterministic — re-running produces no new fills.
    # ------------------------------------------------------------------
    print()
    print("[3] Backfill is idempotent (re-run = no change)")
    stats2 = await backfill_missing_product_names()
    check("re-run sales_filled = 0", stats2["sales_filled"], 0)

    # Snapshot the mock-named rows so we can verify no name changes.
    snap_before = await fetch_all(
        'SELECT id, "Product Name" AS name FROM sales WHERE is_mock_named = 1 '
        'ORDER BY id'
    )
    stats3 = await backfill_missing_product_names()
    check("3rd run sales_filled = 0", stats3["sales_filled"], 0)
    snap_after = await fetch_all(
        'SELECT id, "Product Name" AS name FROM sales WHERE is_mock_named = 1 '
        'ORDER BY id'
    )
    check("mock names byte-identical across re-runs",
          [(r["id"], r["name"]) for r in snap_before],
          [(r["id"], r["name"]) for r in snap_after])

    # ------------------------------------------------------------------
    # 4. Downstream sync picks up the new names.
    # ------------------------------------------------------------------
    print()
    print("[4] Hierarchy + inventory + forecast sync pick up mock-named rows")
    await sync_product_master_from_data()
    await sync_product_sku_master()
    await refresh_inventory()
    await refresh_forecast()

    inv_rows = await fetch_all("SELECT COUNT(*) AS n FROM sku_inventory")
    assert_true("sku_inventory has rows", int(inv_rows[0]["n"]) > 0)

    # Mock-named products should be present in product_sku_master.
    mock_names_list = [r["name"] for r in mock_named_rows]
    for name in set(mock_names_list):
        row = await fetch_one(
            "SELECT sku_code FROM product_sku_master WHERE product_name = ?",
            (name,),
        )
        assert_true(f"sku_master has entry for mock name: {name}",
                    row is not None)

    # ------------------------------------------------------------------
    # 5. KPIs still consistent: total_revenue matches the raw sum.
    # ------------------------------------------------------------------
    print()
    print("[5] KPI totals unchanged by backfill")
    expected_total = sum(r["Total Amount"] for r in real_rows + empty_rows)
    r = await calculate_by_name("total_revenue")
    check("total_revenue", r.value, float(expected_total))

    # AI can now answer "top products" for the mock-named revenue too.
    r2 = await calculate_by_name("top_products_last_7_days")
    assert_true("top_products_last_7_days produced rows",
                r2.error is None and len(r2.rows) > 0)

    # ------------------------------------------------------------------
    # 6. mock_backfill_stats endpoint helper returns correct counts.
    # ------------------------------------------------------------------
    print()
    print("[6] mock_backfill_stats helper")
    s = await mock_backfill_stats()
    check("stats sales.total",      s["sales"]["total"],      10)
    check("stats sales.mock_named", s["sales"]["mock_named"],  5)
    check("stats sales.real_named", s["sales"]["real_named"],  5)
    check("stats sales.still_empty", s["sales"]["still_empty"], 0)
    check("stats pool_size",        s["pool_size"], 50)

    # ------------------------------------------------------------------
    # 7. Brand-new row inserted later also gets backfilled on next call.
    # ------------------------------------------------------------------
    print()
    print("[7] A later row with empty name is backfilled on next call")
    insert_rows("sales", [
        {"Date": "2026-05-19", "Total Amount": 700.0, "Party Name": "Q6",
         "Order No": "E6", "Product Name": ""},
    ], batch_id="b-mock-test-2")
    stats4 = await backfill_missing_product_names()
    check("subsequent row backfilled", stats4["sales_filled"], 1)

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
