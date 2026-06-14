"""Password-reset token helper tests (make / verify round-trip + tampering).

These exercise the pure-crypto helpers added to ``app.identity``:
``make_password_reset_token`` / ``verify_password_reset_token``. They touch no
database — the token is a self-contained HMAC-signed payload — but we still
clear ``DATABASE_URL`` and pin a deterministic ``auth_token_secret`` BEFORE
importing app modules so the suite never depends on the ambient ``.env`` (which
in this environment points at live Supabase) and signing is reproducible.

Run:
    cd backend && python -m pytest tests/test_password_reset.py -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Clear DATABASE_URL before importing app modules (settings is cached at import).
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402

from app.infrastructure import settings  # noqa: E402
import app.identity as identity  # noqa: E402
from app.identity import (  # noqa: E402
    make_password_reset_token,
    verify_password_reset_token,
)


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    """Deterministic signer so make/verify don't depend on the ambient .env."""
    monkeypatch.setattr(
        settings, "auth_token_secret", "unit-test-secret-not-a-real-key",
        raising=False,
    )


def test_reset_token_valid_round_trip():
    token = make_password_reset_token("user-123", "ada@example.com")
    assert isinstance(token, str) and token.count(".") == 1
    payload = verify_password_reset_token(token)
    assert payload == {"user_id": "user-123", "email": "ada@example.com"}


def test_tampered_token_returns_none():
    token = make_password_reset_token("user-123", "ada@example.com")
    payload_b64, sig_b64 = token.rsplit(".", 1)

    # Flip a character in the payload — signature no longer matches.
    flipped = ("X" if payload_b64[0] != "X" else "Y") + payload_b64[1:]
    assert verify_password_reset_token(f"{flipped}.{sig_b64}") is None

    # Tamper the signature instead.
    bad_sig = ("X" if sig_b64[0] != "X" else "Y") + sig_b64[1:]
    assert verify_password_reset_token(f"{payload_b64}.{bad_sig}") is None

    # Garbage / malformed inputs never raise, always None.
    assert verify_password_reset_token("") is None
    assert verify_password_reset_token("no-dot-here") is None
    assert verify_password_reset_token(None) is None  # type: ignore[arg-type]


def test_expired_token_returns_none(monkeypatch):
    # Mint a token whose exp is already in the past by pinning time.time().
    real_time = time.time
    monkeypatch.setattr(identity.time, "time", lambda: real_time() - 3600)
    token = make_password_reset_token("user-123", "ada@example.com")
    # Back to real time: the token's exp (issued an hour ago, 30-min TTL) is
    # now well in the past.
    monkeypatch.setattr(identity.time, "time", real_time)
    assert verify_password_reset_token(token) is None


def test_login_token_is_not_accepted_as_reset_token():
    # A bearer/login token (no "k":"pwreset" discriminator) must NOT verify as a
    # reset token even though it's signed with the same secret + wire shape.
    from app.identity import Principal, mint_token

    login_token = mint_token(
        Principal(user_id="u1", tenant_id="t1", email="ada@example.com")
    )
    assert verify_password_reset_token(login_token) is None
