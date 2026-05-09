"""User CRUD against the `users` table.

Every user belongs to exactly one (tenant_id, workspace_id). Multi-
workspace per user is a future feature — today the registration flow
creates the first workspace alongside the user and they own it.

All queries here are tenant-scoped reads/writes; the callers (auth routes)
trust the values they pass in because they come from a verified JWT.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.infrastructure import fetch_one, get_connection


_log = logging.getLogger("agentic_ai.auth.users")


@dataclass(frozen=True)
class UserRow:
    id: str
    email: str
    tenant_id: str
    workspace_id: str
    role: str
    created_at: str
    last_login_at: Optional[str] = None
    # password_hash is intentionally NOT included — we never serialise it.


def _row_to_user(row: dict) -> UserRow:
    return UserRow(
        id=row["id"],
        email=row["email"],
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        role=row["role"],
        created_at=row.get("created_at") or "",
        last_login_at=row.get("last_login_at"),
    )


async def create_user(
    *,
    email: str,
    password_hash: str,
    tenant_id: str,
    workspace_id: str,
    role: str = "owner",
) -> UserRow:
    """Insert a new user. Caller is responsible for ensuring no row with
    the same email already exists (use `find_user_by_email` first)."""
    user_id = str(uuid4())
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, tenant_id, "
            "workspace_id, role) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email.lower().strip(), password_hash, tenant_id,
             workspace_id, role),
        )
        await db.commit()
    _log.info("user created: id=%s email=%s tenant=%s role=%s",
              user_id, email, tenant_id, role)
    row = await fetch_one(
        "SELECT id, email, tenant_id, workspace_id, role, created_at, "
        "last_login_at FROM users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        raise RuntimeError("user create succeeded but row not found")
    return _row_to_user(row)


async def find_user_by_email(email: str) -> tuple[UserRow, str] | None:
    """Email lookup for login. Returns (UserRow, password_hash) on success,
    None on miss. The hash is the ONLY way the password leaves the DB —
    callers compare with bcrypt and discard immediately."""
    if not email:
        return None
    row = await fetch_one(
        "SELECT id, email, password_hash, tenant_id, workspace_id, role, "
        "created_at, last_login_at FROM users WHERE email = ?",
        (email.lower().strip(),),
    )
    if not row:
        return None
    return _row_to_user(row), row["password_hash"]


async def get_user_by_id(user_id: str) -> UserRow | None:
    if not user_id:
        return None
    row = await fetch_one(
        "SELECT id, email, tenant_id, workspace_id, role, created_at, "
        "last_login_at FROM users WHERE id = ?",
        (user_id,),
    )
    return _row_to_user(row) if row else None


async def update_last_login(user_id: str) -> None:
    """Stamp the user's last_login_at — used for audit + 'show signed in
    devices' style features later."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with get_connection() as db:
        await db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (now, user_id),
        )
        await db.commit()
