"""Enrichment E2E — inventory + forecast derived from real sales.

Verifies:
  1. Inventory rows are generated for every SKU in product_sku_master
  2. Velocity + status classification works (ok / low / overstocked / dead)
  3. Dead-stock detection fires for inactive SKUs (no sales in 30+ days)
  4. Forecast generates 14-day projections for active SKUs only
  5. New KPIs (low_stock_skus, forecast_revenue_next_7d, etc.) execute
  6. Real sales totals stay byte-identical (no enrichment side effects)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_enrich_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_enrich_test_cache.json"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)

_passed = _failed = 0


def _safe(s) -> str:
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


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name, list_kpis
    from app.hierarchy import (
        seed_default_business, sync_product_master_from_data,
        sync_product_sku_master,
    )
    from app.enrichment import (
        refresh_inventory, refresh_forecast, list_inventory,
        inventory_snapshot, forecast_summary, list_forecast_for_sku,
    )
    from app.time_engine import invalidate_cache

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # Build a 60-day sales history across multiple SKUs with varied
    # velocity, so we can verify low / ok / dead classification.
    sales = []
    # Active high-velocity SKU: ~3 sales/day for 60 days
    for i in range(60):
        d = (60 - i)   # 60..1 days ago from 2026-05-19
        # Using fixed dates for determinism: dataset_today = 2026-05-19
        from datetime import date, timedelta
        the_date = (date(2026, 5, 19) - timedelta(days=d)).isoformat()
        sales.append({
            "Date": the_date, "Total Amount": 1000.0,
            "Party Name": "Cust", "Order No": f"O{i}",
            "Product Name": "Nike Running Shoes Mens",
        })
    # Active medium-velocity SKU: 1 sale every 3 days for 30 days
    for i in range(0, 30, 3):
        from datetime import date, timedelta
        the_date = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": the_date, "Total Amount": 800.0,
            "Party Name": "Cust2", "Order No": f"OM{i}",
            "Product Name": "Bata Casual Slippers Mens",
        })
    # DEAD-STOCK SKU: only ancient sales, nothing in last 30 days.
    for i in range(50, 60):
        from datetime import date, timedelta
        the_date = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": the_date, "Total Amount": 1500.0,
            "Party Name": "Cust3", "Order No": f"OD{i}",
            "Product Name": "Womens Vintage Heels",
        })

    insert_rows("sales", sales, batch_id="b1")
    bump_data_version()
    invalidate_cache()

    await sync_product_master_from_data()
    await sync_product_sku_master()

    # ---- 1. Inventory refresh produces rows for every SKU ----
    print("[1] Inventory refresh")
    inv_stats = await refresh_inventory()
    check("inventory.skus", inv_stats["skus"], 3)

    inv = await list_inventory()
    by_product = {row["product_name"]: row for row in inv}
    assert_true("inventory row for Nike Running Shoes Mens", "Nike Running Shoes Mens" in by_product)
    assert_true("inventory row for Bata Casual Slippers Mens", "Bata Casual Slippers Mens" in by_product)
    assert_true("inventory row for Womens Vintage Heels", "Womens Vintage Heels" in by_product)

    # ---- 2. Velocity + status correctness ----
    print()
    print("[2] Velocity + status classification")
    nike = by_product.get("Nike Running Shoes Mens", {})
    assert_true(
        "Nike SKU avg_daily_sales > 0 (high-velocity)",
        (nike.get("avg_daily_sales") or 0) > 0.5,
    )
    assert_true(
        "Nike SKU has reasonable on_hand_qty (>=30)",
        (nike.get("on_hand_qty") or 0) >= 30,
    )
    assert_true(
        "Nike status is 'ok' (30 days of cover at seed time)",
        nike.get("status") == "ok",
    )

    bata = by_product.get("Bata Casual Slippers Mens", {})
    assert_true(
        "Bata SKU also has status 'ok'",
        bata.get("status") in ("ok", "low"),  # both acceptable for medium velocity
    )

    vintage = by_product.get("Womens Vintage Heels", {})
    check("Vintage Heels status = 'dead' (no sales last 30 days)",
          vintage.get("status"), "dead")

    # ---- 3. Snapshot counts ----
    print()
    print("[3] Inventory snapshot summary")
    snap = await inventory_snapshot()
    check("snapshot.total", snap["total"], 3)
    check("snapshot.dead >= 1", snap["dead"] >= 1, True)
    check("snapshot.ok >= 1",   snap["ok"] >= 1,   True)

    # ---- 4. Forecast — only for ACTIVE SKUs ----
    print()
    print("[4] Forecast refresh — active SKUs only")
    fc_stats = await refresh_forecast()
    # Active = 2 (Nike + Bata); Vintage Heels has no recent sales so it's skipped.
    check("forecast.skus = 2 active",  fc_stats["skus"], 2)
    check("forecast.rows = 14 * 2 = 28", fc_stats["rows"], 28)

    nike_forecast = await list_forecast_for_sku(
        next(row["sku_code"] for row in inv if row["product_name"] == "Nike Running Shoes Mens")
    )
    check("Nike forecast has 14 rows", len(nike_forecast), 14)
    assert_true(
        "Nike forecast revenue per day > 0",
        all(float(r["forecast_revenue"]) > 0 for r in nike_forecast),
    )

    vintage_forecast = await list_forecast_for_sku(
        next(row["sku_code"] for row in inv if row["product_name"] == "Womens Vintage Heels")
    )
    check("Vintage Heels forecast: 0 rows (dead SKU)", len(vintage_forecast), 0)

    # ---- 5. Forecast summary KPI ----
    print()
    print("[5] Forecast summary")
    summary = await forecast_summary()
    assert_true("forecast_summary has_data = True", summary["has_data"] is True)
    assert_true("next_7_days_revenue > 0", summary["next_7_days_revenue"] > 0)
    assert_true(
        "next_30_days_revenue >= next_7_days_revenue (longer horizon, more)",
        summary["next_30_days_revenue"] >= summary["next_7_days_revenue"],
    )

    # ---- 6. New enrichment KPIs execute correctly ----
    print()
    print("[6] Enrichment KPIs execute")
    r = await calculate_by_name("low_stock_skus")
    check("low_stock_skus is a non-negative integer", isinstance(r.value, (int, float)) and r.value >= 0, True)

    r = await calculate_by_name("dead_stock_skus")
    check("dead_stock_skus = 1", r.value, 1)

    r = await calculate_by_name("overstocked_skus")
    check("overstocked_skus is a non-negative integer", isinstance(r.value, (int, float)) and r.value >= 0, True)

    r = await calculate_by_name("inventory_status_breakdown")
    assert_true("inventory_status_breakdown produced rows", len(r.rows) >= 1)

    r = await calculate_by_name("forecast_revenue_next_7d")
    check("forecast_revenue_next_7d is a positive number",
          isinstance(r.value, (int, float)) and r.value > 0, True)

    r = await calculate_by_name("forecast_revenue_next_14d")
    check("forecast_revenue_next_14d >= forecast_revenue_next_7d",
          r.value >= summary["next_7_days_revenue"], True)

    # ---- 7. Critical: real sales totals unchanged by enrichment ----
    print()
    print("[7] Real sales totals unchanged by enrichment")
    r = await calculate_by_name("total_revenue")
    # 60 * 1000 (Nike) + 10 * 800 (Bata at i=0,3,6,...,27) + 10 * 1500 (Vintage)
    # Bata: i in {0,3,6,9,12,15,18,21,24,27} = 10 sales
    expected_revenue = 60 * 1000 + 10 * 800 + 10 * 1500
    check("total_revenue", r.value, float(expected_revenue))

    r = await calculate_by_name("total_orders")
    check("total_orders (60 + 10 + 10)", r.value, 80)

    r = await calculate_by_name("total_customers")
    check("total_customers (3 distinct)", r.value, 3)

    # ---- 8. All other v1/v2 KPIs still execute (no breakage) ----
    print()
    print("[8] Zero KPI regressions from enrichment")
    all_kpis = await list_kpis(enabled_only=True)
    pre_existing = [
        k for k in all_kpis
        if k.kpi_category not in ("inventory", "forecast")
    ]
    broken = 0
    for kpi in pre_existing:
        result = await calculate_by_name(kpi.id)
        if result.error and "no uploaded data" not in (result.error or "").lower():
            broken += 1
            print(_safe(f"    [FAIL] {kpi.id}: {result.error}"))
    check("zero pre-existing KPIs broken", broken, 0)

    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
