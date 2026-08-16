"""Identity / Auth foundation — per-user accounts + a tenant per owner.

Multi-tenant slice 1. This module owns the ``users`` / ``tenants`` tables
(registered in ``app.infrastructure.init_database``) and turns an
email + password into a tenant-bearing :class:`Principal`:

    principal = await signup("ada@example.com", "hunter2")
    token     = mint_token(principal)
    ...
    principal = verify_token(token)        # round-trips back

Design notes (deliberately mirroring the rest of the backend):

* **Portable SQL.** Every statement uses ``?`` placeholders and runs through
  ``app.infrastructure.get_connection``, which transparently translates to
  ``$1, $2, ...`` on Postgres. The schema is all-TEXT so the same DDL works on
  both engines (see ``_USERS_DDL`` / ``_TENANTS_DDL`` in infrastructure.py).
* **Same id / timestamp idioms** as the ``conversations`` tables:
  ``uuid.uuid4().hex`` ids and ``datetime.now(timezone.utc).isoformat(
  timespec="seconds")`` timestamps.
* **No duplicated crypto.** The bearer-token machinery is the HMAC primitives
  already in :mod:`app.auth` (``_b64u_encode`` / ``_b64u_decode`` / ``_sign``)
  signed with ``settings.auth_token_secret``. We only widen the *payload* from
  the admin token's ``{u, iat, exp}`` to ``{u, uid, tid, iat, exp}`` so a
  verified token resolves a full Principal without a DB hit.
* **bcrypt** for password hashing (already in ``backend/requirements.txt``).
  bcrypt silently truncates input beyond 72 bytes, so we truncate explicitly
  to keep hashing/verification consistent.

``mint_token`` is the only function that can raise here (a misconfigured
secret would be a programmer error worth surfacing). ``authenticate`` and
``verify_token`` NEVER raise — callers degrade to a 401 / guest path.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from app.auth import _b64u_decode, _b64u_encode, _sign
from app.infrastructure import get_connection, settings

_log = logging.getLogger("agentic_ai.identity")

# bcrypt only considers the first 72 bytes of the password. Truncate up front
# so the hash we store and the value we later verify see the exact same bytes.
_BCRYPT_MAX_BYTES = 72

# Minimum password length enforced at signup. Not a secret — a UX/safety floor.
_MIN_PASSWORD_LEN = 6


# ---------------------------------------------------------------------------
# Access-status enforcement (Phase 2). A small in-process cache so the paywall
# check costs at most one DB read per user every few minutes — NOT one per
# request — and customers never have to re-login. Fails OPEN so a transient DB
# blip never locks paying customers out.
# ---------------------------------------------------------------------------
_ACCESS_CACHE: dict[str, tuple[str, float]] = {}
_ACCESS_TTL = 300.0  # seconds
TRIAL_DAYS = 7  # free-trial length granted to new signups


def invalidate_access_status(user_id: str) -> None:
    """Drop a user's cached access_status so the next request re-reads the DB.
    Called after the admin portal changes a status → the change takes effect on
    the very next request instead of waiting out the cache TTL."""
    if user_id:
        _ACCESS_CACHE.pop(user_id, None)


def _effective_status(raw: str | None, trial_ends_at: str | None) -> str:
    """Collapse the stored access_status (+ trial clock) into the EFFECTIVE gate
    state the middleware acts on: 'allowed' | 'expired' | 'denied' | 'pending'.

    A 'trial' account is 'allowed' until trial_ends_at, then 'expired' (→ the
    payment screen). Anything unparseable fails OPEN (stays 'allowed') so our
    own bug never locks a trial user out. Non-trial statuses pass through."""
    status = (raw or "allowed").strip().lower()
    if status != "trial":
        return status
    if not trial_ends_at:
        return "allowed"
    try:
        if datetime.fromisoformat(trial_ends_at) > datetime.now(timezone.utc):
            return "allowed"
    except Exception:
        return "allowed"  # unparseable date → don't punish a trial user
    return "expired"


async def get_trial_ends_at(user_id: str) -> str | None:
    """The user's trial end timestamp (ISO string), or None when they aren't on
    a trial. Powers the "N days left" badge. Never raises."""
    if not user_id:
        return None
    try:
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT trial_ends_at FROM public.users WHERE id = ?", (user_id,)
            )
            rows = await cur.fetchall()
            await cur.close()
        if rows:
            return dict(rows[0]).get("trial_ends_at") or None
    except Exception:
        _log.warning("trial_ends_at lookup failed for %s", user_id, exc_info=True)
    return None


async def get_access_status(user_id: str) -> str:
    """Return the EFFECTIVE access state ('allowed' | 'expired' | 'denied' |
    'pending'), cached for a few minutes. Unknown users and any lookup error
    resolve to 'allowed' (fail OPEN) — availability beats strictness."""
    if not user_id:
        return "allowed"
    now = time.time()
    hit = _ACCESS_CACHE.get(user_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    status = "allowed"
    try:
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT access_status, trial_ends_at FROM public.users WHERE id = ?",
                (user_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        if rows:
            r = dict(rows[0])
            status = _effective_status(r.get("access_status"), r.get("trial_ends_at"))
    except Exception:
        _log.warning("access_status lookup failed for %s", user_id, exc_info=True)
        status = "allowed"  # fail open
    _ACCESS_CACHE[user_id] = (status, now + _ACCESS_TTL)
    return status


@dataclass(frozen=True)
class Principal:
    """The authenticated actor for a request: a user bound to their tenant."""

    user_id: str
    tenant_id: str
    email: str


class EmailExists(Exception):
    """Raised by :func:`signup` when the (normalized) email is already taken."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_email(email: str) -> str:
    """Canonical form used for storage AND lookup: stripped + lowercased."""
    return (email or "").strip().lower()


