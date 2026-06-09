"""Per-tenant Google Drive token store + signed-OAuth-state tests.

Covers the security-sensitive change that moved Drive OAuth tokens from a
single global JSON file to per-tenant rows in the ``drive_tokens`` table, and
the HMAC-signed ``state`` that carries the tenant_id through the (public) OAuth
callback.

LOCAL SQLITE ONLY — mirrors test_identity.py's hard-force-SQLite harness so the
suite never dials the live Supabase instance configured via ``DATABASE_URL``:

  1. At import time (before app modules read settings) point
     ``FINANCIAL_DB_PATH`` at a throwaway temp file and clear ``DATABASE_URL``.
  2. An autouse fixture zeroes the cached ``settings.database_url`` and asserts
     ``db_engine.is_postgres()`` is False, and monkeypatches the Postgres entry
     points to raise.

We avoid importing the real Google client libraries: the store only needs a
``.to_json()`` to save and a parseable JSON blob to read back, so a tiny stub
Credentials object exercises ``save_credentials`` and the raw-row read path
(``_read_credentials_json``) without any network or google-auth dependency.

Run:
    cd backend && python -m pytest tests/test_drive_tokens.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# --- Force SQLite BEFORE importing app modules -----------------------------
_SANDBOX_DB = _BACKEND_DIR / "_drive_tokens_test.db"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ.pop("DATABASE_URL", None)

from app import db_engine  # noqa: E402
from app import google_drive  # noqa: E402
from app.infrastructure import fetch_one, init_database, settings  # noqa: E402

_TEST_SECRET = "unit-test-secret-not-a-real-key"


def _purge_db_files() -> None:
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"),
              Path(str(_SANDBOX_DB) + "-wal"), Path(str(_SANDBOX_DB) + "-shm")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """Hard-force the SQLite engine for every test and assert it stuck."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "database_url", "", raising=False)
    monkeypatch.setattr(settings, "financial_db_path", str(_SANDBOX_DB),
                        raising=False)
    # Deterministic secret so the signed-state assertions don't depend on the
    # ambient .env (it's the same secret get_authorization_url/exchange_code use).
    monkeypatch.setattr(settings, "auth_token_secret", _TEST_SECRET, raising=False)

    assert db_engine.is_postgres() is False, (
        "test must run on SQLite — is_postgres() is True; DATABASE_URL leaked"
    )

    def _no_postgres(*_a, **_k):
        raise AssertionError("Postgres path reached in a SQLite-only test")

    monkeypatch.setattr(db_engine, "pg_connection", _no_postgres)
    monkeypatch.setattr(db_engine, "get_pool", _no_postgres)

    _purge_db_files()
    asyncio.run(init_database())
    yield
    _purge_db_files()


