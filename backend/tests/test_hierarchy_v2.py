"""Hierarchy v2 E2E — Men / Women / Children structure.

Verifies:
  1. Classifier produces (Men|Women|Children) × spec'd lines × spec'd types
  2. All 8 Men lines + 7 Women lines + 7 Children lines exist as nodes
     even when no product yet maps to them (drilldown safety)
  3. SKU codes follow MSN/WHL/CHS-style prefix
  4. Re-sync is idempotent — existing SKU codes stay stable
  5. NEW v2 KPIs return correct aggregations
  6. EVERY pre-existing (v1) KPI still works — zero loss
  7. Headline KPI totals are byte-identical to a v2-free run
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_v2_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_v2_test_cache.json"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:60s} got={got!r:35s} want={want!r}")


def assert_true(label, condition):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name, list_kpis
    from app.hierarchy import (
        seed_default_business, sync_product_master_from_data,
        sync_product_sku_master, list_v2_tree, list_sku_master, classify_to_v2,
    )
    from app.time_engine import invalidate_cache

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # 9-row dataset — covers all 3 classes + multiple lines per class.
    sales = [
        # Men
        {"Date": "2026-05-19", "Total Amount": 1500.0, "Party Name": "A", "Order No": "O1", "Product Name": "Nike Running Shoes Mens"},
        {"Date": "2026-05-19", "Total Amount": 2000.0, "Party Name": "A", "Order No": "O2", "Product Name": "Bata Casual Slippers Mens"},
        {"Date": "2026-05-18", "Total Amount": 800.0,  "Party Name": "B", "Order No": "O3", "Product Name": "Mens Brown Sandals"},
        {"Date": "2026-05-17", "Total Amount": 1100.0, "Party Name": "C", "Order No": "O4", "Product Name": "Liberty Formal Oxford"},
        {"Date": "2026-05-16", "Total Amount": 900.0,  "Party Name": "D", "Order No": "O5", "Product Name": "Puma High Top Sneakers"},
        # Women
        {"Date": "2026-05-15", "Total Amount": 2500.0, "Party Name": "E", "Order No": "O6", "Product Name": "Womens Black Block Heels"},
        {"Date": "2026-05-14", "Total Amount": 1800.0, "Party Name": "F", "Order No": "O7", "Product Name": "Ladies Strappy Sandals"},
        # Children
        {"Date": "2026-05-13", "Total Amount": 700.0,  "Party Name": "G", "Order No": "O8", "Product Name": "Boys Black Velcro School Shoes"},
        {"Date": "2026-05-12", "Total Amount": 1200.0, "Party Name": "H", "Order No": "O9", "Product Name": "Kids Disney Character Shoes"},
    ]
    insert_rows("sales", sales, batch_id="b1")
    bump_data_version()
    invalidate_cache()

    sync_v2 = await sync_product_sku_master()
    check("sync distinct products", sync_v2["distinct_products"], 9)
    check("sync new_skus", sync_v2["new_skus"], 9)

    # ---- 1. Classifier maps correctly to spec'd classes/lines/types ----
    print()
    print("[1] Classifier output — Men / Women / Children")
    cases = [
        ("Nike Running Shoes Mens",          "Men",      "Sports Shoes",   "Running Shoes"),
        ("Bata Casual Slippers Mens",        "Men",      "Slippers",       "Slippers"),  # 'casual' matches Casual Shoes? no — slipper wins because slipper keyword appears
        ("Mens Brown Sandals",               "Men",      "Sandals",        "Sandals"),
        ("Liberty Formal Oxford",            "Men",      "Formal Shoes",   "Oxford Formal"),
        ("Puma High Top Sneakers",           "Men",      "Sneakers",       "High Top Sneakers"),
        ("Womens Black Block Heels",         "Women",    "Heels",          "Block Heels"),
        ("Ladies Strappy Sandals",           "Women",    "Sandals",        "Strappy Sandals"),
        ("Boys Black Velcro School Shoes",   "Children", "School Shoes",   "Black Velcro School Shoes"),
        ("Kids Disney Character Shoes",      "Children", "Cartoon/Character Footwear", "Disney Character Shoes"),
    ]
    for name, want_class, want_line, want_type in cases:
        got = classify_to_v2(name)
        check(f"{name!r:42s} class",  got["class"], want_class)
        check(f"{name!r:42s} line",   got["line"],  want_line)
        check(f"{name!r:42s} type",   got["type"],  want_type)

    # ---- 2. Tree structure: spec-compliant 3 classes + correct line counts ----
    print()
    print("[2] Tree structure compliance")
    tree = await list_v2_tree()
    by_level: dict[str, list] = {}
    for n in tree:
        by_level.setdefault(n["level"], []).append(n)

    class_names = {n["name"] for n in by_level.get("class", [])}
    check("3 classes (Men, Women, Children)",
          class_names, {"Men", "Women", "Children"})

    # Lines must exist regardless of whether products mapped to them.
    line_names = {n["name"] for n in by_level.get("line", [])}
    # 8 men + 7 women + 7 children — some line NAMES are shared between
    # classes (e.g. "Sports Shoes" exists for all 3) so we count by
    # (parent_id, name) pairs, not just names.
    line_pairs = {(n["parent_id"], n["name"]) for n in by_level.get("line", [])}
    check("line count = 8 + 7 + 7 = 22", len(line_pairs), 22)

    # Each class has the expected line names
    men_id = next(n["id"] for n in by_level["class"] if n["name"] == "Men")
    women_id = next(n["id"] for n in by_level["class"] if n["name"] == "Women")
    child_id = next(n["id"] for n in by_level["class"] if n["name"] == "Children")
    men_lines = {n["name"] for n in by_level["line"] if n["parent_id"] == men_id}
    women_lines = {n["name"] for n in by_level["line"] if n["parent_id"] == women_id}
    child_lines = {n["name"] for n in by_level["line"] if n["parent_id"] == child_id}
    check("Men has 8 lines (spec)", men_lines, {
        "Casual Shoes", "Formal Shoes", "Sports Shoes", "Sneakers",
        "Sandals", "Slippers", "Loafers", "Boots",
    })
    check("Women has 7 lines (spec)", women_lines, {
        "Heels", "Flats", "Sandals", "Casual Shoes",
        "Sports Shoes", "Slippers", "Ethnic Footwear",
    })
    check("Children has 7 lines (spec)", child_lines, {
        "School Shoes", "Sports Shoes", "Casual Shoes", "Sandals",
        "Slippers", "Party Wear", "Cartoon/Character Footwear",
    })

    # ---- 3. SKU codes use the new class+line prefix scheme ----
    print()
    print("[3] SKU code prefix scheme (class+line)")
    sku_map = {row["product_name"]: row["sku_code"] for row in await list_sku_master()}
    assert_true("Mens Brown Sandals -> SKU-MSD-*",
                sku_map["Mens Brown Sandals"].startswith("SKU-MSD-"))
    assert_true("Puma High Top Sneakers -> SKU-MSN-*",
                sku_map["Puma High Top Sneakers"].startswith("SKU-MSN-"))
    assert_true("Womens Black Block Heels -> SKU-WHL-*",
                sku_map["Womens Black Block Heels"].startswith("SKU-WHL-"))
    assert_true("Boys ... School Shoes -> SKU-CHS-*",
                sku_map["Boys Black Velcro School Shoes"].startswith("SKU-CHS-"))
    assert_true("Liberty Formal Oxford -> SKU-MFS-*",
                sku_map["Liberty Formal Oxford"].startswith("SKU-MFS-"))

    # ---- 4. Re-sync is idempotent ----
    print()
    print("[4] Re-sync stability")
    second = await sync_product_sku_master()
    check("re-sync new_skus = 0", second["new_skus"], 0)
    check("re-sync updated_skus = 9", second["updated_skus"], 9)
    after = {row["product_name"]: row["sku_code"] for row in await list_sku_master()}
    for k, v in sku_map.items():
        check(f"SKU stable: {k[:30]}", after[k], v)

    # ---- 5. v2 KPIs return correct values ----
    # Total sales = 1500+2000+800+1100+900+2500+1800+700+1200 = 12500
    # By class: Men = 6300 (5 rows), Women = 4300 (2 rows), Children = 1900 (2 rows)
    print()
    print("[5] v2 KPIs")
    r = await calculate_by_name("sales_by_class_v2")
    rows = {row["label"]: row["value"] for row in r.rows}
    check("sales_by_class Men",      rows.get("Men"),      6300.0)
    check("sales_by_class Women",    rows.get("Women"),    4300.0)
    check("sales_by_class Children", rows.get("Children"), 1900.0)

    r = await calculate_by_name("sales_by_need")
    need_rows = {row["label"]: row["value"] for row in r.rows}
    check("sales_by_need Fashion total", need_rows.get("Fashion"), 12500.0)

    r = await calculate_by_name("sales_by_family")
    fam_rows = {row["label"]: row["value"] for row in r.rows}
    check("sales_by_family Footwear total", fam_rows.get("Footwear"), 12500.0)

    r = await calculate_by_name("top_sku")
    check("top_sku value = highest single SKU revenue", r.rows[0]["value"], 2500.0)  # Womens Block Heels

    # ---- 6. PRESERVATION: every pre-existing KPI still works ----
    print()
    print("[6] KPI preservation — every v1 KPI still executes")
    all_kpis = await list_kpis(enabled_only=True)
    v1_kpis = [k for k in all_kpis if k.kpi_category != "hierarchy_v2"]
    broken = 0
    for kpi in v1_kpis:
        result = await calculate_by_name(kpi.id)
        if result.error and "no uploaded data" not in (result.error or "").lower():
            broken += 1
            print(f"    [FAIL] {kpi.id}: {result.error}")
    check("zero v1 KPIs broken", broken, 0)

    # ---- 7. Headline totals unchanged ----
    print()
    print("[7] Headline totals unchanged")
    r = await calculate_by_name("total_revenue")
    check("total_revenue", r.value, 12500.0)
    r = await calculate_by_name("total_orders")
    check("total_orders", r.value, 9)
    r = await calculate_by_name("total_customers")
    check("total_customers", r.value, 8)
    r = await calculate_by_name("top_product")
    check("top_product value", r.rows[0]["value"], 2500.0)

    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
