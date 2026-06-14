"""Unit tests for SqlDryRun's _validate_shape static checks.

Focus: the schema-qualified-table-source block that closes the
cross-tenant isolation hole (an LLM-written `FROM t_other.u_sales`
or `public.users` would otherwise escape the tenant's search_path).

These exercise the pure validator only — no DB / EXPLAIN needed.

Run:
    cd backend && python -m pytest tests/test_sql_dry_run_validate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.coordinator.tools.sql_dry_run import _validate_shape


# ---------------------------------------------------------------------------
# Schema-qualified table sources are REJECTED (the isolation hole).
# ---------------------------------------------------------------------------


class TestQualifiedTableSourceRejected:
    def test_cross_tenant_qualifier_in_from_rejected(self):
        err = _validate_shape("SELECT * FROM t_acme.u_sales")
        assert err is not None
        assert "schema-qualified" in err.lower()

    def test_cross_tenant_qualifier_in_join_rejected(self):
        err = _validate_shape(
            "SELECT a.x FROM u_sales a JOIN t_other.u_orders b ON a.id = b.id"
        )
        assert err is not None
        assert "schema-qualified" in err.lower()

    def test_public_qualifier_table_source_rejected(self):
        err = _validate_shape("SELECT * FROM public.users")
        assert err is not None
        # Either the qualified-source message or the public-qualifier message
        # is acceptable; both signal the same isolation problem.
        assert (
            "schema-qualified" in err.lower() or "public" in err.lower()
        )

    def test_pg_catalog_qualifier_rejected(self):
        # pg_catalog also trips the system-catalog check; either rejection is fine.
        err = _validate_shape("SELECT * FROM pg_catalog.pg_tables")
        assert err is not None

    def test_quoted_schema_qualified_source_rejected(self):
        err = _validate_shape('SELECT * FROM "t_other"."u_sales"')
        assert err is not None
        assert "schema-qualified" in err.lower()

    def test_whitespace_around_dot_still_rejected(self):
        err = _validate_shape("SELECT * FROM t_other . u_sales")
        assert err is not None
        assert "schema-qualified" in err.lower()


# ---------------------------------------------------------------------------
# System / metadata catalogs are REJECTED (pre-existing check, kept honest).
# ---------------------------------------------------------------------------


class TestSystemCatalogRejected:
    def test_information_schema_rejected(self):
        err = _validate_shape(
            "SELECT table_name FROM information_schema.tables"
        )
        assert err is not None

    def test_sqlite_master_rejected(self):
        err = _validate_shape("SELECT name FROM sqlite_master")
        assert err is not None


# ---------------------------------------------------------------------------
# Legitimate unqualified queries PASS — including column-qualified refs
# (alias.col / table.col) in SELECT / WHERE / ORDER BY / GROUP BY.
# ---------------------------------------------------------------------------


class TestLegitimateQueriesPass:
    def test_simple_unqualified_select_passes(self):
        assert _validate_shape("SELECT * FROM u_sales") is None

    def test_column_qualified_refs_pass(self):
        sql = (
            "SELECT s.product, SUM(s.amount) AS total "
            "FROM u_sales s "
            "WHERE s.region = 'west' "
            "GROUP BY s.product "
            "ORDER BY s.product DESC"
        )
        assert _validate_shape(sql) is None

    def test_join_with_column_qualifiers_passes(self):
        sql = (
            "SELECT a.product, b.qty "
            "FROM u_sales a "
            "JOIN u_orders b ON a.id = b.sale_id "
            "WHERE a.amount > 0"
        )
        assert _validate_shape(sql) is None

    def test_aliased_table_source_passes(self):
        # `FROM u_sales s` — alias, not a qualifier — must still work.
        assert _validate_shape("SELECT s.x FROM u_sales s") is None

    def test_quoted_unqualified_table_passes(self):
        assert _validate_shape('SELECT * FROM "My Table"') is None

    def test_cte_with_qualified_columns_passes(self):
        sql = (
            "WITH t AS (SELECT product, SUM(amt) s FROM u_sales GROUP BY product) "
            "SELECT t.product, t.s FROM t ORDER BY t.s DESC"
        )
        assert _validate_shape(sql) is None
