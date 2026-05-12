"""Cost master + quantity backfill + 10 advanced KPI tests.

Verifies the new data + KPI layer added so the AI can answer:
  • profit / margin per product
  • declining / growth-driving products
  • fast-moving + risk SKUs
  • sub-hierarchy (top Women lines / top Men lines)
  • executive summary
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB    = _BACKEND_DIR / "_costs_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_costs_test_cache.json"
os.environ["FINANCIAL_DB_PATH"]   = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)
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
    print(f"  [{mark}] {label:60s} got={g:40s} want={w}")


def assert_true(label: str, condition: bool):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok)
    _failed += int(not ok)
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label}")


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import (
        init_database, insert_rows, bump_data_version, fetch_all, fetch_one,
    )
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name
    from app.hierarchy import (
        seed_default_business, sync_product_master_from_data,
        sync_product_sku_master,
    )
    from app.enrichment import (
        backfill_missing_product_names,
        backfill_quantities,
        cost_master_snapshot,
        list_product_costs,
        refresh_forecast,
        refresh_inventory,
        refresh_product_costs,
    )
    from app.time_engine import invalidate_cache
    from datetime import date, timedelta

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # Insert realistic 30-day footwear sales spread across class/line.
    # Anchor to 2026-05-19 as dataset_today so dataset-relative windows
    # are deterministic.
    base = date(2026, 5, 19)
    sales: list[dict] = []
    products_for_men   = [
        ("Nike Running Shoes Mens", 3500),
        ("Bata Casual Slippers Mens", 600),
        ("Liberty Formal Oxford Brown Mens", 2800),
        ("Puma High Top Sneakers Mens", 4500),
    ]
    products_for_women = [
        ("Womens Black Block Heels", 4200),
        ("Ladies Strappy Sandals", 1200),
        ("Womens Ballerina Flats Pink", 1800),
    ]
    products_for_kids  = [
        ("Boys Black Velcro School Shoes", 1500),
        ("Kids Disney Character Shoes", 2200),
    ]
    all_products = products_for_men + products_for_women + products_for_kids

    # Last-7 window: each product sells 2 units/day at avg price
    # Prior-7 window: products sell at differing rates so we can verify
    # declining / growth KPIs.
    order_no = 1

    def add_row(d: date, product: str, price: float, qty: float | None = None):
        nonlocal order_no
        sales.append({
            "Date": d.isoformat(),
            "Total Amount": price,
            "Party Name": f"Cust{order_no % 7}",
            "Order No": f"O{order_no}",
            "Product Name": product,
            "Quantity": qty,
        })
        order_no += 1

    # Days 1-7 (PRIOR week): some products STRONG, others WEAK
    for d_off in range(7, 14):
        d = base - timedelta(days=d_off)
        # Nike: 5 sales in prior week (strong, will decline)
        for _ in range(5):
            add_row(d, "Nike Running Shoes Mens", 3500)
        # Womens Block Heels: 1 sale (weak, will grow)
        add_row(d, "Womens Black Block Heels", 4200)
        # Others: moderate
        for p, price in [("Bata Casual Slippers Mens", 600),
                         ("Boys Black Velcro School Shoes", 1500)]:
            add_row(d, p, price)

    # Days 0-6 (CURRENT week): reverse — Nike weak, Heels strong
    for d_off in range(0, 7):
        d = base - timedelta(days=d_off)
        add_row(d, "Nike Running Shoes Mens", 3500)  # only 1 per day → drop
        for _ in range(5):
            add_row(d, "Womens Black Block Heels", 4200)  # surge
        for p, price in [("Bata Casual Slippers Mens", 600),
                         ("Boys Black Velcro School Shoes", 1500),
                         ("Liberty Formal Oxford Brown Mens", 2800),
                         ("Ladies Strappy Sandals", 1200),
                         ("Puma High Top Sneakers Mens", 4500),
                         ("Womens Ballerina Flats Pink", 1800),
                         ("Kids Disney Character Shoes", 2200)]:
            add_row(d, p, price)

    insert_rows("sales", sales, batch_id="b-cost-test")
    bump_data_version()
    invalidate_cache()
    await backfill_missing_product_names()
    await sync_product_master_from_data()
    await sync_product_sku_master()
    await refresh_inventory()
    await refresh_forecast()

    # ------------------------------------------------------------------
    # 1. Cost master generation
    # ------------------------------------------------------------------
    print("[1] Cost master generation")
    stats = await refresh_product_costs()
    check("products_priced = 9 distinct", stats["products_priced"], 9)
    snap = await cost_master_snapshot()
    check("synthetic count = 9", int(snap["synthetic"] or 0), 9)
    check("manual count = 0",    int(snap["manual"] or 0),    0)
    assert_true("avg_margin_pct between 35 and 55",
                35 <= float(snap["avg_margin_pct"] or 0) <= 55)

    # ------------------------------------------------------------------
    # 2. Margin assignment is deterministic + within band
    # ------------------------------------------------------------------
    print()
    print("[2] Margin bands are line-aware")
    costs = await list_product_costs()
    by_name = {r["product_name"]: r for r in costs}
    # Heels band = 35-45%
    heels = by_name.get("Womens Black Block Heels")
    assert_true("Heels margin in 35-45 band",
                35 <= float(heels["margin_pct"]) <= 45)
    # Slippers band = 45-55%
    slippers = by_name.get("Bata Casual Slippers Mens")
    assert_true("Slippers margin in 45-55 band",
                45 <= float(slippers["margin_pct"]) <= 55)
    # Cost < price always
    for r in costs:
        assert_true(f"cost < price for {r['product_name']}",
                    r["unit_cost"] < r["avg_sale_price"])

    # ------------------------------------------------------------------
    # 3. Deterministic re-run
    # ------------------------------------------------------------------
    print()
    print("[3] Cost regen is deterministic")
    snap1 = await list_product_costs()
    await refresh_product_costs()
    snap2 = await list_product_costs()
    check("byte-identical re-run",
          {r["product_name"]: r["unit_cost"] for r in snap1},
          {r["product_name"]: r["unit_cost"] for r in snap2})

    # ------------------------------------------------------------------
    # 4. Quantity backfill
    # ------------------------------------------------------------------
    print()
    print("[4] Quantity backfill fills NULL/0 rows only")
    qty_stats = await backfill_quantities()
    assert_true("sales_filled > 0", qty_stats["sales_filled"] > 0)
    null_rows = await fetch_one(
        'SELECT COUNT(*) AS n FROM sales WHERE "Quantity" IS NULL OR "Quantity" <= 0'
    )
    check("no NULL quantities left", int(null_rows["n"]), 0)
    # Quantities should be in [1, 3]
    range_check = await fetch_one(
        'SELECT MIN("Quantity") AS mn, MAX("Quantity") AS mx FROM sales'
    )
    assert_true("quantities in [1, 3]",
                1 <= float(range_check["mn"]) <= float(range_check["mx"]) <= 3)

    # ------------------------------------------------------------------
    # 5. Profit / Margin / Loss KPIs
    # ------------------------------------------------------------------
    print()
    print("[5] Per-product profit + margin KPIs")
    hp = await calculate_by_name("highest_profit_products")
    assert_true("highest_profit_products has rows", len(hp.rows) > 0)
    assert_true("top profit product is a real product",
                bool(hp.rows[0].get("label")))
    hm = await calculate_by_name("highest_margin_products")
    assert_true("highest_margin_products has rows", len(hm.rows) > 0)
    # First should be a higher-margin line (Slippers/Sandals/School)
    top_margin_product = hm.rows[0]["label"]
    assert_true(f"top margin product {top_margin_product!r} from a high-margin line",
                any(kw in top_margin_product.lower() for kw in
                    ["slipper", "sandal", "school", "kids", "boys"]))
    lm = await calculate_by_name("loss_making_products")
    check("loss_making_products empty (none are loss-making)", len(lm.rows), 0)

    # ------------------------------------------------------------------
    # 6. Declining + Growth KPIs
    # ------------------------------------------------------------------
    print()
    print("[6] Declining + Growth KPIs detect direction")
    dec = await calculate_by_name("declining_products")
    assert_true("declining_products has Nike at top of decline",
                any("nike" in r["label"].lower() for r in dec.rows[:3]))
    gr = await calculate_by_name("growth_driving_products")
    assert_true("growth_driving_products has Block Heels in top growers",
                any("heel" in r["label"].lower() for r in gr.rows[:3]))

    # ------------------------------------------------------------------
    # 7. Fast-moving + Risk + Sub-hierarchy
    # ------------------------------------------------------------------
    print()
    print("[7] Fast-moving + Risk + Sub-hierarchy KPIs")
    fm = await calculate_by_name("fast_moving_skus")
    assert_true("fast_moving_skus has rows", len(fm.rows) > 0)
    risk = await calculate_by_name("business_risk_skus")
    assert_true("business_risk_skus executes (may be empty)", risk.error is None)
    twl = await calculate_by_name("top_women_lines")
    assert_true("top_women_lines has Heels first",
                len(twl.rows) > 0 and twl.rows[0]["label"] == "Heels")
    tml = await calculate_by_name("top_men_lines")
    assert_true("top_men_lines has rows", len(tml.rows) > 0)

    # ------------------------------------------------------------------
    # 8. Executive summary
    # ------------------------------------------------------------------
    print()
    print("[8] Executive summary digest")
    es = await calculate_by_name("executive_summary")
    assert_true("executive_summary executes", es.error is None)
    assert_true("executive_summary has at least 1 row", len(es.rows) >= 1)
    es_row = es.rows[0]
    assert_true("includes total_revenue field", "total_revenue" in es_row)
    assert_true("includes last_7_revenue field", "last_7_revenue" in es_row)
    assert_true("includes low_stock_count field", "low_stock_count" in es_row)
    assert_true("includes projected_next_7 field", "projected_next_7" in es_row)

    # ------------------------------------------------------------------
    # 9. KPI preservation — all pre-existing KPIs still execute
    # ------------------------------------------------------------------
    print()
    print("[9] Zero regressions on pre-existing KPIs")
    from app.kpi import list_kpis, execute_kpi
    new_ids = {
        "highest_profit_products", "highest_margin_products", "loss_making_products",
        "declining_products", "growth_driving_products",
        "fast_moving_skus", "business_risk_skus",
        "top_women_lines", "top_men_lines", "executive_summary",
    }
    broken = []
    for k in await list_kpis(enabled_only=True):
        if k.id in new_ids:
            continue
        r = await execute_kpi(k)
        if r.error:
            broken.append((k.id, r.error[:80]))
    check("zero pre-existing KPIs broken", len(broken), 0)
    if broken:
        for b in broken[:5]:
            print(f"      {b[0]}: {b[1]}")

    # ------------------------------------------------------------------
    # 10. Real totals unchanged
    # ------------------------------------------------------------------
    print()
    print("[10] Real totals unchanged")
    tr = await calculate_by_name("total_revenue")
    expected = sum(s["Total Amount"] for s in sales)
    check("total_revenue", tr.value, float(expected))

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
