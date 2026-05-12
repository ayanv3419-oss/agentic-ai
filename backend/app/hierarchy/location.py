"""Location hierarchy — Business → City → Branch → Counter.

The current upload schema has no branch column, so on first boot we seed:
    Business: "My Business"
      └ City: "Main"
          └ Branch: "Main Shop"   (default, is_default=1)

The user can add more branches via the API; clarification logic only kicks
in when there are 2+ enabled branches. Until then queries assume the
default branch (single-branch deployment).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

import aiosqlite

from app.infrastructure import db_path, fetch_all, fetch_one


_log = logging.getLogger("agentic_ai.hierarchy.location")


LOCATION_LEVELS = ("business", "city", "branch", "counter")

DEFAULT_BUSINESS_NAME = "My Business"
DEFAULT_CITY_NAME     = "Main"
DEFAULT_BRANCH_NAME   = "Main Shop"


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "unknown"


async def _ensure_location_node(db, level: str, name: str, parent_id: str | None) -> str:
    slug = _slug(name)
    cur = await db.execute(
        "SELECT id FROM location_hierarchy WHERE parent_id IS ? AND slug = ?",
        (parent_id, slug),
    )
    row = await cur.fetchone()
    await cur.close()
    if row:
        return row[0]
    node_id = f"lh-{uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO location_hierarchy (id, level, parent_id, name, slug) "
        "VALUES (?, ?, ?, ?, ?)",
        (node_id, level, parent_id, name, slug),
    )
    return node_id


async def seed_default_business() -> dict[str, str]:
    """Idempotently create the default Business → City → Branch chain plus a
    single default branch_master row. Returns {business_id, city_id,
    branch_id}. Safe to call on every startup."""
    async with aiosqlite.connect(db_path()) as db:
        biz_id    = await _ensure_location_node(db, "business", DEFAULT_BUSINESS_NAME, None)
        city_id   = await _ensure_location_node(db, "city",     DEFAULT_CITY_NAME, biz_id)
        branch_id = await _ensure_location_node(db, "branch",   DEFAULT_BRANCH_NAME, city_id)

        # Ensure a default branch_master row exists pointing to that branch node.
        cur = await db.execute(
            "SELECT id FROM branch_master WHERE is_default = 1"
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            await db.execute(
                "INSERT OR IGNORE INTO branch_master "
                "(id, branch_name, city, address, is_default, location_id, enabled) "
                "VALUES (?, ?, ?, ?, 1, ?, 1)",
                (f"bm-{uuid4().hex[:12]}", DEFAULT_BRANCH_NAME,
                 DEFAULT_CITY_NAME, None, branch_id),
            )
        await db.commit()

    return {"business_id": biz_id, "city_id": city_id, "branch_id": branch_id}


async def create_branch(
    branch_name: str,
    city: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """Manual branch creation by the user. Auto-creates the city node under
    the default business if `city` is provided and new."""
    name = (branch_name or "").strip()
    if not name:
        raise ValueError("branch_name is required")

    # Check for duplicates (case-insensitive)
    existing = await fetch_one(
        "SELECT id FROM branch_master WHERE LOWER(branch_name) = LOWER(?)",
        (name,),
    )
    if existing:
        raise ValueError(f"branch already exists: {name!r}")

    async with aiosqlite.connect(db_path()) as db:
        biz_id  = await _ensure_location_node(db, "business", DEFAULT_BUSINESS_NAME, None)
        city_id = await _ensure_location_node(
            db, "city", (city or DEFAULT_CITY_NAME).strip(), biz_id,
        )
        branch_id = await _ensure_location_node(db, "branch", name, city_id)
        bm_id = f"bm-{uuid4().hex[:12]}"
        await db.execute(
            "INSERT INTO branch_master "
            "(id, branch_name, city, address, is_default, location_id, enabled) "
            "VALUES (?, ?, ?, ?, 0, ?, 1)",
            (bm_id, name, city, address, branch_id),
        )
        await db.commit()
    _log.info("branch created: name=%s city=%s", name, city)
    return {
        "id":          bm_id,
        "branch_name": name,
        "city":        city,
        "address":     address,
        "is_default":  False,
        "enabled":     True,
        "location_id": branch_id,
    }


async def list_branches(enabled_only: bool = True) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, branch_name, city, address, is_default, location_id, "
        "       enabled, created_at, updated_at "
        "FROM branch_master "
    )
    if enabled_only:
        sql += "WHERE enabled = 1 "
    sql += "ORDER BY is_default DESC, branch_name"
    return await fetch_all(sql)


async def get_default_branch() -> dict[str, Any] | None:
    return await fetch_one(
        "SELECT id, branch_name, city, address, is_default, location_id, enabled "
        "FROM branch_master WHERE is_default = 1 LIMIT 1"
    )


async def list_location_hierarchy() -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT id, level, parent_id, name, slug FROM location_hierarchy "
        "ORDER BY level, name"
    )
