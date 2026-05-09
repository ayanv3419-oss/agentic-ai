"""Regression suite for the multi-user / SaaS foundation pieces:

  - per-(user, conversation_id) ConversationStore replaces _LAST_KIND
  - data_version-stamped cache keys
  - 50 MB upload cap
  - Groq-failure deterministic fallback (unit-level — patches select_sub_agent
    to raise and asserts the coordinator routes to QueryAgent instead of
    erroring the turn)
  - Schema has tenant_id / workspace_id / user_id columns

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_multitenant_foundations.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")
os.environ.setdefault("FINANCIAL_DB_PATH", "/tmp/_mt_test.db")

import aiosqlite  # noqa: E402

from app.analytics_engine import (  # noqa: E402
    EventEmitter,
    TurnState,
    _get_last_kind,
    _reset_last_kind,
    _set_last_kind,
    classify_query_kind,
    run_query_turn,
)
from app.infrastructure import (  # noqa: E402
    bump_data_version,
    cache_key_for,
    get_cache_store,
    get_conversation_store,
    get_data_version,
    init_database,
    quoted,
    settings,
    SCHEMA_COLUMNS,
)


# ---- Conversation isolation ---------------------------------------------

def case_two_users_dont_share_continuity() -> tuple[bool, str]:
    """User A's last_kind must NOT be visible to user B — the previous global
    `_LAST_KIND` would have leaked across."""
    _reset_last_kind()
    _set_last_kind("data_query", user_id="alice", conversation_id="c1")
    _set_last_kind("chat",       user_id="bob",   conversation_id="c1")
    a = _get_last_kind(user_id="alice", conversation_id="c1")
    b = _get_last_kind(user_id="bob",   conversation_id="c1")
    if a != "data_query" or b != "chat":
        return False, f"alice={a!r} bob={b!r}"
    return True, "alice=data_query bob=chat — isolated"


def case_two_conversations_same_user_isolated() -> tuple[bool, str]:
    """Same user with two conversation ids should not bleed continuity."""
    _reset_last_kind()
    _set_last_kind("data_query", user_id="alice", conversation_id="c1")
    _set_last_kind("chat",       user_id="alice", conversation_id="c2")
    c1 = _get_last_kind(user_id="alice", conversation_id="c1")
    c2 = _get_last_kind(user_id="alice", conversation_id="c2")
    if c1 != "data_query" or c2 != "chat":
        return False, f"c1={c1!r} c2={c2!r}"
    return True, "alice/c1=data_query alice/c2=chat — isolated"


def case_anon_default_falls_back_to_legacy_bucket() -> tuple[bool, str]:
    """Calls without ids land in a single 'anon:default' bucket — same as
    the previous global behaviour. Tests that haven't been updated still
    work."""
    _reset_last_kind()
    _set_last_kind("data_query")  # legacy zero-arg
    if _get_last_kind() != "data_query":
        return False, "legacy zero-arg path broken"
    return True, "legacy zero-arg path preserved"


def case_continuity_works_in_classify_query_kind() -> tuple[bool, str]:
    """The conversation context that classify_query_kind consumes is now
    the per-conversation entry, not a global."""
    _reset_last_kind()
    # Pretend user alice's last turn was a data_query.
    kind, _, _ = classify_query_kind(
        "and october", has_data=True, previous_kind="data_query",
    )
    if kind != "data_query":
        return False, f"continuity failed: got {kind}"
    # Same short follow-up but no prior context = chat.
    kind2, _, _ = classify_query_kind("and october", has_data=True, previous_kind=None)
    if kind2 != "chat":
        return False, f"no-context follow-up should be chat, got {kind2}"
    return True, "continuity gate respects previous_kind"


# ---- data_version cache key ---------------------------------------------

def case_data_version_changes_cache_key() -> tuple[bool, str]:
    """Same question hashes to a different key after data_version bumps."""
    v0 = get_data_version()
    k1 = cache_key_for("hello?", tenant_id="t1", conversation_id="c1")
    bump_data_version()
    k2 = cache_key_for("hello?", tenant_id="t1", conversation_id="c1")
    if k1 == k2:
        return False, f"keys equal across version bump: v={v0} k={k1}"
    return True, f"version bumped {v0}->{get_data_version()}; key changed"


def case_tenant_changes_cache_key() -> tuple[bool, str]:
    k_a = cache_key_for("hello?", tenant_id="alice", conversation_id="c1")
    k_b = cache_key_for("hello?", tenant_id="bob",   conversation_id="c1")
    if k_a == k_b:
        return False, "tenant_id ignored"
    return True, "tenant_id namespaces the key"


def case_conversation_changes_cache_key() -> tuple[bool, str]:
    k1 = cache_key_for("and october", conversation_id="c1")
    k2 = cache_key_for("and october", conversation_id="c2")
    if k1 == k2:
        return False, "conversation_id ignored"
    return True, "conversation_id namespaces the key"


# ---- Upload cap ---------------------------------------------------------

def case_upload_cap_is_50_mb() -> tuple[bool, str]:
    """The per-process default must NOT be the legacy 1 GB."""
    expected = 50 * 1024 * 1024
    if settings.max_upload_bytes != expected:
        return False, f"max_upload_bytes={settings.max_upload_bytes} (expected {expected})"
    return True, "max_upload_bytes = 50 MB"


# ---- Schema has tenant columns ------------------------------------------

async def case_schema_has_tenant_columns() -> tuple[bool, str]:
    """Newly initialised tables MUST have tenant_id, workspace_id, user_id
    so the day multi-tenancy lands, no schema migration is required."""
    # Use a fresh DB path so we're testing the CREATE TABLE path, not just
    # the ALTER TABLE migration.
    test_db = "/tmp/_mt_schema_test.db"
    if Path(test_db).exists():
        Path(test_db).unlink()
    saved = settings.financial_db_path
    settings.financial_db_path = test_db
    try:
        await init_database()
        async with aiosqlite.connect(test_db) as db:
            cur = await db.execute(f"PRAGMA table_info({quoted('sales')})")
            cols = {row[1] for row in await cur.fetchall()}
    finally:
        settings.financial_db_path = saved
        if Path(test_db).exists():
            Path(test_db).unlink()
    required = {"tenant_id", "workspace_id", "user_id"}
    missing = required - cols
    if missing:
        return False, f"missing columns: {sorted(missing)}"
    return True, f"schema has {sorted(required)}"


# ---- Groq-failure deterministic fallback --------------------------------

async def case_groq_dispatcher_failure_falls_back() -> tuple[bool, str]:
    """When the LLM dispatcher raises, the coordinator must fall back to
    QueryAgent (a deterministic generic-summary path) instead of erroring
    the entire turn. Verified by patching select_sub_agent to raise."""
    import app.analytics_engine as ae

    captured: list[tuple[str, dict]] = []

    class Cap(EventEmitter):
        async def emit(self, ev, payload):
            captured.append((ev, payload))

    # Use a query that classify_intent returns at LOW confidence so the
    # coordinator hits the LLM dispatcher path. "hi today" is intentionally
    # weird — short, no domain match.
    question = "give me xqz analytics for blah"  # gibberish -> low confidence

    async def boom(_q):
        raise RuntimeError("simulated Groq outage")

    saved = ae.select_sub_agent
    ae.select_sub_agent = boom  # type: ignore[assignment]
    try:
        # Force the analytics route by setting has_data to True via the DB
        # already initialised by an earlier case.
        state = TurnState(question=question, user_id="alice", conversation_id="c1")
        state = await run_query_turn(state, Cap())
    finally:
        ae.select_sub_agent = saved  # type: ignore[assignment]

    dispatched = [p for ev, p in captured if ev == "sub_agent.dispatched"]
    if not dispatched:
        return False, "no sub_agent.dispatched event"
    strategy = dispatched[0].get("strategy")
    sub_agent = dispatched[0].get("sub_agent")
    if strategy != "deterministic_fallback":
        return False, f"strategy={strategy} sub_agent={sub_agent}"
    if sub_agent != "QueryAgent":
        return False, f"sub_agent={sub_agent} (expected QueryAgent)"
    return True, "fallback strategy=deterministic_fallback sub_agent=QueryAgent"


# ---- Backend kinds expose for /health -----------------------------------

def case_health_exposes_backend_kinds() -> tuple[bool, str]:
    """The cache + conversation backends must report a `kind()` so /health
    can show ops which impl is wired."""
    cs = get_cache_store().kind()
    convo = get_conversation_store().kind()
    if cs != "json_file":
        return False, f"cache backend kind = {cs!r}"
    if convo != "in_memory":
        return False, f"conversation backend kind = {convo!r}"
    return True, "cache=json_file convo=in_memory"


# ---- Runner -------------------------------------------------------------

SYNC_CASES = [
    ("two users, same conversation_id, isolated continuity",
     case_two_users_dont_share_continuity),
    ("same user, two conversation_ids, isolated continuity",
     case_two_conversations_same_user_isolated),
    ("legacy zero-arg path still works (anon:default bucket)",
     case_anon_default_falls_back_to_legacy_bucket),
    ("classify_query_kind continuity gate honours previous_kind",
     case_continuity_works_in_classify_query_kind),
    ("data_version bump changes cache key",
     case_data_version_changes_cache_key),
    ("tenant_id namespaces cache key",
     case_tenant_changes_cache_key),
    ("conversation_id namespaces cache key",
     case_conversation_changes_cache_key),
    ("max_upload_bytes default = 50 MB",
     case_upload_cap_is_50_mb),
    ("backend kinds exposed for /health",
     case_health_exposes_backend_kinds),
]

ASYNC_CASES = [
    ("schema has tenant_id / workspace_id / user_id",
     case_schema_has_tenant_columns),
    ("LLM dispatcher failure -> deterministic QueryAgent fallback",
     case_groq_dispatcher_failure_falls_back),
]


def main() -> int:
    print("=== Multi-tenant foundations ===")
    passed = failed = 0
    for label, fn in SYNC_CASES:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:64} :: {detail}")
        passed += int(ok); failed += int(not ok)

    for label, fn in ASYNC_CASES:
        try:
            ok, detail = asyncio.run(fn())
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:64} :: {detail}")
        passed += int(ok); failed += int(not ok)

    total = passed + failed
    print(f"\nTOTAL: {passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
