"""Regression suite for the final production layer:

  - new tables exist (memberships, audit_logs, feedback)
  - AuditLogRepo.write inserts + list_recent reads tenant-scoped
  - FeedbackRepo.write inserts + rejects bad ratings
  - Tenant-safe vector retrieval (per-tenant records don't leak)
  - Engine status reports sqlite|postgres correctly per env
  - Postgres adapter returns PostgresUnavailableError without DATABASE_URL
  - Redis adapter status reports configured=False without REDIS_URL
  - Security headers present on every response
  - Rate limiting on /auth/login + /auth/register

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_production_layer.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_TMP_DB = Path(tempfile.gettempdir()) / "agentic_ai_prod_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")
os.environ["FINANCIAL_DB_PATH"] = str(_TMP_DB)
# Ensure REDIS_URL + DATABASE_URL are explicitly UNSET for these tests —
# the production layer is supposed to fall back gracefully when both are
# absent. We pop env AND override settings.* below because pydantic-settings
# loads `backend/.env` independently of os.environ — popping isn't enough.
os.environ.pop("REDIS_URL", None)
os.environ.pop("DATABASE_URL", None)

import aiosqlite  # noqa: E402

from app.database import (  # noqa: E402
    AuditLogRepo, FeedbackRepo, RepositoryError, engine_kind, engine_status,
)
from app.database.pg_adapter import PostgresUnavailableError, pg_pool, pg_status
from app.cache import redis_status
from app.cache.redis_client import RedisUnavailableError, get_redis
from app.infrastructure import init_database, settings

# Force the unconfigured-fallback path. .env may carry a DATABASE_URL /
# REDIS_URL — overriding the loaded settings here makes these tests
# deterministic. Production code reads `settings.database_url` etc., so
# this is sufficient to simulate "URL unset".
settings.database_url = ""
settings.redis_url = ""


# ---- Schema -------------------------------------------------------------

async def case_new_tables_exist() -> tuple[bool, str]:
    """memberships / audit_logs / feedback are present after init_database."""
    await init_database()
    async with aiosqlite.connect(str(_TMP_DB)) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('memberships','audit_logs','feedback')"
        )
        tables = {row[0] for row in await cur.fetchall()}
    missing = {"memberships", "audit_logs", "feedback"} - tables
    if missing:
        return False, f"missing: {sorted(missing)}"
    return True, f"tables present: {sorted(tables)}"


# ---- AuditLogRepo -------------------------------------------------------

async def case_audit_log_write_and_list() -> tuple[bool, str]:
    await init_database()
    log_id = await AuditLogRepo.write(
        action="auth.test",
        tenant_id="t-1",
        workspace_id="w-1",
        user_id="u-1",
        request_id="r-1",
        metadata={"foo": "bar"},
    )
    if not log_id:
        return False, "write returned empty id"
    rows = await AuditLogRepo.list_recent(tenant_id="t-1", action="auth.test", limit=5)
    if not rows:
        return False, "no rows returned"
    if rows[0]["id"] != log_id:
        return False, f"id mismatch: stored={log_id} got={rows[0]['id']}"
    if rows[0]["metadata"] != {"foo": "bar"}:
        return False, f"metadata not roundtripped: {rows[0]['metadata']}"
    return True, f"audit log roundtrip ok"


async def case_audit_log_tenant_isolation() -> tuple[bool, str]:
    await init_database()
    await AuditLogRepo.write(action="alpha.event", tenant_id="t-A", user_id="u-A")
    await AuditLogRepo.write(action="alpha.event", tenant_id="t-B", user_id="u-B")
    rows_a = await AuditLogRepo.list_recent(tenant_id="t-A", action="alpha.event")
    rows_b = await AuditLogRepo.list_recent(tenant_id="t-B", action="alpha.event")
    if not rows_a or not rows_b:
        return False, f"missing rows: A={len(rows_a)} B={len(rows_b)}"
    a_users = {r["user_id"] for r in rows_a}
    b_users = {r["user_id"] for r in rows_b}
    if "u-B" in a_users or "u-A" in b_users:
        return False, f"cross-tenant leak: A_users={a_users} B_users={b_users}"
    return True, f"tenant isolation ok (A={a_users} B={b_users})"


async def case_audit_log_requires_tenant_for_read() -> tuple[bool, str]:
    """Defensive: list_recent without tenant_id MUST raise so call sites
    can't accidentally read across tenants."""
    try:
        await AuditLogRepo.list_recent(tenant_id="", limit=1)
    except RepositoryError:
        return True, "RepositoryError raised on missing tenant"
    except Exception as e:
        return False, f"wrong exception: {type(e).__name__}"
    return False, "no exception raised"


