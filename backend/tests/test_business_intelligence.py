"""Business Intelligence E2E — hierarchy × time × top-N combinations,
inventory list KPIs, forecast top-N, and graceful-empty narration.

Critical claims verified:
  1. Questions like "top performing products this week" match the new
     dataset-relative windowed KPI (not the all-time fallback).
  2. Hierarchy-aware questions ("best men products") return the right
     class scope.
  3. Inventory & forecast results are persisted to disk (sku_inventory
     and sku_forecast tables survive module reload — no in-memory state).
  4. When a list-output KPI returns zero rows, the narrator delivers a
     graceful executive-style fallback message — NEVER "no sales found".
  5. The user-facing SSE payload still contains no formula, no SQL, no
     internal column names.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_bi_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_bi_test_cache.json"
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


class CapturingEmitter:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    async def emit(self, event: str, data):
        self.events.append((event, data))

    async def comment(self, text: str):
        return

    async def close(self):
        return


async def main():
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import init_database, insert_rows, bump_data_version
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name, match_kpi
    from app.hierarchy import (
        seed_default_business, sync_product_master_from_data, sync_product_sku_master,
    )
    from app.enrichment import refresh_inventory, refresh_forecast
    from app.analytics_engine import TurnState, run_query_turn
    from app.time_engine import invalidate_cache

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()
    await seed_default_business()

    # Multi-class footwear dataset spanning ~45 dataset days.
    sales = []
    # Men - Nike high-velocity (last 30 days, varied amounts)
    for i in range(30):
        d = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": d, "Total Amount": 1000.0 + (i % 5) * 100,
            "Party Name": f"Cust{i % 4}", "Order No": f"MN-{i}",
            "Product Name": "Nike Running Shoes Mens",
        })
    # Men - Bata (only this-week activity)
    for i in range(5):
        d = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": d, "Total Amount": 600.0,
            "Party Name": "Cust5", "Order No": f"BT-{i}",
            "Product Name": "Bata Casual Slippers Mens",
        })
    # Women - Block Heels (this week + last week)
    for i in range(10):
        d = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": d, "Total Amount": 2500.0,
            "Party Name": "Cust6", "Order No": f"WH-{i}",
            "Product Name": "Womens Black Block Heels",
        })
    # Children - School Shoes
    for i in range(7):
        d = (date(2026, 5, 19) - timedelta(days=i)).isoformat()
        sales.append({
            "Date": d, "Total Amount": 700.0,
            "Party Name": "Cust7", "Order No": f"CS-{i}",
            "Product Name": "Boys Black Velcro School Shoes",
        })

    insert_rows("sales", sales, batch_id="b1")
    bump_data_version(); invalidate_cache()
    await sync_product_master_from_data()
    await sync_product_sku_master()
    await refresh_inventory()
    await refresh_forecast()

    # ---------- 1. New windowed KPIs match the right questions ----------
    print("[1] Matcher routes 'top products this week'-style questions")
    m = await match_kpi("top performing products this week")
    assert_true(
        "'top performing products this week' matches a windowed KPI",
        m is not None and m.kpi.id in ("top_products_last_7_days", "top_products_today"),
        hint=f"matched={m.kpi.id if m else None}",
    )
    m = await match_kpi("best selling products this week")
    assert_true(
        "'best selling products this week' matches windowed KPI",
        m is not None and m.kpi.id in ("top_products_last_7_days", "top_products_today"),
    )
    m = await match_kpi("best men products")
    assert_true("'best men products' matches top_men_products", m is not None and m.kpi.id == "top_men_products")
    m = await match_kpi("top women footwear")
    assert_true("'top women footwear' matches top_women_products", m is not None and m.kpi.id == "top_women_products")
    m = await match_kpi("top kids footwear")
    assert_true("'top kids footwear' matches top_children_products", m is not None and m.kpi.id == "top_children_products")
    m = await match_kpi("low stock items")
    assert_true("'low stock items' matches low_stock_items list KPI", m is not None and m.kpi.id == "low_stock_items")
    m = await match_kpi("which products are not selling")
    assert_true("'which products are not selling' matches dead_stock_items", m is not None and m.kpi.id == "dead_stock_items")

    # ---------- 2. KPI execution returns realistic results ----------
    print()
    print("[2] New combo KPIs produce useful answers")
    r = await calculate_by_name("top_products_last_7_days")
    assert_true("top_products_last_7_days has rows", len(r.rows) >= 1)
    # Womens Heels should top last-7-days (10 sales × 2500 = 25000) over
    # Nike Mens (7 sales × ~1000 = ~7000 in last 7 days). Verify.
    top_label = (r.rows[0] or {}).get("label", "")
    assert_true(
        "top_products_last_7_days top is Women's Heels",
        "Womens Black Block Heels" in top_label,
        hint=f"top label was {top_label!r}",
    )

    r = await calculate_by_name("top_men_products")
    assert_true("top_men_products produced rows", len(r.rows) >= 1)
    # Top Men's product should be Nike Running Shoes (30 sales × ~1000+).
    assert_true(
        "Men's top is Nike Running Shoes",
        "Nike Running Shoes" in (r.rows[0] or {}).get("label", ""),
    )
    # No Women's product should appear in the Men's result.
    for row in r.rows:
        assert_true(
            f"Men's top row {row.get('label')!r} is NOT a Women's product",
            "Womens" not in (row.get("label") or "")
            and "Ladies" not in (row.get("label") or ""),
        )

    r = await calculate_by_name("top_women_products")
    assert_true(
        "Women's top is Black Block Heels",
        "Block Heels" in (r.rows[0] or {}).get("label", ""),
    )

    r = await calculate_by_name("top_children_products")
    assert_true(
        "Children's top is School Shoes",
        "School Shoes" in (r.rows[0] or {}).get("label", ""),
    )

    # ---------- 3. Inventory list KPIs surface real SKU detail ----------
    print()
    print("[3] Inventory list KPIs return SKU detail")
    r = await calculate_by_name("dead_stock_items")
    # In our dataset there are NO dead-stock items (every product had a
    # sale in the last 30 days). The narrator should NOT crash and the
    # answer should be a graceful no-flag message.
    answer_dead = await _narrate_for(run_query_turn, "which products are not selling")
    assert_true(
        "dead-stock answer is graceful (no 'no sales' / 'no data')",
        "no sales found" not in answer_dead.lower()
        and "no data" not in answer_dead.lower(),
        hint=f"answer={answer_dead!r}",
    )
    assert_true(
        "dead-stock answer mentions healthy / no flagged items",
        any(w in answer_dead.lower() for w in
            ("healthy", "no items currently", "no flagged", "no inventory")),
        hint=f"answer={answer_dead!r}",
    )

    # ---------- 4. Forecast top KPI ----------
    print()
    print("[4] Forecast-driven top KPI")
    r = await calculate_by_name("top_skus_forecast_7d")
    assert_true("top_skus_forecast_7d produced rows", len(r.rows) >= 1)
    assert_true(
        "top forecast row has a SKU label",
        (r.rows[0].get("label") or "").startswith("SKU-"),
    )

    # ---------- 5. Persistence: enrichment survives module reload ----------
    print()
    print("[5] Enrichment tables persist across module reload")
    # Clear app.* modules to simulate restart, then re-import and read.
    for mod_name in list(sys.modules):
        if mod_name.startswith("app."):
            del sys.modules[mod_name]
    from app.infrastructure import init_database as _init_again, fetch_one as _fetch_one  # noqa
    await _init_again()      # idempotent — should NOT wipe tables
    inv_row = await _fetch_one("SELECT COUNT(*) AS n FROM sku_inventory")
    fc_row  = await _fetch_one("SELECT COUNT(*) AS n FROM sku_forecast")
    sku_row = await _fetch_one("SELECT COUNT(*) AS n FROM product_sku_master")
    check("sku_inventory rows persisted across reload",  int(inv_row["n"]) > 0, True)
    check("sku_forecast rows persisted across reload",   int(fc_row["n"]) > 0, True)
    check("product_sku_master rows persisted",           int(sku_row["n"]) > 0, True)

    # ---------- 6. No leak: combo KPI answers stay user-safe ----------
    print()
    print("[6] No formula/SQL/internal leakage in new combo KPI responses")
    from app.analytics_engine import TurnState as _TS, run_query_turn as _rqt
    forbidden = ["formula", "sql_used", "required_columns",
                 "SELECT ", "FROM sales", "JOIN ", '"Product Name"',
                 "product_sku_master", "product_hierarchy_v2"]
    for q in ["top men products", "best women footwear",
              "top performing products this week", "low stock items"]:
        state = _TS(question=q)
        em = CapturingEmitter()
        await _rqt(state, em)
        import json as _json
        blob = _json.dumps([{"event": e, "data": d} for e, d in em.events], default=str)
        leaks = [tok for tok in forbidden if tok in blob]
        assert_true(
            f"no leaks for {q!r}",
            len(leaks) == 0,
            hint=f"leaked: {leaks}",
        )

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


async def _narrate_for(run_query_turn_fn, question: str) -> str:
    """Run a question through the pipeline and return the final_answer string."""
    from app.analytics_engine import TurnState
    em = CapturingEmitter()
    state = TurnState(question=question)
    result = await run_query_turn_fn(state, em)
    return (result.final_answer or "").encode("ascii", "replace").decode("ascii")


if __name__ == "__main__":
    asyncio.run(main())
