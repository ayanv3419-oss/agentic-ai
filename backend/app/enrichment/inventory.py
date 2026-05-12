"""Inventory intelligence — derived from real sales velocity.

The platform does not have an inventory upload format, so this module
synthesizes a realistic stock snapshot from the actual sales history:

  - avg_daily_sales       = total qty sold / observed-day-span
  - avg_daily_revenue     = total revenue / observed-day-span
  - on_hand_qty (seeded)  = max(10, round(avg_daily_sales * 30))
                              (≈ one month of cover at observed pace)
  - reorder_level         = round(avg_daily_sales * 7)
                              (one week of cover)
  - days_of_cover         = on_hand_qty / avg_daily_sales
  - status:
        avg_daily_sales == 0 OR no sales in last 30 days → 'dead'
        days_of_cover < 7                                 → 'low'
        days_of_cover > 60                                → 'overstocked'
        else                                              → 'ok'

The point of the synthesis is to give the AI and dashboard a realistic
inventory layer to reason over WITHOUT pretending real stock counts
exist. Every row is tagged `source='synthetic'` for clarity.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.infrastructure import db_path, fetch_all


_log = logging.getLogger("agentic_ai.enrichment.inventory")


# Rules expressed as constants so they're easy to tune.
_COVER_DAYS_TARGET   = 30          # initial stock target in days of cover
_REORDER_DAYS        = 7           # reorder when below 7 days of cover
_OVERSTOCK_DAYS      = 60          # over 60 days of cover = overstocked
_DEAD_INACTIVITY_DAYS = 30         # no sales in 30+ days = dead stock
_MIN_INITIAL_STOCK   = 10          # never seed below this


async def refresh_inventory() -> dict[str, int]:
    """Rebuild sku_inventory from real sales history.

    Returns counts: {skus, low, overstocked, dead, ok}.
    """
    # We pull SKU code via product_sku_master to keep the row identifier
    # aligned with the synthetic hierarchy v2 layer.
    rows = await fetch_all(
        "SELECT pm.sku_code, pm.product_name "
        "FROM product_sku_master pm ORDER BY pm.sku_code"
    )
    if not rows:
        _log.info("inventory refresh: no SKUs in master — nothing to do")
        return {"skus": 0, "low": 0, "overstocked": 0, "dead": 0, "ok": 0}

    counts = {"skus": 0, "low": 0, "overstocked": 0, "dead": 0, "ok": 0}

    # Wipe + rebuild in a single write transaction. SQLite plain sqlite3
    # driver is fastest here (same approach as insert_rows in infrastructure).
    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM sku_inventory")

        for r in rows:
            sku_code = r["sku_code"]
            product_name = r["product_name"]

            # Fetch sales aggregates for THIS product. Sales count + revenue
            # + date span over rows in the sales table tagged with this name.
            cur = conn.execute(
                'SELECT '
                '   COUNT(*) AS n, '
                '   COALESCE(SUM("Total Amount"), 0) AS total_rev, '
                '   MIN("Date") AS first_date, '
                '   MAX("Date") AS last_date '
                'FROM sales '
                'WHERE "Product Name" = ? '
                '  AND "Date" IS NOT NULL AND "Date" GLOB \'????-??-??\'',
                (product_name,),
            )
            r2 = cur.fetchone()
            cur.close()
            n_sales = int(r2[0] or 0)
            total_revenue = float(r2[1] or 0)
            first_date = r2[2]
            last_date = r2[3]

            avg_daily_sales = 0.0
            avg_daily_revenue = 0.0
            if n_sales > 0 and first_date and last_date:
                # Day-span (inclusive): use SQLite julianday diff.
                cur = conn.execute(
                    "SELECT MAX(1, CAST(julianday(?) - julianday(?) AS INTEGER) + 1) AS days",
                    (last_date, first_date),
                )
                days = int(cur.fetchone()[0] or 1)
                cur.close()
                avg_daily_sales = n_sales / days
                avg_daily_revenue = total_revenue / days

            # Dead-stock check: how many days since the last sale?
            days_since_last = None
            if last_date:
                cur = conn.execute(
                    "SELECT CAST(julianday(date('now')) - julianday(?) AS INTEGER) AS d",
                    (last_date,),
                )
                d = cur.fetchone()[0]
                cur.close()
                days_since_last = int(d) if d is not None else None

            # Seed stock: 30 days of cover, with a floor.
            on_hand_qty = max(_MIN_INITIAL_STOCK, round(avg_daily_sales * _COVER_DAYS_TARGET))
            reorder_level = max(1, round(avg_daily_sales * _REORDER_DAYS))
            days_of_cover = (on_hand_qty / avg_daily_sales) if avg_daily_sales > 0 else None

            if avg_daily_sales == 0 or (days_since_last is not None and days_since_last > _DEAD_INACTIVITY_DAYS):
                status = "dead"
            elif days_of_cover is not None and days_of_cover < _REORDER_DAYS:
                status = "low"
            elif days_of_cover is not None and days_of_cover > _OVERSTOCK_DAYS:
                status = "overstocked"
            else:
                status = "ok"

            conn.execute(
                "INSERT INTO sku_inventory "
                "(sku_code, product_name, on_hand_qty, reorder_level, "
                " avg_daily_sales, avg_daily_revenue, days_of_cover, "
                " status, last_refreshed_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'synthetic')",
                (sku_code, product_name, int(on_hand_qty), int(reorder_level),
                 avg_daily_sales, avg_daily_revenue, days_of_cover, status),
            )
            counts["skus"] += 1
            counts[status] = counts.get(status, 0) + 1

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _log.exception("inventory refresh failed")
        return counts
    finally:
        conn.close()

    _log.info("inventory refresh: %s", counts)
    return counts


async def list_inventory(status: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT sku_code, product_name, on_hand_qty, reorder_level, "
        "       avg_daily_sales, avg_daily_revenue, days_of_cover, "
        "       status, last_refreshed_at, source FROM sku_inventory"
    )
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY status, sku_code"
    return await fetch_all(sql, params)


async def inventory_snapshot() -> dict[str, Any]:
    """Headline counts + per-status sample for the inventory dashboard."""
    by_status = await fetch_all(
        "SELECT status, COUNT(*) AS n FROM sku_inventory GROUP BY status"
    )
    counts: dict[str, int] = {row["status"]: int(row["n"]) for row in by_status}
    return {
        "ok":          counts.get("ok", 0),
        "low":         counts.get("low", 0),
        "overstocked": counts.get("overstocked", 0),
        "dead":        counts.get("dead", 0),
        "total":       sum(counts.values()),
        "by_status":   counts,
    }