# ---- FeedbackRepo -------------------------------------------------------

async def case_feedback_write_and_validate() -> tuple[bool, str]:
    await init_database()
    fid = await FeedbackRepo.write(
        tenant_id="t-1", user_id="u-1", rating=1,
        conversation_id="c-1", turn_id="turn-1", comment="great",
    )
    if not fid:
        return False, "feedback write returned empty id"
    # Bad rating must raise.
    try:
        await FeedbackRepo.write(tenant_id="t-1", user_id="u-1", rating=99)
    except RepositoryError:
        pass
    else:
        return False, "rating=99 was accepted"
    # Missing tenant must raise.
    try:
        await FeedbackRepo.write(tenant_id="", user_id="u-1", rating=1)
    except RepositoryError:
        pass
    else:
        return False, "missing tenant_id was accepted"
    return True, "feedback write + validation ok"


# ---- Tenant-safe vector ------------------------------------------------

def case_vector_tenant_isolation() -> tuple[bool, str]:
    """Records stamped with tenant=A must not surface for tenant=B."""
    from app.vector import (
        InMemoryVectorStore, VectorRecord, set_vector_store, semantic_search,
    )
    store = InMemoryVectorStore()
    set_vector_store(store)
    # Three records: one global (no tenant), one per tenant.
    store.upsert([
        VectorRecord(id="GlobalSneaker",  text="Sneakers", kind="entity",
                     metadata={}),
        VectorRecord(id="A-Sneaker",      text="Sneakers", kind="entity",
                     metadata={"tenant_id": "tenant-A"}),
        VectorRecord(id="B-Sneaker",      text="Sneakers", kind="entity",
                     metadata={"tenant_id": "tenant-B"}),
    ])
    a_hits = semantic_search("snkrs", kind="entity",
                             tenant_id="tenant-A", limit=10, min_score=0.05)
    b_hits = semantic_search("snkrs", kind="entity",
                             tenant_id="tenant-B", limit=10, min_score=0.05)
    a_ids = {h.canonical for h in a_hits}
    b_ids = {h.canonical for h in b_hits}
    if "B-Sneaker" in a_ids:
        return False, f"tenant-B record leaked into tenant-A: {a_ids}"
    if "A-Sneaker" in b_ids:
        return False, f"tenant-A record leaked into tenant-B: {b_ids}"
    # Global record must appear for both.
    if "GlobalSneaker" not in a_ids or "GlobalSneaker" not in b_ids:
        return False, f"global record missing: A={a_ids} B={b_ids}"
    return True, f"tenant vector filter ok (A={sorted(a_ids)} B={sorted(b_ids)})"


# ---- Engine + adapter status ------------------------------------------

def case_engine_kind_sqlite_when_no_url() -> tuple[bool, str]:
    if engine_kind() != "sqlite":
        return False, f"engine_kind={engine_kind()} (expected 'sqlite')"
    s = engine_status()
    if s.get("kind") != "sqlite" or not s.get("path"):
        return False, f"engine_status={s}"
    return True, f"engine_kind=sqlite path={s['path']}"


def case_pg_status_unavailable_without_url() -> tuple[bool, str]:
    s = pg_status()
    if s["configured"] is True:
        return False, f"pg reports configured=True without DATABASE_URL: {s}"
    if s["pool_open"] is True:
        return False, f"pool unexpectedly open"
    return True, f"pg fallback ok: {s}"


async def case_pg_pool_raises_without_url() -> tuple[bool, str]:
    try:
        await pg_pool()
    except PostgresUnavailableError:
        return True, "PostgresUnavailableError raised cleanly"
    except Exception as e:
        return False, f"wrong exception: {type(e).__name__}: {e}"
    return False, "pg_pool() did not raise"


def case_redis_status_unconfigured() -> tuple[bool, str]:
    s = redis_status()
    if s["configured"] is True:
        return False, f"redis reports configured=True: {s}"
    if s["client_open"] is True:
        return False, f"client unexpectedly open"
    return True, f"redis fallback ok: {s}"


