"""Product Hierarchy v2 — 6-level synthetic enterprise hierarchy.

Levels:    Need → Family → Class → Line → Type → Item (SKU)

For the footwear/fashion domain (current data), the spec is:

    Need:    Fashion   (single root for this domain)
    Family:  Footwear  (single family for this domain)
    Class:   Men | Women | Children
             ─────────────────────────
    Line under Men (8):       Casual Shoes, Formal Shoes, Sports Shoes,
                              Sneakers, Sandals, Slippers, Loafers, Boots
    Line under Women (7):     Heels, Flats, Sandals, Casual Shoes,
                              Sports Shoes, Slippers, Ethnic Footwear
    Line under Children (7):  School Shoes, Sports Shoes, Casual Shoes,
                              Sandals, Slippers, Party Wear,
                              Cartoon/Character Footwear

    Type:    a salable sub-style detected per Line (e.g. "High Top
             Sneakers", "Block Heels", "Black Velcro School Shoes").
    Item:    one row in product_sku_master = one canonical Product Name,
             stamped with a stable SKU code like SKU-MSN-001 (M = Men,
             SN = Sneakers).

This layer is STRICTLY ADDITIVE:
  • Existing product_hierarchy + product_master remain in place.
  • All v1 KPIs are unaffected.
  • v2 KPIs JOIN through product_sku_master + product_hierarchy_v2.

Sync is idempotent — re-running it preserves existing SKU codes for
known product names. A `force_rebuild=True` flag wipes both v2 tables
and rebuilds from scratch (used once when the structure spec changes).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import aiosqlite

from app.infrastructure import db_path, fetch_all, fetch_one


_log = logging.getLogger("agentic_ai.hierarchy.v2")


V2_LEVELS = ("need", "family", "class", "line", "type")

# Singular Need + Family for this footwear domain. The deeper levels are
# where the spec's structure lives.
ROOT_NEED   = "Fashion"
ROOT_FAMILY = "Footwear"

# A magic marker we look for at the class level — if "Men" doesn't exist
# under Footwear, we know the v2 tree is from an old structure and must be
# wiped + rebuilt on startup.
CLASS_MEN     = "Men"
CLASS_WOMEN   = "Women"
CLASS_CHILDREN = "Children"
ALL_CLASSES   = (CLASS_MEN, CLASS_WOMEN, CLASS_CHILDREN)

# Short codes used in SKU prefixes: class_code + line_code → 3-letter prefix
CLASS_CODE = {CLASS_MEN: "M", CLASS_WOMEN: "W", CLASS_CHILDREN: "C"}

# Ordered class-detection rules. First gender token in the product name
# wins. Default class is Men (most ambiguous footwear is men's by retail
# convention in this category).
_CLASS_RULES: list[tuple[str, set[str]]] = [
    (CLASS_WOMEN,    {"women", "womens", "woman", "ladies", "ladie",
                      "lady", "girls", "girl", "female", "femme",
                      "heel", "heels", "stiletto", "wedge", "pumps"}),
    (CLASS_CHILDREN, {"kids", "kid", "children", "child", "boys", "boy",
                      "school", "junior", "infant", "baby", "toddler",
                      "cartoon", "character", "disney", "marvel"}),
    (CLASS_MEN,      {"men", "mens", "man", "gents", "gent", "male"}),
]
DEFAULT_CLASS = CLASS_MEN

# (line_name, line_code, keyword_set) — checked in order within the chosen
# class. First match wins. If nothing matches, falls back to the class's
# default line.
_LINE_RULES: dict[str, list[tuple[str, str, set[str]]]] = {
    # Rule ORDER: concrete-shape lines first (Slippers, Sandals, Sneakers,
    # Loafers, Boots) so a product whose name carries a shape token never
    # gets misrouted by a generic modifier like "casual". Modifier-driven
    # lines come last as fallbacks.
    CLASS_MEN: [
        ("Slippers",      "SL", {"slipper", "slippers", "chappal",
                                  "chappals", "flip", "flops", "thong"}),
        ("Sandals",       "SD", {"sandal", "sandals", "gladiator", "gladiators"}),
        ("Sneakers",      "SN", {"sneaker", "sneakers", "canvas"}),
        ("Loafers",       "LF", {"loafer", "loafers", "moccasin", "moccasins"}),
        ("Boots",         "BT", {"boot", "boots", "ankle"}),
        ("Formal Shoes",  "FS", {"formal", "oxford", "derby", "brogue"}),
        ("Sports Shoes",  "SP", {"sport", "sports", "running", "runner",
                                  "training", "trainer", "athletic",
                                  "gym", "jog", "jogger"}),
        ("Casual Shoes",  "CS", {"casual"}),
    ],
    CLASS_WOMEN: [
        ("Ethnic Footwear",  "ET", {"ethnic", "kolhapuri", "mojari", "mojaris",
                                     "punjabi", "jutti", "juti", "jutis"}),
        ("Heels",            "HL", {"heel", "heels", "stiletto", "stilettos",
                                     "wedge", "wedges", "pump", "pumps"}),
        ("Flats",            "FL", {"flat", "flats", "ballerina", "ballet"}),
        ("Slippers",         "SL", {"slipper", "slippers", "flip", "flops",
                                     "chappal", "thong"}),
        ("Sandals",          "SD", {"sandal", "sandals", "gladiator"}),
        ("Sports Shoes",     "SP", {"sport", "sports", "running", "trainer",
                                     "gym", "athletic", "jog"}),
        ("Casual Shoes",     "CS", {"casual", "sneaker", "sneakers",
                                     "loafer", "moccasin"}),
    ],
    CLASS_CHILDREN: [
        ("Cartoon/Character Footwear",   "CC", {"cartoon", "character",
                                                 "disney", "marvel", "barbie",
                                                 "spiderman", "frozen", "elsa"}),
        ("School Shoes",                 "HS", {"school"}),
        ("Slippers",                     "SL", {"slipper", "slippers", "flip",
                                                 "flops", "chappal"}),
        ("Sandals",                      "SD", {"sandal", "sandals"}),
        ("Party Wear",                   "PW", {"party", "wedding", "festive",
                                                 "occasion"}),
        ("Sports Shoes",                 "SP", {"sport", "sports", "running",
                                                 "athletic"}),
        ("Casual Shoes",                 "CS", {"casual", "sneaker", "sneakers"}),
    ],
}

# Default line per class when nothing matches.
_DEFAULT_LINE: dict[str, tuple[str, str]] = {
    CLASS_MEN:      ("Casual Shoes", "CS"),
    CLASS_WOMEN:    ("Casual Shoes", "CS"),
    CLASS_CHILDREN: ("Casual Shoes", "CS"),
}

# (type_name, keyword_set) per line. First match wins. Falls back to the
# line name itself if nothing matches — guarantees every product lands on
# a real Type node rather than NULL.
_TYPE_RULES: dict[tuple[str, str], list[tuple[str, set[str]]]] = {
    # ---- Men's lines ----
    (CLASS_MEN, "Sneakers"): [
        ("High Top Sneakers", {"high", "top"}),
        ("Canvas Sneakers",   {"canvas"}),
        ("Slip-On Sneakers",  {"slip"}),
        ("Low Top Sneakers",  {"low"}),
    ],
    (CLASS_MEN, "Sports Shoes"): [
        ("Running Shoes",     {"running", "runner", "jog"}),
        ("Training Shoes",    {"training", "trainer", "gym", "fitness"}),
        ("Walking Shoes",     {"walking", "walker"}),
    ],
    (CLASS_MEN, "Formal Shoes"): [
        ("Oxford Formal",     {"oxford"}),
        ("Derby Formal",      {"derby"}),
        ("Brogue Formal",     {"brogue"}),
        ("Leather Formal",    {"leather"}),
    ],
    (CLASS_MEN, "Casual Shoes"): [
        ("Slip-On Casuals",   {"slip"}),
        ("Lace-Up Casuals",   {"lace"}),
    ],
    (CLASS_MEN, "Loafers"): [
        ("Penny Loafers",     {"penny"}),
        ("Tassel Loafers",    {"tassel"}),
        ("Moccasin Loafers",  {"moccasin", "moccasins"}),
    ],
    (CLASS_MEN, "Boots"): [
        ("Ankle Boots",       {"ankle"}),
        ("Chelsea Boots",     {"chelsea"}),
        ("Combat Boots",      {"combat"}),
    ],
    (CLASS_MEN, "Sandals"): [
        ("Floater Sandals",   {"floater"}),
        ("Gladiator Sandals", {"gladiator"}),
        ("Comfort Sandals",   {"comfort"}),
    ],
    (CLASS_MEN, "Slippers"): [
        ("Flip Flops",        {"flip", "flops", "thong"}),
        ("House Slippers",    {"house", "indoor"}),
        ("Chappal Slippers",  {"chappal", "chappals"}),
    ],

    # ---- Women's lines ----
    (CLASS_WOMEN, "Heels"): [
        ("Block Heels",       {"block"}),
        ("Stiletto Heels",    {"stiletto", "stilettos"}),
        ("Wedge Heels",       {"wedge", "wedges"}),
        ("Kitten Heels",      {"kitten"}),
        ("Pump Heels",        {"pump", "pumps"}),
    ],
    (CLASS_WOMEN, "Flats"): [
        ("Ballerina Flats",   {"ballerina", "ballet"}),
        ("Pointed Flats",     {"pointed"}),
        ("Slip-On Flats",     {"slip"}),
    ],
    (CLASS_WOMEN, "Sandals"): [
        ("Strappy Sandals",   {"strappy", "strap"}),
        ("Flat Sandals",      {"flat"}),
        ("Heeled Sandals",    {"heel", "heeled"}),
    ],
    (CLASS_WOMEN, "Casual Shoes"): [
        ("Sneakers (Casual)", {"sneaker", "sneakers"}),
        ("Loafers (Casual)",  {"loafer"}),
    ],
    (CLASS_WOMEN, "Sports Shoes"): [
        ("Running Shoes",     {"running", "runner", "jog"}),
        ("Training Shoes",    {"training", "trainer", "gym"}),
        ("Walking Shoes",     {"walking"}),
    ],
    (CLASS_WOMEN, "Slippers"): [
        ("Flip Flops",        {"flip", "flops"}),
        ("Home Slippers",     {"home", "house", "indoor"}),
    ],
    (CLASS_WOMEN, "Ethnic Footwear"): [
        ("Kolhapuri",         {"kolhapuri"}),
        ("Mojari",            {"mojari", "mojaris"}),
        ("Jutti",             {"jutti", "juti", "jutis"}),
    ],

    # ---- Children's lines ----
    (CLASS_CHILDREN, "School Shoes"): [
        ("Black Velcro School Shoes", {"velcro"}),
        ("Black Lace School Shoes",   {"lace"}),
        ("White School Shoes",        {"white"}),
    ],
    (CLASS_CHILDREN, "Sports Shoes"): [
        ("Kids' Running Shoes", {"running"}),
        ("Kids' Trainers",      {"trainer", "training"}),
    ],
    (CLASS_CHILDREN, "Casual Shoes"): [
        ("Kids' Sneakers",      {"sneaker", "sneakers"}),
        ("Kids' Slip-Ons",      {"slip"}),
    ],
    (CLASS_CHILDREN, "Sandals"): [
        ("Velcro Sandals",      {"velcro"}),
        ("Comfort Sandals",     {"comfort"}),
    ],
    (CLASS_CHILDREN, "Slippers"): [
        ("Flip Flops",          {"flip", "flops"}),
        ("Home Slippers",       {"home", "indoor"}),
    ],
    (CLASS_CHILDREN, "Party Wear"): [
        ("Party Mary Janes",    {"mary"}),
        ("Festive Sandals",     {"sandal"}),
    ],
    (CLASS_CHILDREN, "Cartoon/Character Footwear"): [
        ("Disney Character Shoes", {"disney"}),
        ("Marvel Character Shoes", {"marvel", "spiderman"}),
        ("Frozen Character Shoes", {"frozen", "elsa"}),
        ("Barbie Character Shoes", {"barbie"}),
    ],
}


# ===========================================================================
# Stable ID derivation
# ===========================================================================

def _stable_node_id(level: str, parent_id: str | None, name: str) -> str:
    h = hashlib.md5(f"{level}|{parent_id or ''}|{name.lower()}".encode("utf-8"))
    return f"phv2-{h.hexdigest()[:12]}"


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "unknown"


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_TOKEN_RE.findall((s or "").lower()))


# ===========================================================================
# Classifier
# ===========================================================================

def classify_to_v2(product_name: str) -> dict[str, str]:
    """Return dict with keys: need, family, class, line, type. Pure function."""
    tokens = _tokens(product_name)

    # 1. Pick class.
    klass = DEFAULT_CLASS
    for cls_name, markers in _CLASS_RULES:
        if tokens & markers:
            klass = cls_name
            break

    # 2. Pick line within that class.
    line_name, line_code = _DEFAULT_LINE[klass]
    for ln, code, kws in _LINE_RULES[klass]:
        if tokens & kws:
            line_name, line_code = ln, code
            break

    # 3. Pick type within (class, line). Fall back to the line name.
    type_name = line_name
    for tn, kws in _TYPE_RULES.get((klass, line_name), []):
        if tokens & kws:
            type_name = tn
            break

    return {
        "need":   ROOT_NEED,
        "family": ROOT_FAMILY,
        "class":  klass,
        "line":   line_name,
        "type":   type_name,
    }


def line_code_for(klass: str, line_name: str) -> str:
    """Lookup the 2-letter line code used in SKU prefixes."""
    for ln, code, _kws in _LINE_RULES.get(klass, []):
        if ln == line_name:
            return code
    return _DEFAULT_LINE.get(klass, ("Casual Shoes", "CS"))[1]


def sku_prefix(klass: str, line_name: str) -> str:
    """3-letter SKU prefix: class_code + line_code. e.g. Men+Sneakers=MSN."""
    return CLASS_CODE.get(klass, "X") + line_code_for(klass, line_name)


# ===========================================================================
# Sync
# ===========================================================================

async def _ensure_v2_node(
    db, level: str, name: str, parent_id: str | None, code: str | None = None,
) -> str:
    node_id = _stable_node_id(level, parent_id, name)
    slug = _slug(name)
    cur = await db.execute(
        "SELECT id FROM product_hierarchy_v2 WHERE id = ?", (node_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if row:
        return node_id
    await db.execute(
        "INSERT OR IGNORE INTO product_hierarchy_v2 "
        "(id, level, parent_id, name, slug, code) VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, level, parent_id, name, slug, code),
    )
    return node_id


async def _has_correct_class_nodes() -> bool:
    """True iff the v2 tree already contains the three spec classes
    (Men, Women, Children) at level='class'. Used to detect when an
    older v2 structure is on disk and needs rebuilding."""
    row = await fetch_one(
        "SELECT COUNT(*) AS n FROM product_hierarchy_v2 "
        "WHERE level = 'class' AND name IN ('Men', 'Women', 'Children')"
    )
    return int((row or {}).get("n") or 0) >= 3


async def _purge_v2_tables() -> None:
    """Wipe both v2 tables. Used on force-rebuild."""
    async with aiosqlite.connect(db_path()) as db:
        await db.execute("DELETE FROM product_sku_master")
        await db.execute("DELETE FROM product_hierarchy_v2")
        await db.commit()


async def sync_product_sku_master(force_rebuild: bool = False) -> dict[str, int]:
    """Rebuild product_hierarchy_v2 + product_sku_master from distinct
    Product Name values in sales + purchase.

    Idempotent — re-running preserves existing SKU codes for known names.
    `force_rebuild=True` wipes both v2 tables and re-derives every node
    + SKU code from scratch (used once on structural schema changes).
    """
    # If forced, OR if the tree pre-dates this spec, wipe and rebuild.
    structure_outdated = not await _has_correct_class_nodes()
    if force_rebuild or structure_outdated:
        _log.info("v2 sync: force_rebuild=%s outdated=%s — wiping v2 tables",
                  force_rebuild, structure_outdated)
        await _purge_v2_tables()

    distinct: set[str] = set()
    for table in ("sales", "purchase"):
        rows = await fetch_all(
            f'SELECT DISTINCT "Product Name" AS name FROM {table} '
            f'WHERE "Product Name" IS NOT NULL AND TRIM("Product Name") <> \'\''
        )
        for r in rows:
            distinct.add(str(r["name"]).strip())

    if not distinct:
        _log.info("v2 sync: no distinct product names")
        return {"distinct_products": 0, "new_skus": 0, "updated_skus": 0}

    ordered = sorted(distinct)
    new_skus = 0
    updated_skus = 0

    async with aiosqlite.connect(db_path()) as db:
        # Preload existing SKU codes by name so stable codes survive re-sync.
        cur = await db.execute(
            "SELECT product_name, sku_code FROM product_sku_master"
        )
        existing_rows = await cur.fetchall()
        await cur.close()
        existing_by_name: dict[str, str] = {r[0]: r[1] for r in existing_rows}

        # Track sequence per 3-letter prefix to mint NEW SKU codes
        # without colliding with existing ones.
        prefix_counter: dict[str, int] = {}
        for code in existing_by_name.values():
            m = re.match(r"^SKU-([A-Z]{2,4})-(\d{3,})$", code or "")
            if m:
                p = m.group(1)
                n = int(m.group(2))
                if n > prefix_counter.get(p, 0):
                    prefix_counter[p] = n

        # Always ensure the canonical tree skeleton exists, even if no
        # products yet match certain classes — dashboards can drill into
        # empty branches and the AI can mention them by name.
        need_id   = await _ensure_v2_node(db, "need",   ROOT_NEED,   None,    "FSH")
        family_id = await _ensure_v2_node(db, "family", ROOT_FAMILY, need_id, "FW")
        for cls_name in ALL_CLASSES:
            cls_id = await _ensure_v2_node(
                db, "class", cls_name, family_id, CLASS_CODE.get(cls_name, "X"),
            )
            # Ensure every spec'd line node exists under each class so
            # the drilldown sees them even before products land there.
            for ln, code, _kws in _LINE_RULES.get(cls_name, []):
                await _ensure_v2_node(db, "line", ln, cls_id, code)

        for product_name in ordered:
            v2 = classify_to_v2(product_name)
            cls_id    = _stable_node_id("class",  family_id, v2["class"])
            line_id   = _stable_node_id("line",   cls_id,    v2["line"])
            type_id   = await _ensure_v2_node(db, "type", v2["type"], line_id)

            prefix = sku_prefix(v2["class"], v2["line"])
            if product_name in existing_by_name:
                sku_code = existing_by_name[product_name]
                await db.execute(
                    "UPDATE product_sku_master SET need_id=?, family_id=?, class_id=?, "
                    "line_id=?, type_id=?, updated_at=datetime('now') WHERE product_name=?",
                    (need_id, family_id, cls_id, line_id, type_id, product_name),
                )
                updated_skus += 1
            else:
                prefix_counter[prefix] = prefix_counter.get(prefix, 0) + 1
                sku_code = f"SKU-{prefix}-{prefix_counter[prefix]:03d}"
                pm_id = "sku-" + hashlib.md5(product_name.encode("utf-8")).hexdigest()[:12]
                await db.execute(
                    "INSERT INTO product_sku_master "
                    "(id, sku_code, product_name, need_id, family_id, class_id, "
                    " line_id, type_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (pm_id, sku_code, product_name, need_id, family_id, cls_id,
                     line_id, type_id),
                )
                new_skus += 1
        await db.commit()

    _log.info(
        "v2 sync: distinct=%d new_skus=%d updated_skus=%d",
        len(distinct), new_skus, updated_skus,
    )
    return {
        "distinct_products": len(distinct),
        "new_skus": new_skus,
        "updated_skus": updated_skus,
    }


# ===========================================================================
# Inspection / drilldown
# ===========================================================================

async def list_v2_tree() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT id, level, parent_id, name, slug, code "
        "FROM product_hierarchy_v2 ORDER BY level, name"
    )


async def list_sku_master() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT pm.id, pm.sku_code, pm.product_name, "
        "       n.name  AS need, "
        "       f.name  AS family, "
        "       c.name  AS class, "
        "       l.name  AS line, "
        "       t.name  AS type "
        "FROM product_sku_master pm "
        "LEFT JOIN product_hierarchy_v2 n ON n.id = pm.need_id "
        "LEFT JOIN product_hierarchy_v2 f ON f.id = pm.family_id "
        "LEFT JOIN product_hierarchy_v2 c ON c.id = pm.class_id "
        "LEFT JOIN product_hierarchy_v2 l ON l.id = pm.line_id "
        "LEFT JOIN product_hierarchy_v2 t ON t.id = pm.type_id "
        "ORDER BY pm.sku_code"
    )


async def drilldown(level: str, parent_id: str | None = None) -> list[dict[str, Any]]:
    if level not in V2_LEVELS:
        raise ValueError(f"unknown v2 level: {level!r}")
    where = "level = ?"
    params: list[Any] = [level]
    if parent_id is None:
        where += " AND parent_id IS NULL"
    else:
        where += " AND parent_id = ?"
        params.append(parent_id)
    return await fetch_all(
        f"SELECT id, level, parent_id, name, slug, code "
        f"FROM product_hierarchy_v2 WHERE {where} ORDER BY name",
        tuple(params),
    )
