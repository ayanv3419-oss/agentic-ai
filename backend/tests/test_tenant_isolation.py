"""Cross-tenant ISOLATION guardrails.

A self-serve multi-tenant SaaS must guarantee Tenant B never sees Tenant A's
data or answers. Two pure invariants underpin that guarantee, and this file
locks them so a future change can't silently reopen a leak:

  1. The response-cache key is TENANT-DISTINCT — two tenants asking the same
     question in the same conversation hash to DIFFERENT keys, so a cached
     numeric answer can never cross tenants (``app.infrastructure.cache_key_for``).
  2. The tenant->schema mapping is TENANT-DISTINCT — two different tenant ids
     map to two different Postgres schemas, which is what makes the
     ``SET LOCAL search_path`` row isolation in ``app.db_engine.pg_connection``
     actually isolate (``app.db_engine.tenant_schema``).

These are deterministic and need no DB, so they run in CI everywhere. The full
end-to-end schema isolation (live ingest in tenant A, then assert tenant B's
``list_dynamic_tables_for_tenant`` returns none of A's tables) is a Postgres
integration check verified against the live Supabase DB — it is intentionally
NOT reproduced here because it needs a provisioned Postgres with per-tenant
schemas; the unit invariants below are the CI gate.

Run:
    cd backend && python -m pytest tests/test_tenant_isolation.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.infrastructure import cache_key_for
from app.db_engine import tenant_schema


# ---------------------------------------------------------------------------
# Invariant 1 — the answer cache key is tenant-distinct
# ---------------------------------------------------------------------------


class TestCacheKeyTenantScoping:
    def test_different_tenants_get_different_keys(self):
        a = cache_key_for("what were total sales?", tenant_id="tenantA")
        b = cache_key_for("what were total sales?", tenant_id="tenantB")
        assert a != b, "same question must hash differently per tenant (leak!)"

    def test_same_tenant_same_question_is_stable(self):
        a1 = cache_key_for("top products", tenant_id="tenantA")
        a2 = cache_key_for("top products", tenant_id="tenantA")
        assert a1 == a2, "same tenant + question must be a stable cache hit"

    def test_default_matches_explicit_public(self):
        # AUTH-off / single-tenant callers omit tenant_id; that must be
        # byte-for-byte identical to the explicit "public" tenant so the
        # default path is unchanged.
        assert cache_key_for("q") == cache_key_for("q", tenant_id="public")

    def test_conversation_still_scopes_within_a_tenant(self):
        # Sanity: tenant scoping is ADDED, not a replacement — other key
        # dimensions (conversation) still differentiate within one tenant.
        c1 = cache_key_for("q", conversation_id="c1", tenant_id="tenantA")
        c2 = cache_key_for("q", conversation_id="c2", tenant_id="tenantA")
        assert c1 != c2


# ---------------------------------------------------------------------------
# Invariant 2 — the tenant->schema mapping is tenant-distinct
# ---------------------------------------------------------------------------


class TestTenantSchemaMapping:
    def test_public_maps_to_public(self):
        assert tenant_schema("public") == "public"
        assert tenant_schema(None) == "public"

    def test_real_tenant_maps_to_prefixed_schema(self):
        s = tenant_schema("abc123")
        assert s == "t_abc123"
        assert s != "public", "a real tenant must NOT resolve to the public schema"

    def test_distinct_tenants_get_distinct_schemas(self):
        # The core isolation primitive: two tenants can never share a schema,
        # so search_path can never collide them.
        s1 = tenant_schema("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        s2 = tenant_schema("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        assert s1 != s2
        assert s1 != "public" and s2 != "public"

    def test_schema_name_is_sanitized(self):
        # Schema names are interpolated into SQL, so the id must be reduced to
        # a safe [a-z0-9_] identifier (defense against injection via tenant id).
        s = tenant_schema("Ab-9!x ")
        assert s.startswith("t_")
        body = s[2:]
        assert all(ch.islower() or ch.isdigit() or ch == "_" for ch in body), s
