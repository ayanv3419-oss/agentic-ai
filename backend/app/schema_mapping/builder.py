"""Metric SQL Builder — builds canonical metric SQL from a ``ResolvedSchema``.

Pure: a ``ResolvedSchema`` in, a SQL string out. Every table and column
name comes from the resolver, so the SQL adapts to whatever the uploaded
workbook named its columns. The metric *formulas* are fixed; only the
identifiers vary.
"""
from __future__ import annotations

from app.schema_mapping.resolver import ColumnRef, ResolvedSchema


class MetricSqlBuilder:
    """Builds canonical metric SQL from a resolved schema. A metric whose
    required concepts are unresolved yields ``None`` so the caller can
    degrade gracefully instead of running a query that would fail."""

    def __init__(self, resolved: ResolvedSchema) -> None:
        self._resolved = resolved

    # -- shared helpers ----------------------------------------------------

    def _margin_refs(self) -> tuple[ColumnRef, ...] | None:
        """The (revenue, quantity, unit_cost, product_label, sku_key) column
        refs, or ``None`` when realized margin/profit cannot be computed on
        this dataset."""
        r = self._resolved
        if not r.can_compute_margin:
            return None
        return tuple(
            r.ref(c) for c in
            ("revenue", "quantity", "unit_cost", "product_label", "sku_key")
        )

    @staticmethod
    def _clean_order(direction: str, limit: int) -> tuple[str, int]:
        """Sanitise the ORDER BY direction + LIMIT. They land in raw SQL
        and the builder is a shared chokepoint, so never trust them blindly."""
        d = "ASC" if str(direction).strip().upper() == "ASC" else "DESC"
        try:
            n = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            n = 10
        return d, n

    # -- metrics -----------------------------------------------------------

    def margin_ranking(
        self, *, direction: str = "DESC", limit: int = 10,
    ) -> str | None:
        """Realized-margin-% product ranking — profit on what actually sold,
        after discounts, computed on SUM totals. ``None`` when the dataset
        lacks a concept the formula needs."""
        refs = self._margin_refs()
        if refs is None:
            return None
        rev, qty, cost, prod, sku = refs
        direction, limit = self._clean_order(direction, limit)
        revenue = f"SUM(s.{rev.column})"
        cogs = f"SUM(s.{qty.column} * i.{cost.column})"
        return (
            f"SELECT s.{prod.column}, "
            f"ROUND({revenue}, 2) AS total_net_sales, "
            f"ROUND({cogs}, 2) AS total_cogs, "
            f"ROUND(({revenue} - {cogs}) * 100.0 / {revenue}, 2) AS margin_pct "
            f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f"ON s.{sku.column} = i.{sku.column} "
            f"GROUP BY s.{prod.column} HAVING {revenue} > 0 "
            f"ORDER BY margin_pct {direction} LIMIT {limit}"
        )

    def profit_ranking(
        self, *, direction: str = "DESC", limit: int = 10,
    ) -> str | None:
        """Profit-AMOUNT product ranking — revenue minus cost of goods, per
        product, on SUM totals. No HAVING filter: loss-making products are
        valid rows and must surface for 'worst profit'. ``None`` when the
        dataset lacks a concept the formula needs."""
        refs = self._margin_refs()
        if refs is None:
            return None
        rev, qty, cost, prod, sku = refs
        direction, limit = self._clean_order(direction, limit)
        revenue = f"SUM(s.{rev.column})"
        cogs = f"SUM(s.{qty.column} * i.{cost.column})"
        return (
            f"SELECT s.{prod.column}, "
            f"ROUND({revenue}, 2) AS total_net_sales, "
            f"ROUND({cogs}, 2) AS total_cogs, "
            f"ROUND({revenue} - {cogs}, 2) AS profit_amount "
            f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f"ON s.{sku.column} = i.{sku.column} "
            f"GROUP BY s.{prod.column} "
            f"ORDER BY profit_amount {direction} LIMIT {limit}"
        )
