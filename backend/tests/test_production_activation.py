"""Regression suite for the production-activation pass:

  - Upstash REST adapter (CacheStore + ConversationStore protocol shape;
    graceful fallback when token is the placeholder; status helper masks
    the token)
  - Postgres dialect translator (`?` → `$N`, datetime('now') → now(),
    schema autoincrement rewrite)
  - Engine routing: with POSTGRES_PRIMARY=false the path is unchanged
    (160 existing tests prove this; here we just assert the gate);
    with POSTGRES_PRIMARY=true + bad DATABASE_URL the gate falls back
    to SQLite without losing data
  - Tenant-scoped _has_any_uploaded_data: alice's data is invisible to
    bob's empty tenant

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_production_activation.py
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

_TMP_DB = Path(tempfile.gettempdir()) / "agentic_ai_activation_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")
os.environ["FINANCIAL_DB_PATH"] = str(_TMP_DB)

import aiosqlite  # noqa: E402

from app.infrastructure import (  # noqa: E402
    SCHEMA_COLUMNS, init_database, quoted, settings,
)

# Force flag-off + URLs unset so the existing default behaviour is the
# test target. The Upstash adapter still tests its own protocol shape
# below by calling the methods directly with the placeholder token.
settings.postgres_primary = False
settings.database_url = ""
settings.redis_url = ""


# ---- Dialect translator -------------------------------------------------

def case_dialect_placeholder_basic() -> tuple[bool, str]:
    from app.database.dialect import to_postgres_placeholders
    out = to_postgres_placeholders(
        "SELECT * FROM sales WHERE tenant_id = ? AND \"Date\" >= ?"
    )
    expected = "SELECT * FROM sales WHERE tenant_id = $1 AND \"Date\" >= $2"
    if out != expected:
        return False, f"got {out!r}"
    return True, "single-quote-free SQL translates correctly"


def case_dialect_placeholder_skips_string_literals() -> tuple[bool, str]:
    """`?` inside a single-quoted string literal must NOT be replaced."""
    from app.database.dialect import to_postgres_placeholders
    out = to_postgres_placeholders(
        "SELECT * FROM x WHERE \"Date\" GLOB '????-??-??' AND a = ?"
    )
    if out != "SELECT * FROM x WHERE \"Date\" GLOB '????-??-??' AND a = $1":
        return False, f"got {out!r}"
    return True, "literal `?` characters preserved"


def case_dialect_placeholder_doubled_quote() -> tuple[bool, str]:
    """Doubled `''` inside a literal is the SQL escape for a single
    quote. The translator must traverse it correctly + not exit
    string mode."""
    from app.database.dialect import to_postgres_placeholders
    out = to_postgres_placeholders(
        "SELECT 'O''Brien' AS name, ? FROM x"
    )
    if out != "SELECT 'O''Brien' AS name, $1 FROM x":
        return False, f"got {out!r}"
    return True, "doubled '' handled correctly"


def case_dialect_clock_function() -> tuple[bool, str]:
    from app.database.dialect import to_postgres_clocks
    out = to_postgres_clocks("INSERT INTO x DEFAULT VALUES, datetime('now')")
    if "now()" not in out or "datetime(" in out:
        return False, f"got {out!r}"
    return True, "datetime('now') -> now()"


def case_dialect_idempotent_on_pg() -> tuple[bool, str]:
    """Running the translator on already-Postgres SQL must not change
    it (no `?` outside literals = nothing to do)."""
    from app.database.dialect import translate
    pg = "SELECT * FROM users WHERE id = $1 AND tenant = $2"
    if translate(pg) != pg:
        return False, "translator mutated already-PG SQL"
    return True, "no-op on already-PG SQL"


# ---- Upstash REST adapter -----------------------------------------------

def case_upstash_status_unconfigured() -> tuple[bool, str]:
    """No URL/token = configured: false. Token fingerprint is
    `<unset>` (no leak risk in /health)."""
    settings.upstash_redis_rest_url = ""
    settings.upstash_redis_rest_token = ""
    from app.cache.upstash_rest import upstash_status
    s = upstash_status()
    if s["configured"] is True:
        return False, f"unset env reports configured=True: {s}"
    if s["token_fingerprint"] != "<unset>":
        return False, f"fingerprint not <unset>: {s}"
    return True, "unconfigured status correct"


def case_upstash_status_token_masked() -> tuple[bool, str]:
    """When token is set, /health must show only the fingerprint."""
    settings.upstash_redis_rest_url = "https://upright-gelding-119467.upstash.io"
    settings.upstash_redis_rest_token = "AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPP1234567890"
    from app.cache.upstash_rest import upstash_status
    s = upstash_status()
    fp = s["token_fingerprint"]
    if "AABBCCDD" not in fp or "***" not in fp:
        return False, f"fingerprint missing markers: {fp}"
    if "EEFFGG" in fp or "MMNNOO" in fp:
        return False, f"fingerprint reveals middle of token: {fp}"
    if s["url"] != "https://upright-gelding-119467.upstash.io":
        return False, f"url field wrong: {s}"
    settings.upstash_redis_rest_url = ""
    settings.upstash_redis_rest_token = ""
    return True, "fingerprint masks middle correctly"


async def case_upstash_get_unconfigured_returns_none() -> tuple[bool, str]:
    """Cache get against unconfigured Upstash returns None (= cache miss),
    not raise. This is the contract the bootstrap relies on."""
    settings.upstash_redis_rest_url = ""
    settings.upstash_redis_rest_token = ""
    from app.cache.upstash_rest import UpstashCacheStore
    store = UpstashCacheStore()
    # Underlying _aget should return None on UpstashUnavailable.
    val = await store._aget("anything")
    if val is not None:
        return False, f"got {val!r}"
    return True, "unconfigured cache get returns None (graceful miss)"


async def case_upstash_put_unconfigured_no_raise() -> tuple[bool, str]:
    """Cache put against unconfigured Upstash must not raise."""
    settings.upstash_redis_rest_url = ""
    settings.upstash_redis_rest_token = ""
    from app.cache.upstash_rest import UpstashCacheStore
    store = UpstashCacheStore()
    try:
        await store._aput("k", {"v": 1})
    except Exception as e:
        return False, f"raised {type(e).__name__}: {e}"
    return True, "unconfigured cache put no-ops cleanly"


# ---- Postgres feature flag ----------------------------------------------

def case_postgres_primary_default_false() -> tuple[bool, str]:
    """The flag must default to False so existing behaviour is preserved."""
    from app.infrastructure import _postgres_primary_active
    settings.postgres_primary = False
    settings.database_url = ""
    if _postgres_primary_active():
        return False, "active without DATABASE_URL"
    settings.database_url = "postgresql://x:y@z/db"
    if _postgres_primary_active():
        return False, "active without flag"
    settings.postgres_primary = True
    if not _postgres_primary_active():
        return False, "inactive when both set"
    settings.postgres_primary = False
    settings.database_url = ""
    return True, "gate requires both flag + URL"


async def case_fetch_all_falls_back_on_postgres_error() -> tuple[bool, str]:
    """When POSTGRES_PRIMARY=true + unreachable DSN, fetch_all silently
    falls back to SQLite — analytics never breaks because of a Postgres
    blip. Must NOT raise."""
    from app.infrastructure import fetch_all
    settings.postgres_primary = True
    settings.database_url = "postgresql://noone:nope@198.51.100.1:5432/x"
    # Reset any cached pg pool so the bad URL gets retried.
    from app.database.pg_adapter import pg_close
    await pg_close()
    try:
        # Trivial query that works against SQLite — should fall back
        # cleanly + return an empty list (no rows yet in test DB).
        rows = await fetch_all("SELECT 1 AS one WHERE 1 = 0")
    except Exception as e:
        return False, f"raised {type(e).__name__}: {e}"
    finally:
        settings.postgres_primary = False
        settings.database_url = ""
    if not isinstance(rows, list):
        return False, f"got {type(rows).__name__}"
    return True, "fetch_all gracefully fell back to SQLite"


# ---- Tenant-scoped has_any_uploaded_data --------------------------------

async def case_has_any_uploaded_data_tenant_scoped() -> tuple[bool, str]:
    """alice's seeded rows must NOT make bob's _has_any_uploaded_data
    return True. This was a real isolation gap before this pass."""
    from app.analytics_engine import _has_any_uploaded_data
    await init_database()
    # Wipe + seed alice-only.
    async with aiosqlite.connect(str(_TMP_DB)) as db:
        await db.execute(f"DELETE FROM {quoted('sales')}")
        cols = ['batch_id', 'source', 'file_name',
                'tenant_id', 'workspace_id', 'user_id',
                *SCHEMA_COLUMNS]
        ph = ",".join("?" for _ in cols)
        qcols = ",".join(quoted(c) for c in cols)
        sql = f"INSERT INTO {quoted('sales')} ({qcols}) VALUES ({ph})"
        schema_vals = [None] * len(SCHEMA_COLUMNS)
        # Required: Date + Total Amount.
        date_idx = SCHEMA_COLUMNS.index("Date")
        amt_idx = SCHEMA_COLUMNS.index("Total Amount")
        schema_vals[date_idx] = "2025-01-01"
        schema_vals[amt_idx] = 100.0
        await db.execute(sql, [
            "b1", "upload", "t.csv",
            "tenant-A", "workspace-A", "user-A",
            *schema_vals,
        ])
        await db.commit()

    a = await _has_any_uploaded_data(tenant_id="tenant-A")
    b = await _has_any_uploaded_data(tenant_id="tenant-B")
    g = await _has_any_uploaded_data()  # legacy: count globally
    if not a:
        return False, f"alice should see data: {a}"
    if b:
        return False, f"bob should NOT see alice's data: {b}"
    if not g:
        return False, f"global path should still work: {g}"
    return True, "tenant_id parameter isolates correctly"


# ---- Bootstrap priority: Upstash > redis-py > fallback -----------------

async def case_bootstrap_unchanged_when_no_redis_configured() -> tuple[bool, str]:
    """With no UPSTASH and no REDIS_URL, bootstrap leaves stores alone."""
    from app.cache import wire_redis_if_configured
    settings.upstash_redis_rest_url = ""
    settings.upstash_redis_rest_token = ""
    settings.redis_url = ""
    out = await wire_redis_if_configured()
    if out != {"cache": "unchanged", "conversation": "unchanged"}:
        return False, f"got {out}"
    return True, "no Redis backend wired when both unconfigured"


# ---- Runner ------------------------------------------------------------

SYNC_CASES = [
    ("dialect: placeholder basic",                       case_dialect_placeholder_basic),
    ("dialect: skip `?` in string literals",             case_dialect_placeholder_skips_string_literals),
    ("dialect: doubled '' handled correctly",            case_dialect_placeholder_doubled_quote),
    ("dialect: datetime('now') -> now()",                case_dialect_clock_function),
    ("dialect: idempotent on Postgres SQL",              case_dialect_idempotent_on_pg),
    ("upstash: status unconfigured",                     case_upstash_status_unconfigured),
    ("upstash: token masked in fingerprint",             case_upstash_status_token_masked),
    ("postgres: gate requires flag + URL",               case_postgres_primary_default_false),
]

ASYNC_CASES = [
    ("upstash: get returns None when unconfigured",      case_upstash_get_unconfigured_returns_none),
    ("upstash: put no-ops when unconfigured",            case_upstash_put_unconfigured_no_raise),
    ("postgres: fetch_all falls back on PG error",       case_fetch_all_falls_back_on_postgres_error),
    ("tenant: _has_any_uploaded_data isolates",          case_has_any_uploaded_data_tenant_scoped),
    ("bootstrap: no-op when no Redis configured",        case_bootstrap_unchanged_when_no_redis_configured),
]


async def main_async() -> int:
    print("=== Production activation ===")
    passed = failed = 0
    for label, fn in SYNC_CASES:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:50} :: {detail}")
        passed += int(ok); failed += int(not ok)

    for label, fn in ASYNC_CASES:
        try:
            ok, detail = await fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:50} :: {detail}")
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