async def case_redis_get_raises_without_url() -> tuple[bool, str]:
    try:
        await get_redis()
    except RedisUnavailableError:
        return True, "RedisUnavailableError raised cleanly"
    except Exception as e:
        return False, f"wrong exception: {type(e).__name__}: {e}"
    return False, "get_redis() did not raise"


# ---- HTTP layer (security headers + rate limits) -----------------------

async def case_security_headers_on_every_response() -> tuple[bool, str]:
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        r = c.get("/health")
    expected = {
        "X-Content-Type-Options":  "nosniff",
        "X-Frame-Options":         "DENY",
        "Referrer-Policy":         "strict-origin-when-cross-origin",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none';",
    }
    missing = {k: v for k, v in expected.items() if r.headers.get(k) != v}
    if missing:
        return False, f"missing/wrong headers: {missing}"
    if not r.headers.get("X-Request-ID"):
        return False, "X-Request-ID not echoed"
    return True, "all security headers + X-Request-ID present"


async def case_register_rate_limit() -> tuple[bool, str]:
    """11th register hit from same IP within 60s should 429."""
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        # Burn through the limit (10/min) — every call uses a fresh email
        # so we hit the rate limit, not the duplicate-email guard.
        statuses = []
        for i in range(12):
            r = c.post("/auth/register", json={
                "email":    f"rl-{i}-{os.urandom(2).hex()}@example.com",
                "password": "verysecret123",
                "workspace_name": "RL Test",
            })
            statuses.append(r.status_code)
    # First 10 should succeed (200) or duplicate (409); 11th+ should be 429.
    early = statuses[:10]
    late = statuses[10:]
    if any(s == 429 for s in early):
        return False, f"rate-limited too early: {statuses}"
    if not any(s == 429 for s in late):
        return False, f"rate limit never tripped: {statuses}"
    return True, f"register rate limit tripped: pattern={statuses}"


async def case_login_rate_limit() -> tuple[bool, str]:
    """Wrong-password floods should 429 after 20 hits."""
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        statuses = []
        for _ in range(25):
            r = c.post("/auth/login", json={
                "username": "ratelimit-target@example.com",
                "password": "wrongwrongwrong",
            })
            statuses.append(r.status_code)
    if not any(s == 429 for s in statuses[20:]):
        return False, f"login rate limit never tripped: {statuses}"
    return True, f"login rate limit tripped after 20"


# ---- Runner ------------------------------------------------------------

SYNC_CASES = [
    ("vector tenant isolation",                         case_vector_tenant_isolation),
    ("engine_kind=sqlite without DATABASE_URL",         case_engine_kind_sqlite_when_no_url),
    ("postgres status unavailable without URL",         case_pg_status_unavailable_without_url),
    ("redis status unconfigured",                       case_redis_status_unconfigured),
]

ASYNC_CASES = [
    ("new tables (memberships, audit_logs, feedback)",  case_new_tables_exist),
    ("AuditLogRepo write + list",                       case_audit_log_write_and_list),
    ("AuditLogRepo tenant isolation",                   case_audit_log_tenant_isolation),
    ("AuditLogRepo refuses cross-tenant read",          case_audit_log_requires_tenant_for_read),
    ("FeedbackRepo write + validation",                 case_feedback_write_and_validate),
    ("pg_pool raises PostgresUnavailableError",         case_pg_pool_raises_without_url),
    ("get_redis raises RedisUnavailableError",          case_redis_get_raises_without_url),
    ("security headers on every response",              case_security_headers_on_every_response),
    ("register rate limit (10/min by IP)",              case_register_rate_limit),
    ("login rate limit (20/min by IP)",                 case_login_rate_limit),
]


async def main_async() -> int:
    print("=== Production layer (Postgres + Redis adapters / audit / RBAC) ===")
    passed = failed = 0
    for label, fn in SYNC_CASES:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:55} :: {detail}")
        passed += int(ok); failed += int(not ok)

    for label, fn in ASYNC_CASES:
        try:
            ok, detail = await fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:55} :: {detail}")
        passed += int(ok); failed += int(not ok)

    total = passed + failed
    print(f"\nTOTAL: {passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> int:
    try:
        return asyncio.run(main_async())
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(_TMP_DB) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    sys.exit(main())
