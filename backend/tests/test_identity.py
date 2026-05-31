"""Identity / Auth foundation tests (multi-tenant slice 1).

LOCAL SQLITE ONLY. ``DATABASE_URL`` in this environment points at a LIVE
Supabase instance, so this module aggressively forces the SQLite engine and
installs tripwires that make any attempt to reach Postgres fail loudly rather
than silently hitting the real database:

  1. At import time (BEFORE app modules read settings) we point
     ``FINANCIAL_DB_PATH`` at a throwaway temp file and clear ``DATABASE_URL``
     from ``os.environ``.
  2. An autouse fixture additionally zeroes the cached
     ``settings.database_url`` (pydantic baked the .env value into the cached
     Settings singleton) and asserts ``app.db_engine.is_postgres()`` is False.
  3. We monkeypatch ``app.db_engine.pg_connection`` / ``get_pool`` to raise, so
     a regression that flipped the engine would error instead of opening a
     connection to Supabase.

The repo's async tests drive coroutines with ``asyncio.run`` (see
test_enrichment.py); we keep that idiom but expose plain ``def test_*``
functions so ``python -m pytest`` collects them without depending on any
``asyncio_mode`` config.

Run:
    cd backend && python -m pytest tests/test_identity.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# --- Force SQLite BEFORE importing app modules -----------------------------
# Each pytest run gets a fresh, isolated DB file next to the other tests'
# sandbox DBs. Clearing DATABASE_URL here means the os.environ source for
# db_engine.database_url() is empty; the fixture handles the .env-backed
# Settings fallback.
_SANDBOX_DB = _BACKEND_DIR / "_identity_test.db"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ.pop("DATABASE_URL", None)

from app import db_engine  # noqa: E402
from app.infrastructure import init_database, settings  # noqa: E402
import app.identity as identity  # noqa: E402
from app.identity import (  # noqa: E402
    EmailExists,
    Principal,
    authenticate,
    mint_token,
    signup,
    verify_token,
)


def _purge_db_files() -> None:
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"),
              Path(str(_SANDBOX_DB) + "-wal"), Path(str(_SANDBOX_DB) + "-shm")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """Hard-force the SQLite engine for every test and assert it stuck.

    Without this, ``db_engine.database_url()`` falls back to the cached
    pydantic Settings (which loaded DATABASE_URL from backend/.env) and the
    suite would talk to live Supabase.
    """
    # 1) Both sources db_engine.database_url() consults must read empty.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "database_url", "", raising=False)
    monkeypatch.setattr(settings, "financial_db_path", str(_SANDBOX_DB),
                        raising=False)
    # Deterministic, non-default token secret/ttl so mint/verify don't depend
    # on whatever the ambient .env has.
    monkeypatch.setattr(settings, "auth_token_secret",
                        "unit-test-secret-not-a-real-key", raising=False)
    monkeypatch.setattr(settings, "auth_token_ttl_hours", 168, raising=False)

    # 2) Tripwire: prove we are NOT on Postgres before doing any DB work.
    assert db_engine.is_postgres() is False, (
        "test must run on SQLite — is_postgres() is True; DATABASE_URL leaked"
    )

    # 3) Belt-and-suspenders: any accidental Postgres path errors loudly
    #    instead of dialing Supabase.
    def _no_postgres(*_a, **_k):
        raise AssertionError("Postgres path reached in a SQLite-only test")

    monkeypatch.setattr(db_engine, "pg_connection", _no_postgres)
    monkeypatch.setattr(db_engine, "get_pool", _no_postgres)

    _purge_db_files()
    asyncio.run(init_database())
    yield
    _purge_db_files()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_signup_creates_user_and_tenant_and_mints_token():
    async def _run():
        principal = await signup("Ada@Example.com", "hunter2")
        assert isinstance(principal, Principal)
        # email normalized (strip + lower)
        assert principal.email == "ada@example.com"
        assert principal.user_id and principal.tenant_id
        assert principal.user_id != principal.tenant_id

        # the rows really exist, and the tenant is owned by the user
        from app.infrastructure import fetch_one
        urow = await fetch_one(
            "SELECT email FROM users WHERE id = ?", (principal.user_id,))
        assert urow is not None and urow["email"] == "ada@example.com"
        trow = await fetch_one(
            "SELECT owner_user_id, name FROM tenants WHERE id = ?",
            (principal.tenant_id,))
        assert trow is not None
        assert trow["owner_user_id"] == principal.user_id
        # name defaults to the email local-part
        assert trow["name"] == "ada"

        # mint_token yields a verifiable, tenant-bearing token
        token = mint_token(principal)
        assert isinstance(token, str) and token.count(".") == 1
        return principal, token

    principal, token = asyncio.run(_run())
    # the token round-trips back to the exact same Principal
    assert verify_token(token) == principal


def test_duplicate_email_raises_email_exists():
    async def _run():
        await signup("dup@example.com", "password1")
        # same email, different case/whitespace -> still a duplicate
        with pytest.raises(EmailExists):
            await signup("  DUP@Example.com  ", "password2")

    asyncio.run(_run())


def test_signup_rejects_bad_input():
    async def _run():
        with pytest.raises(ValueError):
            await signup("", "longenough")        # empty email
        with pytest.raises(ValueError):
            await signup("   ", "longenough")      # whitespace-only email
        with pytest.raises(ValueError):
            await signup("a@b.com", "short")       # password < 6 chars
        with pytest.raises(ValueError):
            await signup("a@b.com", "")            # empty password

    asyncio.run(_run())


def test_authenticate_ok_and_wrong_password():
    async def _run():
        created = await signup("login@example.com", "correct horse")
        # correct credentials (and case-insensitive email) -> same principal
        ok = await authenticate("LOGIN@example.com", "correct horse")
        assert ok is not None
        assert ok.user_id == created.user_id
        assert ok.tenant_id == created.tenant_id
        assert ok.email == "login@example.com"

        # wrong password -> None
        assert await authenticate("login@example.com", "wrong") is None
        # unknown email -> None
        assert await authenticate("nobody@example.com", "correct horse") is None
        # empty inputs -> None (and never raise)
        assert await authenticate("", "") is None

    asyncio.run(_run())


def test_mint_then_verify_round_trips_to_same_principal():
    async def _run():
        return await signup("round@example.com", "trip-pass")

    principal = asyncio.run(_run())
    token = mint_token(principal)
    back = verify_token(token)
    assert back == principal
    assert back is not None
    assert back.user_id == principal.user_id
    assert back.tenant_id == principal.tenant_id
    assert back.email == principal.email


def test_expired_token_returns_none(monkeypatch):
    principal = Principal(user_id="u1", tenant_id="t1", email="exp@example.com")
    real_time = time.time
    # Mint a 1-hour-TTL token as if it were issued two days ago, then verify
    # against the real clock: exp is well in the past -> None.
    past = real_time() - 2 * 24 * 3600
    monkeypatch.setattr(settings, "auth_token_ttl_hours", 1, raising=False)
    monkeypatch.setattr(identity.time, "time", lambda: past)
    token = mint_token(principal)
    monkeypatch.setattr(identity.time, "time", real_time)
    assert verify_token(token) is None


def test_tampered_payload_or_signature_returns_none():
    principal = Principal(user_id="u2", tenant_id="t2", email="t@example.com")
    token = mint_token(principal)
    payload_b64, sig_b64 = token.rsplit(".", 1)

    # tampered signature
    bad_sig = sig_b64[:-2] + ("AA" if not sig_b64.endswith("AA") else "BB")
    assert verify_token(f"{payload_b64}.{bad_sig}") is None

    # tampered payload (re-encode a forged tenant) but keep the old signature
    import base64
    import json as _json
    pad = "=" * (-len(payload_b64) % 4)
    forged = _json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    forged["tid"] = "attacker-tenant"
    forged_json = _json.dumps(forged, separators=(",", ":"), sort_keys=True)
    forged_b64 = base64.urlsafe_b64encode(
        forged_json.encode()).decode().rstrip("=")
    assert verify_token(f"{forged_b64}.{sig_b64}") is None

    # different secret can't be verified with the live one
    from app.auth import _sign
    other_sig = _sign(payload_b64, "a-totally-different-secret")
    assert verify_token(f"{payload_b64}.{other_sig}") is None


def test_verify_token_never_raises_on_garbage():
    for junk in ["", "not-a-token", "....", "a.b.c", "onlyonepart",
                 "@@@.@@@", "x." * 50, None]:  # type: ignore[list-item]
        # must return None, not raise, for any malformed input
        assert verify_token(junk) is None  # type: ignore[arg-type]


def test_stored_hash_differs_from_plaintext_but_verifies():
    async def _run():
        password = "s3cret-passphrase"
        principal = await signup("hash@example.com", password)
        from app.infrastructure import fetch_one
        row = await fetch_one(
            "SELECT password_hash FROM users WHERE id = ?",
            (principal.user_id,))
        assert row is not None
        stored = row["password_hash"]
        # never store the plaintext
        assert stored != password
        assert password not in stored
        # looks like a bcrypt hash
        assert stored.startswith("$2")
        # and it still authenticates
        assert await authenticate("hash@example.com", password) is not None

    asyncio.run(_run())
