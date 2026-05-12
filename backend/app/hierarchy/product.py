"""Product hierarchy — classifier + persistent registry built from real data.

Hierarchy:
    Business         (e.g. "Footwear")
      └ Category     (Shoes | Slippers | Sandals | Other)
          └ Subcategory   (Sports Shoes | Casual Shoes | …)
              └ Product Type  (Running Shoes | Walking Shoes | …)
                  └ Brand    (Nike | Bata | …)
                      └ Product / SKU  (links via product_master.product_name)

The classifier operates ONLY on canonical Product Name strings that appear
in the sales/purchase tables. It never invents products.

Classification rules are keyword-based and conservative — any product the
classifier can't slot lands under (Footwear → Other) with the original name
as both the subcategory and product type, so nothing is lost.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from app.infrastructure import db_path, fetch_all


_log = logging.getLogger("agentic_ai.hierarchy.product")


PRODUCT_LEVELS = ("business", "category", "subcategory", "product_type", "brand")

# Default top-level business — the only one we ship. Users can rename.
DEFAULT_BUSINESS = "Footwear"


# ===========================================================================
# Classifier — keyword rules for the footwear domain.
# Order matters: first matching rule wins. Categories are checked first,
# then within a category we narrow to subcategory + product_type.
# ===========================================================================

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_TOKEN_RE.findall((s or "").lower()))


# Tokens that decisively pick a CATEGORY. Checked first, before any
# modifier-based subcategory inference. The order here matters: slippers
# and sandals beat shoes because "Casual Slippers" must not become "Shoes".
_CATEGORY_MARKERS: list[tuple[str, set[str]]] = [
    ("Slippers", {"slipper", "slippers", "chappal", "chappals", "flip", "flops", "thong"}),
    ("Sandals",  {"sandal", "sandals", "gladiator", "gladiators"}),
    ("Shoes",    {"shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "loafer", "loafers", "moccasin", "moccasins"}),
]

# Subcategory rules WITHIN a category. First matching rule wins per category.
# (subcategory, product_type, keyword_set)
_SUBCAT_RULES: dict[str, list[tuple[str, str, set[str]]]] = {
    "Shoes": [
        ("Sports Shoes",   "Running Shoes",  {"running", "runner", "jog", "jogger"}),
        ("Sports Shoes",   "Training Shoes", {"training", "trainer", "gym", "fitness", "workout"}),
        ("Sports Shoes",   "Sports Shoes",   {"sport", "sports", "athletic"}),
        ("Formal Shoes",   "Formal Shoes",   {"formal", "oxford", "derby", "brogue", "leather"}),
        ("School Shoes",   "School Shoes",   {"school", "kids", "child", "children"}),
        ("Casual Shoes",   "Sneakers",       {"sneaker", "sneakers", "canvas"}),
        ("Casual Shoes",   "Loafers",        {"loafer", "loafers", "moccasin", "moccasins"}),
        ("Casual Shoes",   "Casual Shoes",   {"casual"}),
        ("Boots",          "Boots",          {"boot", "boots", "ankle"}),
        # Fallback if only the generic "shoe" token is present.
        ("Shoes",          "Shoes",          {"shoe", "shoes"}),
    ],
    "Slippers": [
        ("Flip Flops",       "Flip Flops",       {"flip", "flops", "thong"}),
        ("Casual Slippers",  "Casual Slippers",  {"casual"}),
        ("House Slippers",   "House Slippers",   {"house", "indoor"}),
        ("Slippers",         "Slippers",         {"slipper", "slippers", "chappal", "chappals"}),
    ],
    "Sandals": [
        ("Men's Sandals",   "Men's Sandals",   {"men", "mens", "gents"}),
        ("Women's Sandals", "Women's Sandals", {"women", "womens", "ladies", "girls"}),
        ("Kids' Sandals",   "Kids' Sandals",   {"kids", "child", "children", "boys"}),
        ("Sandals",         "Sandals",         {"sandal", "sandals", "gladiator", "gladiators"}),
    ],
}


# Optional brand vocabulary — populated only when the brand token actually
# appears in a product name. We won't tag a brand we don't see in the data.
_KNOWN_BRANDS = {
    "nike", "adidas", "puma", "reebok", "asics", "skechers", "fila",
    "bata", "liberty", "relaxo", "paragon", "vkc", "campus",
    "woodland", "hush puppies", "clarks", "lee cooper", "redtape", "red tape",
    "sparx", "lakhani", "action", "crocs",
}


def classify_product(product_name: str) -> dict[str, str]:
    """Slot a product name into the hierarchy. Pure function — no DB I/O.

    Two-pass:
      1. CATEGORY from decisive markers (slipper / sandal / shoe).
      2. SUBCATEGORY + PRODUCT_TYPE from rules within that category.

    Falls back to ('Other', 'Other', product_name) if no category marker
    matches — guarantees every uploaded product lands somewhere.
    """
    name = (product_name or "").strip()
    if not name:
        return {"business": DEFAULT_BUSINESS, "category": "Other",
                "subcategory": "Unknown", "product_type": "Unknown", "brand": ""}

    tokens = _tokens(name)
    name_lower = name.lower()

    # --- 1. Pick category ---
    category: str | None = None
    for cat, markers in _CATEGORY_MARKERS:
        if tokens & markers:
            category = cat
            break
    if category is None:
        category = "Other"

    # --- 2. Pick subcategory + product_type inside that category ---
    subcategory = product_type = None
    rules = _SUBCAT_RULES.get(category, [])
    for sub, pt, kw in rules:
        if tokens & kw:
            subcategory, product_type = sub, pt
            break
    if subcategory is None:
        # Use a sensible default per category if no modifier matched.
        if category == "Other":
            subcategory = product_type = name[:60] or "Other"
        else:
            subcategory = product_type = category  # generic bucket

    # --- 3. Brand: first known brand token found in the name ---
    brand = ""
    for b in _KNOWN_BRANDS:
        if b in name_lower:
            brand = b.title()
            break

    return {
        "business":    DEFAULT_BUSINESS,
        "category":    category,
        "subcategory": subcategory,
        "product_type": product_type,
        "brand":       brand,
    }


# ===========================================================================
# Hierarchy persistence — adjacency-list tree built lazily as products land.
# ===========================================================================

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "unknown"


async def _ensure_node(
    db,
    level: str,
    name: str,
    parent_id: str | None,
) -> str:
    """INSERT-or-find a hierarchy node. Returns its id."""
    slug = _slug(name)
    cur = await db.execute(
        "SELECT id FROM product_hierarchy WHERE parent_id IS ? AND slug = ?",
        (parent_id, slug),
    )
    row = await cur.fetchone()
    await cur.close()
    if row:
        return row[0]
    node_id = f"ph-{uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO product_hierarchy (id, level, parent_id, name, slug) "
        "VALUES (?, ?, ?, ?, ?)",
        (node_id, level, parent_id, name, slug),
    )
    return node_id


async def sync_product_master_from_data() -> dict[str, int]:
    """Rebuild product_master by scanning distinct Product Name values in
    sales + purchase tables. Idempotent — safe to call after every upload.

    Returns counts: {distinct_products, mapped, new_rows}.
    """
    # 1. Collect distinct product names from current uploaded data.
    distinct: set[str] = set()
    for table in ("sales", "purchase"):
        rows = await fetch_all(
            f'SELECT DISTINCT "Product Name" AS name FROM {table} '
            f'WHERE "Product Name" IS NOT NULL AND TRIM("Product Name") <> \'\''
        )
        for r in rows:
            distinct.add(str(r["name"]).strip())

    if not distinct:
        _log.info("product sync: no distinct product names in uploaded data")
        return {"distinct_products": 0, "mapped": 0, "new_rows": 0}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_rows = 0
    mapped = 0
    async with aiosqlite.connect(db_path()) as db:
        # Ensure the single root business node exists.
        biz_id = await _ensure_node(db, "business", DEFAULT_BUSINESS, None)
        for product_name in sorted(distinct):
            tags = classify_product(product_name)
            category_id    = await _ensure_node(db, "category",     tags["category"],    biz_id)
            subcategory_id = await _ensure_node(db, "subcategory",  tags["subcategory"], category_id)
            product_type_id = await _ensure_node(db, "product_type", tags["product_type"], subcategory_id)
            brand_id: str | None = None
            if tags["brand"]:
                brand_id = await _ensure_node(db, "brand", tags["brand"], product_type_id)

            # Upsert master row by canonical product_name
            cur = await db.execute(
                "SELECT id FROM product_master WHERE product_name = ?",
                (product_name,),
            )
            existing = await cur.fetchone()
            await cur.close()
            if existing:
                await db.execute(
                    "UPDATE product_master SET business_id=?, category_id=?, "
                    "subcategory_id=?, product_type_id=?, brand_id=?, updated_at=? "
                    "WHERE product_name=?",
                    (biz_id, category_id, subcategory_id, product_type_id,
                     brand_id, now, product_name),
                )
                mapped += 1
            else:
                pm_id = f"pm-{uuid4().hex[:12]}"
                await db.execute(
                    "INSERT INTO product_master "
                    "(id, product_name, business_id, category_id, subcategory_id, "
                    " product_type_id, brand_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pm_id, product_name, biz_id, category_id, subcategory_id,
                     product_type_id, brand_id, now, now),
                )
                new_rows += 1
                mapped += 1
        await db.commit()

    _log.info(
        "product sync: distinct=%d mapped=%d new=%d",
        len(distinct), mapped, new_rows,
    )
    return {"distinct_products": len(distinct), "mapped": mapped, "new_rows": new_rows}


async def list_product_hierarchy() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT id, level, parent_id, name, slug FROM product_hierarchy "
        "ORDER BY level, name"
    )


async def list_product_master() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT pm.id, pm.product_name, "
        "       biz.name  AS business, "
        "       cat.name  AS category, "
        "       sub.name  AS subcategory, "
        "       pt.name   AS product_type, "
        "       br.name   AS brand "
        "FROM product_master pm "
        "LEFT JOIN product_hierarchy biz ON biz.id = pm.business_id "
        "LEFT JOIN product_hierarchy cat ON cat.id = pm.category_id "
        "LEFT JOIN product_hierarchy sub ON sub.id = pm.subcategory_id "
        "LEFT JOIN product_hierarchy pt  ON pt.id  = pm.product_type_id "
        "LEFT JOIN product_hierarchy br  ON br.id  = pm.brand_id "
        "ORDER BY pm.product_name"
    )