class _StubCreds:
    """Minimal stand-in for google.oauth2.credentials.Credentials.

    The token store only calls ``.to_json()`` on save; loading parses the JSON
    back. This lets the isolation test run without the real google-auth libs.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def to_json(self) -> str:
        return json.dumps({
            "token": self._token,
            "refresh_token": f"refresh-{self._token}",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "scopes": list(google_drive.DRIVE_SCOPES),
        })


# ---------------------------------------------------------------------------
# (a) Per-tenant isolation of the token store
# ---------------------------------------------------------------------------

def test_token_store_is_isolated_per_tenant():
    async def _run():
        # Save a distinct token for tenant A only.
        await google_drive.save_credentials("tenant-a", _StubCreds("token-A"))

        # Tenant A's blob round-trips back at the raw-row level (no google-auth
        # dependency) and is exactly what we stored.
        a_json = await google_drive._read_credentials_json("tenant-a")
        assert a_json is not None
        assert json.loads(a_json)["token"] == "token-A"

        # Tenant B never connected — its row is absent (no cross-tenant leak).
        assert await google_drive._read_credentials_json("tenant-b") is None
        assert await google_drive.is_connected("tenant-b") is False

        # The persisted row really exists in the public-schema table, keyed by
        # tenant_id (mirrors uploads/conversations first-party isolation).
        row = await fetch_one(
            "SELECT tenant_id, credentials_json FROM drive_tokens "
            "WHERE tenant_id = ?",
            ("tenant-a",),
        )
        assert row is not None
        assert row["tenant_id"] == "tenant-a"
        assert json.loads(row["credentials_json"])["token"] == "token-A"

    asyncio.run(_run())


def test_save_credentials_upserts_same_tenant():
    async def _run():
        await google_drive.save_credentials("tenant-x", _StubCreds("first"))
        await google_drive.save_credentials("tenant-x", _StubCreds("second"))

        # Upsert (ON CONFLICT) — one row, latest token wins.
        rows = await fetch_one(
            "SELECT COUNT(*) AS n FROM drive_tokens WHERE tenant_id = ?",
            ("tenant-x",),
        )
        assert rows is not None and int(rows["n"]) == 1
        latest = await google_drive._read_credentials_json("tenant-x")
        assert json.loads(latest)["token"] == "second"

    asyncio.run(_run())


def test_revoke_deletes_only_that_tenants_row():
    async def _run():
        await google_drive.save_credentials("keep", _StubCreds("k"))
        await google_drive.save_credentials("drop", _StubCreds("d"))

        await google_drive.revoke_credentials("drop")

        assert await google_drive._read_credentials_json("drop") is None
        # The other tenant's token is untouched.
        assert await google_drive._read_credentials_json("keep") is not None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (b) Signed OAuth state round-trip + tamper rejection
# ---------------------------------------------------------------------------

def test_signed_state_round_trips_to_tenant_id():
    for tenant_id in ("public", "ab12cd34ef", "tenant-with-dashes", "x" * 64):
        state = google_drive.sign_oauth_state(tenant_id)
        # Shape: <b64u(tenant)>.<sig>
        assert state.count(".") == 1
        assert google_drive.verify_oauth_state(state) == tenant_id


def test_tampered_or_invalid_state_is_rejected():
    state = google_drive.sign_oauth_state("victim-tenant")
    tid_b64, sig = state.rsplit(".", 1)

    # Tampered signature.
    bad_sig = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    with pytest.raises(ValueError):
        google_drive.verify_oauth_state(f"{tid_b64}.{bad_sig}")

    # Forged tenant payload re-signed by an attacker who lacks our secret:
    # signing "attacker-tenant" with a different secret must not verify under
    # the real secret.
    from app.auth import _b64u_encode, _sign
    forged_b64 = _b64u_encode(b"attacker-tenant")
    forged_sig = _sign(forged_b64, "a-totally-different-secret")
    with pytest.raises(ValueError):
        google_drive.verify_oauth_state(f"{forged_b64}.{forged_sig}")

    # Swapped payload but kept the victim's signature (signature won't match the
    # new payload).
    with pytest.raises(ValueError):
        google_drive.verify_oauth_state(f"{forged_b64}.{sig}")

    # Structurally malformed states.
    for junk in ("", "no-dot", ".", "onlyonepart", "a.b.c"):
        with pytest.raises(ValueError):
            google_drive.verify_oauth_state(junk)
    with pytest.raises(ValueError):
        google_drive.verify_oauth_state(None)  # type: ignore[arg-type]


def test_get_authorization_url_embeds_signed_state_when_libs_present():
    """If google-auth-oauthlib is installed, the URL build pins OUR signed
    state (so the callback can recover the tenant). Skips cleanly when the
    optional dependency is absent — the unit logic is covered above."""
    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except Exception:
        pytest.skip("google-auth-oauthlib not installed")

    # Needs client creds to build a Flow; stub them on settings.
    import app.infrastructure as infra
    prev_id = infra.settings.google_client_id
    prev_secret = infra.settings.google_client_secret
    prev_redirect = infra.settings.google_redirect_uri
    try:
        infra.settings.google_client_id = "test-client-id"
        infra.settings.google_client_secret = "test-client-secret"
        infra.settings.google_redirect_uri = "http://localhost:8000/auth/google/callback"
        url, state = google_drive.get_authorization_url("tenant-42")
        assert "accounts.google.com" in url
        # The state handed to Google verifies back to the tenant we asked for.
        assert google_drive.verify_oauth_state(state) == "tenant-42"
        # And it is embedded in the consent URL (urlencoded).
        from urllib.parse import quote
        assert f"state={quote(state, safe='')}" in url or f"state={state}" in url
    finally:
        infra.settings.google_client_id = prev_id
        infra.settings.google_client_secret = prev_secret
        infra.settings.google_redirect_uri = prev_redirect