def _now_iso() -> str:
    """ISO-8601 UTC, second precision — same idiom as the conversations tables."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A pre-computed bcrypt hash used to equalize response timing when an email
# is not found in `authenticate`. Without this, the early-return before any
# bcrypt call leaks which emails have accounts via timing side-channel.
_DUMMY_HASH: str = bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode("utf-8")


def _hash_password(password: str) -> str:
    """bcrypt-hash ``password`` and return a utf-8 string safe to store."""
    digest = bcrypt.hashpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES],
                           bcrypt.gensalt())
    return digest.decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt check. Never raises — a malformed stored hash
    (or any unexpected input) is treated as a non-match."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_BCRYPT_MAX_BYTES],
            (password_hash or "").encode("utf-8"),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------

async def signup(email: str, password: str) -> Principal:
    """Create a user + their owning tenant and return the resulting Principal.

    Validation: email must be non-empty (after strip/lower) and the password
    at least ``_MIN_PASSWORD_LEN`` chars — otherwise ``ValueError``. A duplicate
    email raises :class:`EmailExists`. The password is bcrypt-hashed; we INSERT
    one ``users`` row and one ``tenants`` row (``owner_user_id`` = the new user,
    ``name`` defaulting to the email's local-part).

    Every signup — including the very first — mints a brand-new uuid tenant and
    provisions its own isolated Postgres schema via ``ensure_tenant_schema``. No
    account ever inherits the shared ``"public"`` schema (self-serve isolation).
    """
    norm = _normalize_email(email)
    if not norm:
        raise ValueError("email is required")
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(
            f"password must be at least {_MIN_PASSWORD_LEN} characters"
        )

    user_id = uuid.uuid4().hex
    created_at = _now_iso()
    # 7-day free trial (Phase 3): new signups get full access until this moment,
    # after which enforcement flips them to 'expired' → the payment screen.
    trial_ends_at = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")
    password_hash = _hash_password(password)
    # Tenant display name defaults to the email's local-part ("ada" of
    # "ada@example.com"); falls back to the full normalized email if there's
    # no "@".
    tenant_name = norm.split("@", 1)[0] or norm

    async with get_connection() as db:
        # Pre-check keeps the error path engine-agnostic (no reliance on the
        # UNIQUE-constraint error text differing between SQLite and Postgres).
        cur = await db.execute("SELECT id FROM users WHERE email = ?", (norm,))
        existing = await cur.fetchall()
        await cur.close()
        if existing:
            raise EmailExists(norm)

        # Every signup gets its OWN fresh, isolated tenant — no account ever
        # inherits the shared "public" schema (self-serve isolation).
        tenant_id = uuid.uuid4().hex

        try:
            await db.execute(
                "INSERT INTO users "
                "(id, email, password_hash, created_at, access_status, trial_ends_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, norm, password_hash, created_at, "trial", trial_ends_at),
            )
        except Exception as _e:
            # Catch unique-constraint violation (concurrent duplicate signup)
            # and surface a clean EmailExists instead of a raw 500.
            _msg = str(_e).lower()
            if "unique" in _msg or "duplicate" in _msg or "already exists" in _msg:
                raise EmailExists(norm) from _e
            raise
        await db.execute(
            "INSERT INTO tenants (id, owner_user_id, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tenant_id, user_id, tenant_name, created_at),
        )
        await db.commit()

    # Provision the tenant's Postgres schema so its u_* tables land in an
    # isolated namespace before the owner uploads anything (multi-tenant slice
    # 2b). Idempotent (CREATE SCHEMA IF NOT EXISTS) and defensive: a
    # provisioning hiccup must never fail an otherwise-successful signup, so we
    # log and continue — the schema is also ensured lazily on first ingest.
    try:
        from app.db_engine import ensure_tenant_schema
        await ensure_tenant_schema(tenant_id)
    except Exception:
        _log.exception(
            "ensure_tenant_schema failed for tenant=%r (signup continues)",
            tenant_id,
        )

    return Principal(user_id=user_id, tenant_id=tenant_id, email=norm)


async def authenticate(email: str, password: str) -> "Principal | None":
    """Verify credentials and return the Principal, else ``None``.

    Looks the user up by normalized email, bcrypt-verifies the password, and
    resolves the user's tenant via ``tenants.owner_user_id``. Returns ``None``
    on any miss (unknown email, wrong password, or — defensively — a user with
    no tenant row). Never raises.
    """
    norm = _normalize_email(email)
    if not norm or not password:
        return None
    try:
        async with get_connection() as db:
            cur = await db.execute(
                "SELECT id, password_hash FROM users WHERE email = ?", (norm,)
            )
            rows = await cur.fetchall()
            await cur.close()
            if not rows:
                # Run a dummy bcrypt comparison so the response time is
                # indistinguishable from a wrong-password attempt, preventing
                # user enumeration via timing.
                _verify_password("dummy", _DUMMY_HASH)
                return None
            user = dict(rows[0])
            if not _verify_password(password, user.get("password_hash") or ""):
                return None

            cur = await db.execute(
                "SELECT id FROM tenants WHERE owner_user_id = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (user["id"],),
            )
            t_rows = await cur.fetchall()
            await cur.close()
            if not t_rows:
                return None
            tenant_id = dict(t_rows[0])["id"]
    except Exception:
        # A DB hiccup must never surface as a 500 on the login path.
        _log.exception("authenticate failed for email=%r", norm)
        return None

    return Principal(user_id=user["id"], tenant_id=tenant_id, email=norm)


# ---------------------------------------------------------------------------
# Tenant-bearing bearer tokens — reuse app.auth's HMAC primitives, widened
# payload {u, uid, tid, iat, exp}. Signed/verified with the same secret as the
# admin token, so a single AUTH_TOKEN_SECRET rotation invalidates everything.
# ---------------------------------------------------------------------------

def mint_token(principal: Principal) -> str:
    """Mint a fresh signed token carrying the full Principal.

    Payload: ``{"u": email, "uid": user_id, "tid": tenant_id, "iat", "exp"}``.
    TTL is ``settings.auth_token_ttl_hours``; the signature is
    ``HMAC-SHA256(settings.auth_token_secret, payload_b64)`` via the shared
    :func:`app.auth._sign`. Same wire shape ``<payload_b64>.<sig_b64>`` as the
    existing admin token, so one verifier could read either.
    """
    now = int(time.time())
    ttl = max(1, settings.auth_token_ttl_hours) * 3600
    payload: dict[str, Any] = {
        "u": principal.email,
        "uid": principal.user_id,
        "tid": principal.tenant_id,
        "iat": now,
        "exp": now + ttl,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64u_encode(payload_json.encode("utf-8"))
    sig_b64 = _sign(payload_b64, settings.auth_token_secret)
    return f"{payload_b64}.{sig_b64}"


def verify_token(token: str) -> "Principal | None":
    """Return the Principal for a valid, unexpired token, else ``None``.

    Checks the HMAC signature (constant-time) and expiry using the same secret
    as :func:`mint_token`. A tampered payload or signature, a missing/expired
    ``exp``, or any malformed input yields ``None``. Never raises.
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        return None
    expected_sig = _sign(payload_b64, settings.auth_token_secret)
    # Constant-time compare — a timing side-channel leaks the secret over
    # enough samples.
    if not hmac.compare_digest(expected_sig, sig_b64):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    u = payload.get("u")
    uid = payload.get("uid")
    tid = payload.get("tid")
    exp = payload.get("exp")
    if not isinstance(u, str) or not isinstance(uid, str) or not isinstance(tid, str):
        return None
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return Principal(user_id=uid, tenant_id=tid, email=u)


# ---------------------------------------------------------------------------
# Password reset — single-use-ish, time-limited signed tokens + a write that
# rotates the stored bcrypt hash. The token reuses the SAME HMAC primitives and
# the SAME ``settings.auth_token_secret`` as the bearer token above (one secret
# rotation invalidates reset links too), but carries a distinct payload tagged
# ``"k": "pwreset"`` so a bearer token can never be replayed as a reset token
# (or vice-versa) even though both share the wire shape and signer.
# ---------------------------------------------------------------------------

# How long a reset link stays valid. 30 minutes — long enough to find the email
# and click, short enough to limit exposure of a leaked link.
_RESET_TOKEN_TTL_SECONDS = 30 * 60

# Payload discriminator. Verification rejects any token lacking this exact tag,
# so a {u,uid,tid} *login* token (which has no "k") can't satisfy the reset
# verifier.
_RESET_TOKEN_KIND = "pwreset"


def make_password_reset_token(user_id: str, email: str) -> str:
    """Mint a signed, time-limited (~30 min) password-reset token.

    Payload: ``{"k": "pwreset", "uid": user_id, "u": email, "iat", "exp"}``,
    signed ``HMAC-SHA256(settings.auth_token_secret, payload_b64)`` via the
    shared :func:`app.auth._sign` — identical machinery and secret as
    :func:`mint_token`, only the payload (kind + shorter TTL) differs. Wire
    shape is ``<payload_b64>.<sig_b64>``, URL-safe for use as a query param.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "k": _RESET_TOKEN_KIND,
        "uid": user_id,
        "u": email,
        "iat": now,
        "exp": now + _RESET_TOKEN_TTL_SECONDS,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64u_encode(payload_json.encode("utf-8"))
    sig_b64 = _sign(payload_b64, settings.auth_token_secret)
    return f"{payload_b64}.{sig_b64}"


def verify_password_reset_token(token: str) -> "dict | None":
    """Return ``{"user_id": ..., "email": ...}`` for a valid reset token, else
    ``None``.

    Validates (constant-time) the HMAC signature with the same secret as
    :func:`make_password_reset_token`, requires the ``"k" == "pwreset"``
    discriminator, and rejects an expired/missing ``exp``. A tampered payload or
    signature, a login token (no ``"k"``), a malformed string, or any unexpected
    input yields ``None``. Never raises.
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        return None
    expected_sig = _sign(payload_b64, settings.auth_token_secret)
    # Constant-time compare — a timing side-channel leaks the secret over
    # enough samples.
    if not hmac.compare_digest(expected_sig, sig_b64):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("k") != _RESET_TOKEN_KIND:
        return None
    uid = payload.get("uid")
    email = payload.get("u")
    exp = payload.get("exp")
    if not isinstance(uid, str) or not isinstance(email, str):
        return None
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return {"user_id": uid, "email": email}


async def set_password(user_id: str, new_password: str) -> bool:
    """Rotate the stored bcrypt hash for ``user_id`` to ``new_password``.

    Hashes with the SAME bcrypt path used at :func:`signup` and UPDATEs the
    user's ``password_hash`` via the SAME ``get_connection`` helper the rest of
    this module writes through. Enforces the same ``_MIN_PASSWORD_LEN`` floor as
    signup (raises ``ValueError`` on a weak/empty password — the
    ``POST /auth/reset`` route maps that to a 400). Returns ``True`` when a row
    was updated, ``False`` when ``user_id`` matched no user (e.g. account
    deleted between link issuance and use).
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not new_password or len(new_password) < _MIN_PASSWORD_LEN:
        raise ValueError(
            f"password must be at least {_MIN_PASSWORD_LEN} characters"
        )

    password_hash = _hash_password(new_password)
    async with get_connection() as db:
        # Confirm the user exists first so we can report the no-such-user case
        # distinctly (engine-agnostic — no reliance on UPDATE rowcount, which
        # differs across the SQLite/asyncpg shims).
        cur = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            return False
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        await db.commit()
    return True
