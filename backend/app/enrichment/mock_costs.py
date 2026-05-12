"""Cost + quantity backfill for profit / margin / unit-velocity analytics.

Two enrichment layers, derived deterministically from the real sales data:

  1. product_cost_master — one row per Product Name.
     unit_cost = avg_sale_price * cost_ratio
     where cost_ratio depends on the class (Men/Women/Children) and line
     (Heels run thinner margin, Sandals run wider, etc.). Stamp source =
     'synthetic'.

  2. Quantity backfill on sales/purchase — for rows where Quantity is
     NULL/0, derive it deterministically from Total Amount and the
     product's avg_sale_price. e.g. if Total Amount ≈ 2× avg_sale_price,
     Quantity = 2. Default = 1. is_mock_quantity = 1 flag for audit.

Both layers are idempotent. Real-supplied quantities and user-set costs
(via the future cost-upload route) are never overwritten.

Margin bands (line-aware, realistic Indian retail footwear):
   Heels / Formal Shoes / Boots / Loafers     →  35-45 % margin
   Sneakers / Sports Shoes / Casual Shoes     →  40-50 % margin
   Sandals / Slippers / School Shoes          →  45-55 % margin
   Flats / Party Wear / Cartoon / Ethnic      →  40-50 % margin
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

from app.infrastructure import db_path, fetch_all, fetch_one


_log = logging.getLogger("agentic_ai.enrichment.mock_costs")


# (line keyword fragment → margin band low, high)  margin = (price - cost) / price
_MARGIN_BANDS_BY_LINE: tuple[tuple[str, float, float], ...] = (
    ("heels",    0.35, 0.45),
    ("formal",   0.35, 0.45),
    ("boots",    0.35, 0.45),
    ("loafers",  0.35, 0.45),
    ("sneakers", 0.40, 0.50),
    ("sports",   0.40, 0.50),
    ("casual",   0.40, 0.50),
    ("sandals",  0.45, 0.55),
    ("slippers", 0.45, 0.55),
    ("school",   0.45, 0.55),
    ("flats",    0.40, 0.50),
    ("party",    0.40, 0.50),
    ("cartoon",  0.40, 0.50),
    ("character", 0.40, 0.50),
    ("ethnic",   0.40, 0.50),
)
_DEFAULT_MARGIN_BAND = (0.40, 0.50)


def _margin_band_for(line_name: str | None) -> tuple[float, float]:
    if not line_name:
        return _DEFAULT_MARGIN_BAND
    ln = line_name.lower()
    for needle, lo, hi in _MARGIN_BANDS_BY_LINE:
        if needle in ln:
            return (lo, hi)
    return _DEFAULT_MARGIN_BAND


def _deterministic_margin(product_name: str, band: tuple[float, float]) -> float:
    """Pick a margin within the band using a hash of the product name —
    same product always gets the same margin. Returns a fraction 0..1."""
    h = int(hashlib.md5(product_name.encode("utf-8")).hexdigest()[:8], 16)
    lo, hi = band
    return lo + (h / 0xFFFFFFFF) * (hi - lo)


async def refresh_product_costs() -> dict[str, int]:
    """Rebuild product_cost_master from sales data.

    For each distinct Product Name:
      • avg_sale_price = AVG("Total Amount") in sales rows that have qty=1
        or no qty (most common case — total = price × 1 unit).
      • cost_ratio = 1 − margin from band
      • unit_cost  = avg_sale_price × cost_ratio

    User-supplied rows (source='manual') are preserved.
    """
    # Pull avg sale price from sales, classified by line via v2 master.
    rows = await fetch_all(
        'SELECT s."Product Name" AS product_name, '
        '       AVG(s."Total Amount") AS avg_price, '
        '       COUNT(*) AS n_sales, '
        '       l.name AS line_name '
        'FROM sales s '
        'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
        'LEFT JOIN product_hierarchy_v2 l ON l.id = pm.line_id '
        'WHERE s."Product Name" IS NOT NULL AND TRIM(s."Product Name") <> \'\' '
        '  AND s."Total Amount" IS NOT NULL AND s."Total Amount" > 0 '
        'GROUP BY s."Product Name", l.name'
    )
    if not rows:
        _log.info("cost refresh: no products to price")
        return {"products_priced": 0, "skipped_manual": 0}

    new_count = 0
    skipped_manual = 0
    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")

        # Preserve manually-set costs.
        cur = conn.execute(
            "SELECT product_name FROM product_cost_master WHERE source = 'manual'"
        )
        manual = {r[0] for r in cur.fetchall()}
        cur.close()

        # Wipe existing synthetic rows so we can fully recompute.
        conn.execute("DELETE FROM product_cost_master WHERE source = 'synthetic'")

        for r in rows:
            product_name = r["product_name"]
            if product_name in manual:
                skipped_manual += 1
                continue
            avg_price = float(r["avg_price"] or 0)
            if avg_price <= 0:
                continue
            band = _margin_band_for(r.get("line_name"))
            margin = _deterministic_margin(product_name, band)
            unit_cost = round(avg_price * (1.0 - margin), 2)

            conn.execute(
                "INSERT INTO product_cost_master "
                "(product_name, unit_cost, avg_sale_price, margin_pct, source) "
                "VALUES (?, ?, ?, ?, 'synthetic')",
                (product_name, unit_cost, round(avg_price, 2), round(margin * 100, 2)),
            )
            new_count += 1

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _log.exception("cost refresh failed")
        return {"products_priced": 0, "skipped_manual": skipped_manual}
    finally:
        conn.close()

    _log.info("cost refresh: priced=%d skipped_manual=%d", new_count, skipped_manual)
    return {"products_priced": new_count, "skipped_manual": skipped_manual}


async def backfill_quantities() -> dict[str, int]:
    """For each sales/purchase row with NULL or 0 Quantity, derive a
    deterministic 1-3 unit count from Total Amount and product's avg
    price. is_mock_quantity = 1 flag is set.

    Real-supplied quantities (already > 0, is_mock_quantity = 0) are
    left untouched.
    """
    totals = {"sales_filled": 0, "purchase_filled": 0}

    # Build a lookup product_name → avg_price from cost master (faster than
    # re-aggregating sales). Falls back to per-row default if unknown.
    cost_rows = await fetch_all(
        "SELECT product_name, avg_sale_price FROM product_cost_master"
    )
    price_lookup: dict[str, float] = {
        r["product_name"]: float(r["avg_sale_price"] or 0)
        for r in cost_rows
    }

    for table in ("sales", "purchase"):
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cur = conn.execute(
                f'SELECT id, "Product Name", "Total Amount" FROM "{table}" '
                f'WHERE ("Quantity" IS NULL OR "Quantity" <= 0)'
            )
            empties = cur.fetchall()
            cur.close()
            if not empties:
                continue

            conn.execute("BEGIN")
            updated = 0
            for row_id, product_name, total_amount in empties:
                total = float(total_amount or 0)
                if total <= 0:
                    qty = 1
                else:
                    avg_price = price_lookup.get(product_name) or total
                    if avg_price <= 0:
                        qty = 1
                    else:
                        ratio = total / avg_price
                        if ratio >= 2.5:
                            qty = 3
                        elif ratio >= 1.5:
                            qty = 2
                        else:
                            qty = 1
                conn.execute(
                    f'UPDATE "{table}" SET "Quantity" = ?, '
                    f'is_mock_quantity = 1 WHERE id = ?',
                    (qty, row_id),
                )
                updated += 1
            conn.execute("COMMIT")
            totals[f"{table}_filled"] = updated
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            _log.exception("quantity backfill failed for %s", table)
        finally:
            conn.close()

    _log.info("quantity backfill: %s", totals)
    return totals


async def cost_master_snapshot() -> dict[str, Any]:
    row = await fetch_one(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN source = 'manual'    THEN 1 ELSE 0 END) AS manual, "
        "       SUM(CASE WHEN source = 'synthetic' THEN 1 ELSE 0 END) AS synthetic, "
        "       AVG(margin_pct)  AS avg_margin_pct, "
        "       MIN(margin_pct)  AS min_margin_pct, "
        "       MAX(margin_pct)  AS max_margin_pct "
        "FROM product_cost_master"
    )
    return dict(row or {"total": 0})


async def list_product_costs(limit: int = 500) -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT product_name, unit_cost, avg_sale_price, margin_pct, "
        "       source, last_refreshed_at "
        "FROM product_cost_master ORDER BY margin_pct DESC LIMIT ?",
        (max(1, min(int(limit), 5000)),),
    )
