"""Metric SQL Builder — builds canonical metric SQL from a ``ResolvedSchema``.

Pure: a ``ResolvedSchema`` in, a SQL string out. Every table and column
name comes from the resolver, so the SQL adapts to whatever the uploaded
workbook named its columns. The metric *formulas* are fixed; only the
identifiers vary.
"""
from __future__ import annotations

from app.schema_mapping.resolver import ResolvedSchema


class MetricSqlBuilder:
    """Builds canonical metric SQL from a resolved schema."""

    def __init__(self, resolved: ResolvedSchema) -> None:
        self._resolved = resolved

    def margin_ranking(
        self, *, direction: str = "DESC", limit: int = 10,
    ) -> str | None:
        """Realized-margin-% product ranking. Margin is profit on what
        actually sold, after discounts, computed on SUM totals.

        ``direction`` is the ORDER BY direction ("DESC" for best, "ASC"
        for worst); ``limit`` caps the rows. Both come from trusted
        internal callers (the deterministic routing path / KPI registry).

        Returns ``None`` when the dataset lacks a concept the formula
        needs, so the caller can degrade gracefully instead of running a
        query that would fail."""
        r = self._resolved
        if not r.can_compute_margin:
            return None
        # Sanitise the order/limit even though callers are trusted: this
        # is a shared chokepoint and the values land in raw SQL.
        direction = "ASC" if str(direction).strip().upper() == "ASC" else "DESC"
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10
        rev = r.ref("revenue")
        qty = r.ref("quantity")
        cost = r.ref("unit_cost")
        prod = r.ref("product_label")
        sku = r.ref("sku_key")
        return (
            f"SELECT s.{prod.column}, "
            f"ROUND(SUM(s.{rev.column}), 2) AS total_net_sales, "
            f"ROUND(SUM(s.{qty.column} * i.{cost.column}), 2) AS total_cogs, "
            f"ROUND((SUM(s.{rev.column}) - SUM(s.{qty.column} * i.{cost.column})) "
            f"* 100.0 / SUM(s.{rev.column}), 2) AS margin_pct "
            f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f"ON s.{sku.column} = i.{sku.column} "
            f"GROUP BY s.{prod.column} HAVING SUM(s.{rev.column}) > 0 "
            f"ORDER BY margin_pct {direction} LIMIT {limit}"
        )
