"""Forecast intelligence — 14-day forward projection per SKU.

Approach (deterministic, no LLM, no opaque ML):
  For each SKU in product_sku_master:
    1. Compute mean qty/day and mean revenue/day over the trailing 30
       days of real sales (relative to MAX(Date) in the dataset, not
       the wall clock).
    2. Project that mean forward 14 days from MAX(Date) + 1.
    3. Confidence ∈ [0, 1] proportional to how many of the trailing 30
       days had ≥ 1 sale (more activity = more confidence).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from app.infrastructure import db_path, fetch_all, fetch_one
from app.time_engine import get_dataset_current_date


_log = logging.getLogger("agentic_ai.enrichment.forecast")


_TRAILING_DAYS = 30
_FORECAST_HORIZON = 14


def _iso(d: date) -> str:
    return d.isoformat()


def _parse_iso(s: str | None) -> date | None:
    if not s or not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


async def refresh_forecast() -> dict[str, int]:
    """Recompute sku_forecast for every SKU. Idempotent."""
    today_iso = await get_dataset_current_date()
    today = _parse_iso(today_iso) if today_iso else None
    if today is None:
        _log.info("forecast refresh: no uploaded data — clearing forecast")
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            conn.execute("DELETE FROM sku_forecast")
        finally:
            conn.close()
        return {"skus": 0, "rows": 0}

    trailing_start = today - timedelta(days=_TRAILING_DAYS - 1)
    horizon_start = today + timedelta(days=1)

    skus = await fetch_all(
        "SELECT sku_code, product_name FROM product_sku_master "
        "ORDER BY sku_code"
    )
    if not skus:
        return {"skus": 0, "rows": 0}

    n_skus = 0
    n_rows = 0
    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM sku_forecast")

        for r in skus:
            sku_code = r["sku_code"]
            product_name = r["product_name"]

            cur = conn.execute(
                'SELECT '
                '  COUNT(*) AS n_sales, '
                '  COALESCE(SUM("Total Amount"), 0) AS total_rev, '
                '  COUNT(DISTINCT "Date") AS active_days '
                'FROM sales '
                'WHERE "Product Name" = ? '
                '  AND "Date" BETWEEN ? AND ? '
                '  AND "Date" GLOB \'????-??-??\'',
                (product_name, _iso(trailing_start), _iso(today)),
            )
            r2 = cur.fetchone()
            cur.close()
            n_sales = int(r2[0] or 0)
            total_rev = float(r2[1] or 0)
            active_days = int(r2[2] or 0)

            if n_sales == 0:
                # No forecast for inactive SKUs — leave rows out and the
                # AI will report zero / "no projected demand" cleanly.
                continue

            mean_qty_per_day = n_sales / _TRAILING_DAYS
            mean_rev_per_day = total_rev / _TRAILING_DAYS

            # Confidence: fraction of trailing days that had activity,
            # capped at 0.95 to never imply certainty.
            confidence = min(0.95, active_days / _TRAILING_DAYS)

            for offset in range(_FORECAST_HORIZON):
                forecast_d = horizon_start + timedelta(days=offset)
                conn.execute(
                    "INSERT INTO sku_forecast "
                    "(sku_code, forecast_date, forecast_qty, forecast_revenue, "
                    " method, confidence) "
                    "VALUES (?, ?, ?, ?, 'trailing_30', ?)",
                    (sku_code, _iso(forecast_d),
                     mean_qty_per_day, mean_rev_per_day, confidence),
                )
                n_rows += 1
            n_skus += 1

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _log.exception("forecast refresh failed")
        return {"skus": n_skus, "rows": n_rows}
    finally:
        conn.close()

    _log.info(
        "forecast refresh: skus=%d rows=%d horizon=%d",
        n_skus, n_rows, _FORECAST_HORIZON,
    )
    return {"skus": n_skus, "rows": n_rows}


async def list_forecast_for_sku(sku_code: str) -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT forecast_date, forecast_qty, forecast_revenue, "
        "       method, confidence, generated_at "
        "FROM sku_forecast WHERE sku_code = ? "
        "ORDER BY forecast_date",
        (sku_code,),
    )


async def forecast_summary() -> dict[str, Any]:
    """Aggregated forecast for the dashboard."""
    today_iso = await get_dataset_current_date()
    today = _parse_iso(today_iso) if today_iso else None
    if today is None:
        return {
            "has_data": False,
            "next_7_days_revenue": 0.0,
            "next_30_days_revenue": 0.0,
            "covered_skus": 0,
        }
    end_7 = today + timedelta(days=7)
    end_30 = today + timedelta(days=30)

    r7 = await fetch_one(
        "SELECT COALESCE(SUM(forecast_revenue), 0) AS rev "
        "FROM sku_forecast WHERE forecast_date BETWEEN ? AND ?",
        (_iso(today + timedelta(days=1)), _iso(end_7)),
    )
    r30 = await fetch_one(
        "SELECT COALESCE(SUM(forecast_revenue), 0) AS rev "
        "FROM sku_forecast WHERE forecast_date BETWEEN ? AND ?",
        (_iso(today + timedelta(days=1)), _iso(end_30)),
    )
    sku_count = await fetch_one("SELECT COUNT(DISTINCT sku_code) AS n FROM sku_forecast")
    return {
        "has_data":              True,
        "anchor_date":           today_iso,
        "next_7_days_revenue":   float((r7 or {}).get("rev") or 0),
        "next_30_days_revenue":  float((r30 or {}).get("rev") or 0),
        "covered_skus":          int((sku_count or {}).get("n") or 0),
        "method":                "trailing_30_day_mean",
    }
