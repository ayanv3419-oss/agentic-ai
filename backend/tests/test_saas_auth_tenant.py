"""Regression suite for the multi-user SaaS layer:

  1. Password hashing (bcrypt round-trip + tamper resistance)
  2. JWT token round-trip + expiry + invalid token returns None
  3. /auth/register creates (tenant, workspace, user) trio + returns JWT
  4. /auth/login (JWT path with email)
  5. /auth/login (legacy admin path with username)
  6. /auth/me returns full principal for JWT tokens
  7. Tenant isolation: alice's upload rows never appear in bob's dashboard
  8. Background-worker registry: ingest_upload task is registered + callable

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_saas_auth_tenant.py
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

# Per-process temp DB so we never touch a developer's real financial DB.
_TMP_DB = Path(tempfile.gettempdir()) / "agentic_ai_saas_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")
os.environ["FINANCIAL_DB_PATH"] = str(_TMP_DB)

import aiosqlite  # noqa: E402

from app.auth import (  # noqa: E402
    decode_token, encode_token, hash_password, verify_password,
)
from app.infrastructure import (  # noqa: E402
    SCHEMA_COLUMNS, init_database, quoted, settings,
)


# -------- 1. Password hashing -------------------------------------------

def case_password_hash_roundtrip() -> tuple[bool, str]:
    h = hash_password("correct horse battery staple")
    if not h.startswith("$2"):
        return False, f"hash format unexpected: {h[:6]!r}"
    if not verify_password("correct horse battery staple", h):
        return False, "verify failed for correct password"
    if verify_password("wrong password", h):
        return False, "verify accepted wrong password"
    return True, "bcrypt hash + verify ok"


def case_password_verify_safe_on_garbage() -> tuple[bool, str]:
    # Malformed hash MUST return False, never raise.
    if verify_password("any", "not-a-bcrypt-hash"):
        return False, "verify accepted invalid hash"
    if verify_password("any", ""):
        return False, "verify accepted empty hash"
    if verify_password("", "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"):
        return False, "verify accepted empty password"
    return True, "verify safely rejects garbage"


# -------- 2. JWT round-trip ---------------------------------------------

def case_jwt_roundtrip() -> tuple[bool, str]:
    token, exp = encode_token(
        user_id="u-1", email="alice@example.com",
        tenant_id="t-1", workspace_id="w-1", role="owner",
        ttl_hours=1,
    )
    p = decode_token(token)
    if p is None:
        return False, "decode_token returned None on valid token"
    if (p.user_id, p.email, p.tenant_id, p.workspace_id, p.role) != \
       ("u-1", "alice@example.com", "t-1", "w-1", "owner"):
        return False, f"decoded principal mismatch: {p}"
    if p.expires_at != exp:
        return False, f"expiry mismatch: claim={p.expires_at} vs {exp}"
    return True, "JWT encode/decode roundtrip ok"


def case_jwt_tamper_returns_none() -> tuple[bool, str]:
    token, _ = encode_token(
        user_id="u-1", email="alice@example.com",
        tenant_id="t-1", workspace_id="w-1", role="owner",
    )
    # Flip the last char of the signature.
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    if decode_token(tampered) is not None:
        return False, "tampered token decoded successfully"
    if decode_token("not-a-jwt-at-all") is not None:
        return False, "garbage decoded successfully"
    if decode_token("") is not None:
        return False, "empty token decoded successfully"
    return True, "tampered + garbage tokens correctly rejected"


# -------- 3-6. End-to-end via FastAPI TestClient ------------------------

async def case_register_creates_principal() -> tuple[bool, str]:
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        r = c.post("/auth/register", json={
            "email": "alice@example.com",
            "password": "verysecret123",
            "workspace_name": "Alice Co",
        })
        if r.status_code != 200:
            return False, f"status={r.status_code} body={r.text[:200]}"
        body = r.json()
        if "token" not in body or "user" not in body:
            return False, f"missing token/user: {sorted(body.keys())}"
        u = body["user"]
        if u["email"] != "alice@example.com" or u["role"] != "owner":
            return False, f"unexpected user: {u}"
        if not u["tenant_id"] or not u["workspace_id"]:
            return False, f"tenant/workspace ids empty: {u}"
        # Duplicate email = 409.
        r2 = c.post("/auth/register", json={
            "email": "alice@example.com", "password": "secondpass123",
            "workspace_name": "Dup",
        })
        if r2.status_code != 409:
            return False, f"duplicate registration returned {r2.status_code}, expected 409"
    return True, "registration + duplicate guard ok"


async def case_login_jwt_path() -> tuple[bool, str]:
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        # Reuse alice from previous case (state persists across cases).
        r = c.post("/auth/login", json={
            "username": "alice@example.com",
            "password": "verysecret123",
        })
        if r.status_code != 200:
            return False, f"status={r.status_code} body={r.text[:200]}"
        body = r.json()
        if "token" not in body or body.get("user", {}).get("email") != "alice@example.com":
            return False, f"no user in login response: {body}"
        # Wrong password = 401.
        r2 = c.post("/auth/login", json={
            "username": "alice@example.com", "password": "WRONG",
        })
        if r2.status_code != 401:
            return False, f"wrong password returned {r2.status_code}, expected 401"
    return True, "JWT login + wrong-password guard ok"


async def case_login_legacy_admin_still_works() -> tuple[bool, str]:
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "admin"})
        if r.status_code != 200:
            return False, f"legacy admin login failed: {r.status_code}"
        body = r.json()
        if "token" not in body:
            return False, f"missing token: {body}"
        # Legacy path returns username (not email) and no `user` block.
        if body.get("username") != "admin":
            return False, f"username mismatch: {body}"
    return True, "legacy admin login still issues a token"


async def case_auth_me_carries_principal() -> tuple[bool, str]:
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        # Login as alice, then /auth/me.
        r = c.post("/auth/login", json={
            "username": "alice@example.com", "password": "verysecret123",
        })
        token = r.json()["token"]
        me = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        if not me.get("authenticated"):
            return False, "authenticated=False"
        if me.get("role") != "owner":
            return False, f"role={me.get('role')!r} (expected 'owner')"
        if not me.get("tenant_id") or not me.get("workspace_id"):
            return False, f"missing tenant/workspace: {me}"
        ws = me.get("workspace")
        if not ws or ws.get("name") != "Alice Co":
            return False, f"workspace block wrong: {ws}"
    return True, "/auth/me returns full principal for JWT users"


# -------- 7. Tenant isolation -------------------------------------------

async def _seed_alice_and_bob() -> tuple[str, str]:
    """Register alice + bob, return (alice_token, bob_token)."""
    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        # Try to register bob if not already present.
        r_bob = c.post("/auth/register", json={
            "email": "bob@example.com",
            "password": "anotherpass456",
            "workspace_name": "Bob Inc",
        })
        # 200 on first call, 409 on subsequent. Either is fine — we just
        # need a valid login afterwards.
        if r_bob.status_code not in (200, 409):
            raise RuntimeError(f"bob register failed: {r_bob.status_code} {r_bob.text}")

        a_login = c.post("/auth/login", json={
            "username": "alice@example.com", "password": "verysecret123",
        })
        b_login = c.post("/auth/login", json={
            "username": "bob@example.com", "password": "anotherpass456",
        })
        if a_login.status_code != 200 or b_login.status_code != 200:
            raise RuntimeError("alice/bob login failed during seed")
        return a_login.json()["token"], b_login.json()["token"]


async def case_tenant_isolation_dashboard() -> tuple[bool, str]:
    """Alice uploads rows tagged with her tenant_id; Bob's dashboard MUST
    NOT include any of those rows."""
    a_token, b_token = await _seed_alice_and_bob()
    # Decode each token so we know each tenant id without an extra call.
    alice = decode_token(a_token); bob = decode_token(b_token)
    if not alice or not bob:
        return False, "token decode failed during isolation test"
    if alice.tenant_id == bob.tenant_id:
        return False, "alice and bob share tenant_id — registration not isolating"

    # Seed alice's tenant directly.
    async with aiosqlite.connect(str(_TMP_DB)) as db:
        await db.execute(f"DELETE FROM {quoted('sales')}")
        cols = ['batch_id', 'source', 'file_name',
                'tenant_id', 'workspace_id', 'user_id',
                *SCHEMA_COLUMNS]
        ph = ",".join("?" for _ in cols)
        qcols = ",".join(quoted(c) for c in cols)
        sql = f"INSERT INTO {quoted('sales')} ({qcols}) VALUES ({ph})"

        # Required SCHEMA_COLUMNS are: Date, Total Amount. Optional ones can be None.
        for date, amt, party in [
            ("2025-01-15", 1000.0, "Acme"),
            ("2025-02-15", 2000.0, "Beta"),
            ("2025-03-15", 3000.0, "Acme"),
        ]:
            schema_vals = []
            for c in SCHEMA_COLUMNS:
                if c == "Date":
                    schema_vals.append(date)
                elif c == "Total Amount":
                    schema_vals.append(amt)
                elif c == "Party Name":
                    schema_vals.append(party)
                else:
                    schema_vals.append(None)
            await db.execute(sql, [
                'b1', 'upload', 't.csv',
                alice.tenant_id, alice.workspace_id, alice.user_id,
                *schema_vals,
            ])
        await db.commit()

    from fastapi.testclient import TestClient
    from app.core_system import app
    with TestClient(app) as c:
        a_dash = c.get("/dashboard", headers={"Authorization": f"Bearer {a_token}"}).json()
        b_dash = c.get("/dashboard", headers={"Authorization": f"Bearer {b_token}"}).json()

    a_total = a_dash.get("kpis", {}).get("total_sales", 0)
    b_total = b_dash.get("kpis", {}).get("total_sales", 0)

    if a_total != 6000.0:
        return False, f"alice total_sales={a_total} (expected 6000.0)"
    if b_total != 0.0:
        return False, f"bob total_sales={b_total} (expected 0 — leaked alice's data!)"

    a_pie = a_dash.get("monthly_sales_pie") or []
    b_pie = b_dash.get("monthly_sales_pie") or []
    if len(a_pie) != 3:
        return False, f"alice pie has {len(a_pie)} months (expected 3)"
    if b_pie:
        return False, f"bob pie not empty: {b_pie}"
    return True, f"alice sees 6000.0 across 3 months; bob sees 0 (isolated)"


# -------- 8. Worker registry ----------------------------------------

def case_workers_registry() -> tuple[bool, str]:
    """`ingest_upload` task is registered + the inline queue is wired."""
    from app.workers import get_job_queue
    from app.workers.queue import _TASKS
    if "ingest_upload" not in _TASKS:
        return False, f"ingest_upload not registered (have: {sorted(_TASKS.keys())})"
    if get_job_queue().backend_kind != "inline":
        return False, f"unexpected queue backend: {get_job_queue().backend_kind}"
    return True, "ingest_upload registered; inline backend active"


# -------- Runner --------------------------------------------------------

SYNC_CASES = [
    ("password hash + verify roundtrip",            case_password_hash_roundtrip),
    ("password verify safely rejects garbage",       case_password_verify_safe_on_garbage),
    ("JWT encode/decode roundtrip",                 case_jwt_roundtrip),
    ("JWT tampered + garbage rejected",             case_jwt_tamper_returns_none),
    ("workers registry: ingest_upload + inline",     case_workers_registry),
]

ASYNC_CASES = [
    ("/auth/register creates principal + 409 dup",  case_register_creates_principal),
    ("/auth/login JWT path + wrong-password",       case_login_jwt_path),
    ("/auth/login legacy admin still works",        case_login_legacy_admin_still_works),
    ("/auth/me returns principal for JWT users",    case_auth_me_carries_principal),
    ("dashboard tenant isolation (alice vs bob)",   case_tenant_isolation_dashboard),
]


async def main_async() -> int:
    await init_database()
    print("=== SaaS auth + tenant isolation ===")
    passed = failed = 0
    for label, fn in SYNC_CASES:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:54} :: {detail}")
        passed += int(ok); failed += int(not ok)

    for label, fn in ASYNC_CASES:
        try:
            ok, detail = await fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:54} :: {detail}")
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
