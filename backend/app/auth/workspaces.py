"""Workspace CRUD.

A workspace is the data-isolation boundary: sales rows, uploads,
conversations, and analytics_cache all carry `workspace_id` (and the
parent `tenant_id`). Today every user owns exactly one workspace, but
the schema supports many-to-many in future via a join table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from app.infrastructure import fetch_one, get_connection


_log = logging.getLogger("agentic_ai.auth.workspaces")


@dataclass(frozen=True)
class WorkspaceRow:
    id: str
    tenant_id: str
    name: str
    created_at: str


async def create_workspace(name: str, tenant_id: str | None = None) -> WorkspaceRow:
    """Insert a new workspace. If `tenant_id` is None we generate a fresh
    one — the standard self-service signup flow creates a (tenant, workspace,
    user) trio at once."""
    wid = str(uuid4())
    tid = tenant_id or str(uuid4())
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO workspaces (id, tenant_id, name) VALUES (?, ?, ?)",
            (wid, tid, name),
        )
        await db.commit()
    _log.info("workspace created: id=%s tenant=%s name=%r", wid, tid, name)
    row = await fetch_one(
        "SELECT id, tenant_id, name, created_at FROM workspaces WHERE id = ?",
        (wid,),
    )
    if row is None:
        raise RuntimeError("workspace create succeeded but row not found")
    return WorkspaceRow(
        id=row["id"], tenant_id=row["tenant_id"],
        name=row["name"], created_at=row["created_at"],
    )


async def get_workspace_by_id(workspace_id: str) -> WorkspaceRow | None:
    if not workspace_id:
        return None
    row = await fetch_one(
        "SELECT id, tenant_id, name, created_at FROM workspaces WHERE id = ?",
        (workspace_id,),
    )
    if not row:
        return None
    return WorkspaceRow(
        id=row["id"], tenant_id=row["tenant_id"],
        name=row["name"], created_at=row["created_at"],
    )
