"""TDD tests for the schema-mapping deep modules (slice 1 of agentic-ai#1).

Concept Resolver + Metric SQL Builder. Both are pure modules, so these
are plain sync tests - no server, LLM, or DB. Closest prior art:
test_coordinator_fixes.py::TestEnforceLimit (pure-function tests).

Run:
    cd backend && python -m pytest tests/test_schema_mapping.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.schema_mapping import ColumnRef, MetricSqlBuilder, resolve_schema


def _tbl(name: str, *cols: str) -> dict:
    """A u_* table in the shape SchemaTool._list_user_tables() produces."""
    return {"table": name, "columns": [{"name": c, "type": "TEXT"} for c in cols]}


class TestConceptResolver:
    def test_resolves_revenue_from_net_sales_column(self):
        resolved = resolve_schema([_tbl("u_sales", "net_sales", "quantity")])
        ref = resolved.ref("revenue")
        assert ref is not None
        assert ref.table == "u_sales"
        assert ref.column == "net_sales"

    def test_empty_workbook_resolves_to_nothing(self):
        resolved = resolve_schema([])
        assert resolved.can_compute_margin is False
        assert resolved.ref("revenue") is None
        assert resolved.missing("revenue", "unit_cost") == ["revenue", "unit_cost"]

    def test_resolves_all_margin_core_concepts_from_demo_schema(self):
        tables = [
            _tbl("u_sales_transactions", "invoice_no", "net_sales",
                 "quantity", "sku_id", "final_product", "brand"),
            _tbl("u_inventory_master", "sku_id", "unit_cost", "stock_qty"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("revenue") == ColumnRef("u_sales_transactions", "net_sales")
        assert resolved.ref("quantity") == ColumnRef("u_sales_transactions", "quantity")
        assert resolved.ref("sku_key") == ColumnRef("u_sales_transactions", "sku_id")
        assert resolved.ref("unit_cost") == ColumnRef("u_inventory_master", "unit_cost")
        assert resolved.ref("product_label") == ColumnRef(
            "u_sales_transactions", "final_product")
        assert resolved.can_compute_margin is True

    def test_resolves_a_renamed_workbook_via_synonyms(self):
        # Same concepts, none of the demo's column names.
        tables = [
            _tbl("u_orders", "sales_value", "units", "item_code", "item_name"),
            _tbl("u_stock", "item_code", "cost_price"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("revenue") == ColumnRef("u_orders", "sales_value")
        assert resolved.ref("quantity") == ColumnRef("u_orders", "units")
        assert resolved.ref("sku_key") == ColumnRef("u_orders", "item_code")
        assert resolved.ref("unit_cost") == ColumnRef("u_stock", "cost_price")
        assert resolved.ref("product_label") == ColumnRef("u_orders", "item_name")
        assert resolved.can_compute_margin is True

    def test_missing_concept_is_unresolved_and_reported(self):
        # Sales data, but no cost column anywhere - margin is uncomputable.
        tables = [
            _tbl("u_orders", "net_sales", "quantity", "sku_id", "final_product"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("unit_cost") is None
        assert resolved.is_resolved("revenue") is True
        assert resolved.is_resolved("unit_cost") is False
        assert resolved.can_compute_margin is False
        assert resolved.missing("revenue", "unit_cost") == ["unit_cost"]

    def test_ambiguous_concept_is_left_unresolved(self):
        # Two columns both match 'revenue' synonyms and neither is the
        # canonical 'revenue' name - the resolver must not pick one.
        tables = [
            _tbl("u_orders", "net_sales", "total_sales", "quantity",
                 "sku_id", "final_product"),
            _tbl("u_stock", "sku_id", "unit_cost"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("revenue") is None
        assert resolved.can_compute_margin is False
        # the unambiguous concepts still resolve
        assert resolved.ref("quantity") == ColumnRef("u_orders", "quantity")
        assert resolved.ref("unit_cost") == ColumnRef("u_stock", "unit_cost")

    def test_exact_canonical_name_preferred_over_synonym(self):
        # Both a canonical 'revenue' column and a 'net_sales' synonym are
        # present - the canonical name wins, and it is NOT treated as an
        # ambiguous two-match case.
        tables = [
            _tbl("u_orders", "revenue", "net_sales", "quantity",
                 "sku_id", "final_product"),
            _tbl("u_stock", "sku_id", "unit_cost"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("revenue") == ColumnRef("u_orders", "revenue")
        assert resolved.can_compute_margin is True


class TestMetricSqlBuilder:
    def test_margin_ranking_sql_from_resolved_schema(self):
        tables = [
            _tbl("u_sales_transactions", "net_sales", "quantity", "sku_id",
                 "final_product"),
            _tbl("u_inventory_master", "sku_id", "unit_cost"),
        ]
        sql = MetricSqlBuilder(resolve_schema(tables)).margin_ranking()
        assert sql is not None
        # margin % computed on SUM totals, never averaged per-row ratios
        assert 'SUM(s."net_sales")' in sql
        assert 'SUM(s."quantity" * i."unit_cost")' in sql
        assert '* 100.0 / SUM(s."net_sales")' in sql
        assert "AS margin_pct" in sql
        # sales joined to inventory on the resolved sku key
        assert 'FROM "u_sales_transactions" s' in sql
        assert 'JOIN "u_inventory_master" i ON s."sku_id" = i."sku_id"' in sql
        assert 'GROUP BY s."final_product"' in sql
        assert "ORDER BY margin_pct DESC" in sql
        assert "LIMIT 10" in sql

    def test_no_sql_when_cost_table_lacks_the_sku_join_key(self):
        # The inventory sheet has unit_cost but its key column ('barcode')
        # is not a SKU synonym - so sku_key resolves only from the sales
        # sheet and the cost table has no matching join column. The builder
        # must NOT emit SQL that would fail at runtime with 'no such column'.
        tables = [
            _tbl("u_sales", "net_sales", "quantity", "sku_id", "final_product"),
            _tbl("u_inv", "barcode", "unit_cost"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.can_compute_margin is False
        assert MetricSqlBuilder(resolved).margin_ranking() is None

    def test_margin_ranking_returns_none_when_cost_unresolved(self):
        # No cost column anywhere -> margin cannot be computed -> no SQL,
        # so the caller (sqlWriter / _metric_hints) can degrade gracefully.
        tables = [
            _tbl("u_orders", "net_sales", "quantity", "sku_id", "final_product"),
        ]
        sql = MetricSqlBuilder(resolve_schema(tables)).margin_ranking()
        assert sql is None

    def test_margin_ranking_direction_and_limit_are_parametrized(self):
        tables = [
            _tbl("u_sales_transactions", "net_sales", "quantity", "sku_id",
                 "final_product"),
            _tbl("u_inventory_master", "sku_id", "unit_cost"),
        ]
        sql = MetricSqlBuilder(resolve_schema(tables)).margin_ranking(
            direction="ASC", limit=5)
        assert sql is not None
        assert "ORDER BY margin_pct ASC" in sql
        assert "LIMIT 5" in sql

    def test_profit_ranking_sql_from_resolved_schema(self):
        tables = [
            _tbl("u_sales_transactions", "net_sales", "quantity", "sku_id",
                 "final_product"),
            _tbl("u_inventory_master", "sku_id", "unit_cost"),
        ]
        sql = MetricSqlBuilder(resolve_schema(tables)).profit_ranking()
        assert sql is not None
        # profit AMOUNT = revenue minus COGS, on SUM totals
        assert 'SUM(s."net_sales") - SUM(s."quantity" * i."unit_cost")' in sql
        assert "AS profit_amount" in sql
        assert "ORDER BY profit_amount DESC" in sql
        # no HAVING - loss-making products must still surface
        assert "HAVING" not in sql
        assert 'JOIN "u_inventory_master" i ON s."sku_id" = i."sku_id"' in sql

    def test_profit_ranking_returns_none_when_cost_unresolved(self):
        tables = [
            _tbl("u_orders", "net_sales", "quantity", "sku_id", "final_product"),
        ]
        assert MetricSqlBuilder(resolve_schema(tables)).profit_ranking() is None

    def test_margin_ranking_sanitizes_direction_and_limit(self):
        # The builder is a shared chokepoint - it must not splice an
        # unrecognised direction or an out-of-range limit straight into
        # the SQL, regardless of what a caller passes.
        builder = MetricSqlBuilder(resolve_schema([
            _tbl("u_sales_transactions", "net_sales", "quantity", "sku_id",
                 "final_product"),
            _tbl("u_inventory_master", "sku_id", "unit_cost"),
        ]))
        assert "ORDER BY margin_pct DESC" in builder.margin_ranking(
            direction="garbage")
        assert "LIMIT 50" in builder.margin_ranking(limit=9999)
        assert "LIMIT 1" in builder.margin_ranking(limit=0)


class TestSemanticSchemaMapping:
    """Goal-5: the canonical-schema layer — confidence scoring, ambiguity
    reporting, the full concept set, graceful partial resolution of
    unknown workbook schemas."""

    def test_canonical_name_scores_higher_confidence_than_synonym(self):
        # 'revenue' column == the concept's canonical name -> 1.0;
        # 'qty' is only a synonym of 'quantity' -> 0.75.
        tables = [_tbl("u_x", "revenue", "qty", "sku_id", "final_product")]
        resolved = resolve_schema(tables)
        assert resolved.confidence("revenue") == 1.0
        assert resolved.confidence("quantity") == 0.75
        assert resolved.confidence("unit_cost") == 0.0   # unresolved

    def test_ambiguous_concept_is_reported_not_guessed(self):
        # two revenue synonyms, no canonical 'revenue' -> unresolved AND
        # reported in ambiguities() with the competing columns.
        tables = [_tbl("u_x", "net_sales", "total_sales", "qty")]
        resolved = resolve_schema(tables)
        assert resolved.is_resolved("revenue") is False
        amb = resolved.ambiguities()
        assert "revenue" in amb
        assert set(amb["revenue"]) == {"net_sales", "total_sales"}

    def test_resolves_dimensional_and_time_concepts(self):
        tables = [_tbl("u_sales", "net_sales", "brand", "category",
                       "payment_mode", "transaction_date", "region")]
        resolved = resolve_schema(tables)
        assert resolved.ref("brand") == ColumnRef("u_sales", "brand")
        assert resolved.ref("category") == ColumnRef("u_sales", "category")
        assert resolved.ref("payment_mode") == ColumnRef("u_sales", "payment_mode")
        assert resolved.ref("transaction_date") == ColumnRef(
            "u_sales", "transaction_date")
        assert resolved.ref("region") == ColumnRef("u_sales", "region")

    def test_resolves_all_helper(self):
        resolved = resolve_schema([_tbl("u_sales", "net_sales", "quantity")])
        assert resolved.resolves_all("revenue", "quantity") is True
        assert resolved.resolves_all("revenue", "unit_cost") is False

    def test_unknown_workbook_resolves_via_synonyms(self):
        # A workbook sharing NONE of the demo's column names still maps -
        # graceful semantic resolution, not a crash.
        tables = [
            _tbl("u_orders", "turnover", "article", "buyer"),
            _tbl("u_stock", "article", "expense"),
        ]
        resolved = resolve_schema(tables)
        assert resolved.ref("revenue") == ColumnRef("u_orders", "turnover")
        assert resolved.ref("sku_key") == ColumnRef("u_orders", "article")
        assert resolved.ref("customer") == ColumnRef("u_orders", "buyer")
        assert resolved.ref("unit_cost") == ColumnRef("u_stock", "expense")

    def test_summary_reports_resolved_unresolved_ambiguous(self):
        tables = [_tbl("u_x", "net_sales", "total_sales", "brand")]
        s = resolve_schema(tables).summary()
        assert s["resolved"]["brand"]["column"] == "brand"
        assert s["resolved"]["brand"]["confidence"] == 1.0
        assert "revenue" in s["ambiguous"]          # net_sales + total_sales
        assert "unit_cost" in s["unresolved"]
        assert s["can_compute_margin"] is False
