"""Mock product-name backfill for sales/purchase rows missing a Product Name.

When a real sales upload contains rows where the "Product Name" column is
NULL or empty (very common in raw POS exports), the AI has no way to
answer product / category / SKU questions for that revenue. This module
fills those gaps deterministically with realistic footwear product names
drawn from a curated retail pool.

Design principles:
  • DETERMINISTIC — the same source row always gets the same mock name.
    The choice is a hash of (batch_id, primary key id), so re-running the
    backfill is idempotent.
  • NEVER OVERWRITES — rows with a real product name (anything non-empty
    after trimming) are left untouched. Only NULL / "" rows get a name.
  • AUDITABLE — every backfilled row gets is_mock_named = 1 so the user
    can filter mock-named revenue separately from real-named revenue.
  • HIERARCHY-FRIENDLY — every name in the pool maps cleanly into the
    existing v2 hierarchy (Men / Women / Children → Line → Type) so that
    sync_product_master + sync_product_sku_master populate consistently.

After backfill runs, the standard enrichment chain (sync_product_master,
sync_product_sku_master, refresh_inventory, refresh_forecast) picks up
the newly-named rows automatically. The AI can then answer:
  "which products are running low" / "top men products" / etc.
including revenue that originally had no product attribution.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

from app.infrastructure import ALLOWED_TABLES, db_path, fetch_one


_log = logging.getLogger("agentic_ai.enrichment.mock_products")


# 50 realistic footwear product names spanning every Class × Line in the
# v2 hierarchy. Each name is crafted so the existing classify_to_v2()
# routes it to the intended branch without ambiguity.
#
# Distribution (so the mock pool roughly mirrors a real footwear shop):
#   Men:      Casual 2, Formal 3, Sports 3, Sneakers 3, Sandals 2,
#             Slippers 3, Loafers 2, Boots 2                =  20
#   Women:    Heels 4, Flats 2, Sandals 2, Casual 2,
#             Sports 2, Slippers 2, Ethnic 2                =  16
#   Children: School 3, Sports 2, Casual 2, Sandals 1,
#             Slippers 1, Party Wear 1, Cartoon 4           =  14
#                                                  TOTAL    =  50
MOCK_FOOTWEAR_POOL: tuple[str, ...] = (
    # ---- Men ----
    # Casual (2)
    "Bata Casual Lace-Up Tan Mens",
    "Liberty Casual Slip-On Mens",
    # Formal (3)
    "Liberty Formal Oxford Brown Mens",
    "Hush Puppies Formal Derby Black Mens",
    "Mens Formal Brogue Leather",
    # Sports (3)
    "Nike Running Shoes Mens",
    "Adidas Sports Trainers Mens",
    "Asics Gym Training Shoes Mens",
    # Sneakers (3)
    "Puma High Top Sneakers Mens",
    "Converse Canvas Sneakers Mens",
    "Mens Slip-On Sneakers White",
    # Sandals (2)
    "Mens Brown Floater Sandals",
    "Mens Comfort Sandals Tan",
    # Slippers (3)
    "Bata Casual Slippers Mens",
    "Relaxo Flip Flops Mens",
    "Mens House Slippers Brown",
    # Loafers (2)
    "Mens Penny Loafers Brown",
    "Tassel Loafers Mens Black",
    # Boots (2)
    "Mens Chelsea Boots Tan",
    "Mens Ankle Boots Black",

    # ---- Women ----
    # Heels (4)
    "Womens Black Block Heels",
    "Womens Stiletto Heels Red",
    "Womens Wedge Heels Beige",
    "Womens Kitten Heels Pink",
    # Flats (2)
    "Womens Ballerina Flats Pink",
    "Womens Pointed Flats Black",
    # Sandals (2)
    "Ladies Strappy Sandals",
    "Womens Flat Sandals Beige",
    # Casual Shoes (2)
    "Womens Casual Sneakers White",
    "Womens Loafer Slip-On",
    # Sports Shoes (2)
    "Womens Running Shoes Pink",
    "Womens Gym Trainer Grey",
    # Slippers (2)
    "Womens Flip Flops Floral",
    "Womens Home Slippers Pink",
    # Ethnic Footwear (2)
    "Womens Kolhapuri Sandals",
    "Womens Jutti Pink Embroidered",

    # ---- Children ----
    # School Shoes (3)
    "Boys Black Velcro School Shoes",
    "Boys Black Lace School Shoes",
    "Kids White School Shoes",
    # Sports Shoes (2)
    "Kids Running Shoes Blue",
    "Kids Trainers Multi",
    # Casual Shoes (2)
    "Kids Casual Sneakers",
    "Kids Slip-On Casuals",
    # Sandals (1)
    "Kids Velcro Sandals",
    # Slippers (1)
    "Kids Flip Flops Cartoon",
    # Party Wear (1)
    "Kids Party Mary Janes",
    # Cartoon/Character (4)
    "Kids Disney Character Shoes",
    "Kids Marvel Spiderman Shoes",
    "Kids Frozen Elsa Shoes",
    "Kids Barbie Character Shoes",
)

assert len(MOCK_FOOTWEAR_POOL) == 50, "mock pool size sanity check"


def _pick_mock_name(batch_id: str, row_id: int) -> str:
    """Deterministic SHA256 → index into the pool. Same input → same output."""
    seed = f"{batch_id or ''}|{row_id}".encode("utf-8")
    h = hashlib.sha256(seed).hexdigest()
    idx = int(h[:8], 16) % len(MOCK_FOOTWEAR_POOL)
    return MOCK_FOOTWEAR_POOL[idx]


async def backfill_missing_product_names() -> dict[str, int]:
    """For each sales/purchase row whose "Product Name" is NULL or empty,
    assign a deterministic mock footwear product name and stamp is_mock_named=1.

    Real-named rows are never touched. Returns counts per table.
    """
    totals: dict[str, int] = {"sales_filled": 0, "purchase_filled": 0}

    for table in ALLOWED_TABLES:
        # Fetch rows that need backfill, then bulk-update in one transaction.
        conn = sqlite3.connect(str(db_path()), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cur = conn.execute(
                f'SELECT id, batch_id FROM "{table}" '
                f'WHERE "Product Name" IS NULL OR TRIM("Product Name") = \'\''
            )
            empties = cur.fetchall()
            cur.close()

            if not empties:
                _log.info("mock backfill: %s has no empty product names", table)
                continue

            conn.execute("BEGIN")
            updated = 0
            for row_id, batch_id in empties:
                mock_name = _pick_mock_name(str(batch_id or ""), int(row_id))
                conn.execute(
                    f'UPDATE "{table}" SET "Product Name" = ?, is_mock_named = 1 WHERE id = ?',
                    (mock_name, row_id),
                )
                updated += 1
            conn.execute("COMMIT")
            totals[f"{table}_filled"] = updated
            _log.info("mock backfill: %s filled=%d", table, updated)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            _log.exception("mock backfill: %s failed", table)
        finally:
            conn.close()

    return totals


async def mock_backfill_stats() -> dict[str, Any]:
    """Report how many rows are real-named vs mock-named across both tables."""
    out: dict[str, Any] = {}
    for table in ALLOWED_TABLES:
        row = await fetch_one(
            f'SELECT '
            f'  COUNT(*) AS total, '
            f'  COALESCE(SUM(CASE WHEN is_mock_named = 1 THEN 1 ELSE 0 END), 0) AS mock, '
            f'  COALESCE(SUM(CASE WHEN is_mock_named = 0 THEN 1 ELSE 0 END), 0) AS real_named, '
            f'  COUNT(CASE WHEN "Product Name" IS NULL OR TRIM("Product Name") = \'\' THEN 1 END) AS still_empty '
            f'FROM "{table}"'
        )
        out[table] = {
            "total":       int((row or {}).get("total") or 0),
            "mock_named":  int((row or {}).get("mock") or 0),
            "real_named":  int((row or {}).get("real_named") or 0),
            "still_empty": int((row or {}).get("still_empty") or 0),
        }
    out["pool_size"] = len(MOCK_FOOTWEAR_POOL)
    return out
