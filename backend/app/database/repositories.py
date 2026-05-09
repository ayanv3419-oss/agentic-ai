"""Repository abstractions — tenant-safe accessors.

Why repositories: as the schema grows (10+ tables), inline SQL across
every route handler becomes unauditable. A repo per table:
  - centralises tenant_id enforcement (impossible to forget)
  - gives a single place to swap SQLite ↔ Postgres SQL dialect
  - makes mocking trivial in tests (swap the impl in `set_*_repo`)

Today's repos all run against SQLite via `infrastructure.fetch_all` etc.
When DATABASE_URL is set + the migration has run, each repo's body
switches to `pg_pool()` queries with `$1, $2, ...` placeholders. The
public interfaces stay identical so call sites don't change.

The two repos shipped here today:
  - `AuditLogRepo`  — records every privileged action (auth, upload, etc.)
  - `FeedbackRepo`  — records 👍/👎 on chat answers

Future repos: UserRepo, WorkspaceRepo, MembershipRepo. They're not yet
needed because the auth module already has direct CRUD; we'll move
them in when SQL dialect divergence forces the abstraction.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.infrastructure import fetch_all, get_connection


_log = logging.getLogger("agentic_ai.database.repositories")


class RepositoryError(Exception):
    """Raised when a repository would emit an unsafe query — e.g. a SELECT
    without a tenant_id filter on a tenant-scoped table. Surfaces a clear
    message at the call site so the bug is easy to fix."""


# ---- AuditLogRepo --------------------------------------------------------

class AuditLogRepo:
    """Append-only audit log. Never UPDATE; rows live forever (until a
    retention job archives them — out of scope for this pass).

    Standard actions:
      - auth.register, auth.login, auth.login_failed, auth.logout
      - upload.success, upload.failed
      - cache.cleared
      - dataset.disconnect
    """

    @staticmethod
    async def write(
        *,
        action: str,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Insert one audit row. Returns the new row id. Never raises —
        an audit-log failure must not break the caller's primary action,
        so we log + swallow."""
        log_id = str(uuid4())
        meta_json = json.dumps(metadata, default=str, ensure_ascii=False) if metadata else None
        try:
            async with get_connection() as db:
                await db.execute(
                    "INSERT INTO audit_logs ("
                    "  id, tenant_id, workspace_id, user_id, request_id, "
                    "  action, resource_type, resource_id, metadata, ip, user_agent"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        log_id, tenant_id, workspace_id, user_id, request_id,
                        action, resource_type, resource_id, meta_json, ip, user_agent,
                    ),
                )
                await db.commit()
        except Exception:
            _log.exception("audit_log write failed: action=%s", action)
        return log_id

    @staticmethod
    async def list_recent(
        *,
        tenant_id: str,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Tenant-scoped read. `tenant_id` is REQUIRED — pass an explicit
        sentinel like 'legacy_admin' for the single-admin path. Refusing
        unfiltered reads at this layer prevents accidental cross-tenant
        leaks via repository bugs."""
        if not tenant_id:
            raise RepositoryError(
                "AuditLogRepo.list_recent requires tenant_id (no cross-tenant reads)"
            )
        sql = (
            "SELECT id, tenant_id, workspace_id, user_id, request_id, "
            "       action, resource_type, resource_id, metadata, "
            "       ip, user_agent, created_at "
            "FROM audit_logs "
            "WHERE tenant_id = ?"
        )
        params: list[Any] = [tenant_id]
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = await fetch_all(sql, tuple(params))
        out: list[dict[str, Any]] = []
        for r in rows:
            entry = dict(r)
            # Decode JSON metadata for caller convenience.
            meta = entry.get("metadata")
            if isinstance(meta, str):
                try:
                    entry["metadata"] = json.loads(meta)
                except Exception:
                    entry["metadata"] = None
            out.append(entry)
        return out


# ---- FeedbackRepo --------------------------------------------------------

class FeedbackRepo:
    """Per-turn 👍/👎 capture. Used by future quality-improvement loop.
    Today we only INSERT; aggregations (e.g. % thumbs-up by intent_type)
    will live in DuckDB analytical queries."""

    @staticmethod
    async def write(
        *,
        tenant_id: str,
        user_id: str,
        rating: int,
        workspace_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        comment: str | None = None,
    ) -> str:
        if not tenant_id or not user_id:
            raise RepositoryError("FeedbackRepo.write requires tenant_id + user_id")
        if rating not in (-1, 0, 1):
            raise RepositoryError(f"rating must be -1/0/1, got {rating!r}")
        fid = str(uuid4())
        async with get_connection() as db:
            await db.execute(
                "INSERT INTO feedback ("
                "  id, tenant_id, workspace_id, user_id, conversation_id, "
                "  turn_id, rating, comment"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, tenant_id, workspace_id, user_id, conversation_id,
                 turn_id, int(rating), comment),
            )
            await db.commit()
        return fid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
