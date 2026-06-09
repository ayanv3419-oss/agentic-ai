"""Metric SQL Builder — builds canonical metric SQL from a ``ResolvedSchema``.

Pure: a ``ResolvedSchema`` in, a SQL string out. Every table and column
name comes from the resolver, so the SQL adapts to whatever the uploaded
workbook named its columns. The metric *formulas* are fixed; only the
identifiers vary.

Strict-on-metrics contract
--------------------------
When a method's required concepts are unresolved, it raises
``MetricNotComputable`` with a clear message naming the missing concepts.
Callers that want graceful degradation should catch this exception (or
inspect ``ResolvedSchema`` first). The KPI engine uses this so the
matched KPI either runs correct SQL or fails with a useful error — never
silently produces wrong numbers.
"""
from __future__ import annotations

from app.schema_mapping.resolver import ColumnRef, ResolvedSchema


class MetricNotComputable(RuntimeError):
    """Raised when a metric cannot be built because a required concept
    did not resolve. Carries the metric name and the missing concepts so
    the engine can surface a clear error to the user."""

    def __init__(self, metric: str, missing: list[str]) -> None:
        self.metric = metric
        self.missing = list(missing)
        super().__init__(
            f"Metric '{metric}' cannot be built: missing concept(s) "
            f"{', '.join(self.missing)}"
        )


class MetricSqlBuilder:
    """Builds canonical metric SQL from a resolved schema.

    All methods return SQL strings whose identifiers come from the
    resolved schema (so they adapt to whatever the workbook named its
    columns). When a required concept does not resolve, the method
    raises ``MetricNotComputable`` — never returns a "best-guess" SQL
    that would silently compute the wrong number."""

    def __init__(self, resolved: ResolvedSchema) -> None:
        self._resolved = resolved

    # -- shared helpers ----------------------------------------------------

    def _require(self, metric: str, *concepts: str) -> tuple[ColumnRef, ...]:
        """Resolve every named concept or raise. Returns the refs in order."""
        missing = self._resolved.missing(*concepts)
        if missing:
            raise MetricNotComputable(metric, missing)
        return tuple(self._resolved.ref(c) for c in concepts)

    def _margin_refs(self) -> tuple[ColumnRef, ...] | None:
        """The (revenue, quantity, unit_cost, product_label, sku_key) column
        refs, or ``None`` when realized margin/profit cannot be computed on
        this dataset (used by the deterministic ranking path which already
        gates on ``can_compute_margin`` and prefers graceful skip)."""
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

    # -- sales metrics ------------------------------------------------------

    def total_revenue(self) -> str:
        """SUM of revenue across all sales rows."""
        (rev,) = self._require("total_revenue", "revenue")
        return (
            f'SELECT COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}"'
        )

    def total_orders(self) -> str:
        """COUNT of distinct invoices/orders. Falls back to row count when
        invoice_id is not resolved (every row is its own order)."""
        (rev,) = self._require("total_orders", "revenue")
        inv = self._resolved.ref("invoice_id")
        if inv is not None:
            return (
                f'SELECT COUNT(DISTINCT "{inv.column}") AS value '
                f'FROM "{inv.table}" '
                f'WHERE "{inv.column}" IS NOT NULL AND "{inv.column}" <> \'\''
            )
        return f'SELECT COUNT(*) AS value FROM "{rev.table}"'

    def total_sales_count(self) -> str:
        """Row count of the sales table."""
        (rev,) = self._require("total_sales_count", "revenue")
        return f'SELECT COUNT(*) AS value FROM "{rev.table}"'

    def average_sale_value(self) -> str:
        """Mean per-row revenue."""
        (rev,) = self._require("average_sale_value", "revenue")
        return (
            f'SELECT COALESCE(AVG("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}"'
        )

    def total_products_sold(self) -> str:
        """Count of distinct SKUs in the sales table."""
        (sku,) = self._require("total_products_sold", "sku_key")
        return (
            f'SELECT COUNT(DISTINCT "{sku.column}") AS value '
            f'FROM "{sku.table}"'
        )

    # -- profit metrics -----------------------------------------------------

    def gross_profit(self) -> str:
        """Revenue minus cost of goods, ONE number, no GROUP BY."""
        rev, qty, cost, sku = self._require(
            "gross_profit", "revenue", "quantity", "unit_cost", "sku_key",
        )
        return (
            f'SELECT (COALESCE(SUM(s."{rev.column}"), 0) '
            f'- COALESCE(SUM(s."{qty.column}" * i."{cost.column}"), 0)) AS value '
            f'FROM "{rev.table}" s '
            f'JOIN "{cost.table}" i ON s."{sku.column}" = i."{sku.column}"'
        )

    def profit_margin_pct(self) -> str:
        """Gross margin as a percentage of revenue, ONE number."""
        rev, qty, cost, sku = self._require(
            "profit_margin_pct",
            "revenue", "quantity", "unit_cost", "sku_key",
        )
        return (
            f'SELECT CASE WHEN COALESCE(SUM(s."{rev.column}"), 0) = 0 THEN 0 '
            f'ELSE (SUM(s."{rev.column}") '
            f'- SUM(s."{qty.column}" * i."{cost.column}")) * 100.0 '
            f'/ SUM(s."{rev.column}") END AS value '
            f'FROM "{rev.table}" s '
            f'JOIN "{cost.table}" i ON s."{sku.column}" = i."{sku.column}"'
        )

    def cost_to_revenue_ratio(self) -> str:
        """COGS / revenue, ONE number."""
        rev, qty, cost, sku = self._require(
            "cost_to_revenue_ratio",
            "revenue", "quantity", "unit_cost", "sku_key",
        )
        return (
            f'SELECT CASE WHEN COALESCE(SUM(s."{rev.column}"), 0) = 0 THEN 0 '
            f'ELSE SUM(s."{qty.column}" * i."{cost.column}") * 1.0 '
            f'/ SUM(s."{rev.column}") END AS value '
            f'FROM "{rev.table}" s '
            f'JOIN "{cost.table}" i ON s."{sku.column}" = i."{sku.column}"'
        )

    def margin_ranking(
        self, *, direction: str = "DESC", limit: int = 10,
    ) -> str | None:
        """Realized-margin-% product ranking — profit on what actually sold,
        after discounts, computed on SUM totals. Returns ``None`` (rather
        than raising) when ``can_compute_margin`` is False, because the
        deterministic ranking path prefers a graceful fall-through to the
        LLM here."""
        refs = self._margin_refs()
        if refs is None:
            return None
        rev, qty, cost, prod, sku = refs
        direction, limit = self._clean_order(direction, limit)
        revenue = f'SUM(s."{rev.column}")'
        cogs = f'SUM(s."{qty.column}" * i."{cost.column}")'
        return (
            f'SELECT s."{prod.column}", '
            f"ROUND({revenue}, 2) AS total_net_sales, "
            f"ROUND({cogs}, 2) AS total_cogs, "
            f"ROUND(({revenue} - {cogs}) * 100.0 / {revenue}, 2) AS margin_pct "
            f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f'ON s."{sku.column}" = i."{sku.column}" '
            f'GROUP BY s."{prod.column}" HAVING {revenue} > 0 '
            f"ORDER BY margin_pct {direction} LIMIT {limit}"
        )

    def profit_ranking(
        self, *, direction: str = "DESC", limit: int = 10,
    ) -> str | None:
        """Profit-AMOUNT product ranking — revenue minus cost of goods, per
        product, on SUM totals. No HAVING filter: loss-making products are
        valid rows and must surface for 'worst profit'. Returns ``None``
        when ``can_compute_margin`` is False — see ``margin_ranking``."""
        refs = self._margin_refs()
        if refs is None:
            return None
        rev, qty, cost, prod, sku = refs
        direction, limit = self._clean_order(direction, limit)
        revenue = f'SUM(s."{rev.column}")'
        cogs = f'SUM(s."{qty.column}" * i."{cost.column}")'
        return (
            f'SELECT s."{prod.column}", '
            f"ROUND({revenue}, 2) AS total_net_sales, "
            f"ROUND({cogs}, 2) AS total_cogs, "
            f"ROUND({revenue} - {cogs}, 2) AS profit_amount "
            f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f'ON s."{sku.column}" = i."{sku.column}" '
            f'GROUP BY s."{prod.column}" '
            f"ORDER BY profit_amount {direction} LIMIT {limit}"
        )

    # -- dimension breakdowns -----------------------------------------------

    def sales_by_brand(self) -> str:
        rev, brand = self._require("sales_by_brand", "revenue", "brand")
        return (
            f'SELECT "{brand.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{brand.column}" IS NOT NULL '
            f'GROUP BY "{brand.column}" ORDER BY value DESC'
        )

    def sales_by_category(self) -> str:
        rev, cat = self._require("sales_by_category", "revenue", "category")
        return (
            f'SELECT "{cat.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{cat.column}" IS NOT NULL '
            f'GROUP BY "{cat.column}" ORDER BY value DESC'
        )

    def sales_by_month(self) -> str:
        rev, date = self._require(
            "sales_by_month", "revenue", "transaction_date",
        )
        return (
            f'SELECT substr("{date.column}", 1, 7) AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{date.column}" IS NOT NULL '
            f'GROUP BY label ORDER BY label ASC'
        )

    def top_brand(self) -> str:
        rev, brand = self._require("top_brand", "revenue", "brand")
        return (
            f'SELECT "{brand.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{brand.column}" IS NOT NULL '
            f'GROUP BY "{brand.column}" ORDER BY value DESC LIMIT 1'
        )

    def top_category(self) -> str:
        rev, cat = self._require("top_category", "revenue", "category")
        return (
            f'SELECT "{cat.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{cat.column}" IS NOT NULL '
            f'GROUP BY "{cat.column}" ORDER BY value DESC LIMIT 1'
        )

    def peak_sales_day(self) -> str:
        rev, date = self._require(
            "peak_sales_day", "revenue", "transaction_date",
        )
        return (
            f'SELECT "{date.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{date.column}" IS NOT NULL '
            f'GROUP BY "{date.column}" ORDER BY value DESC LIMIT 1'
        )

    def payment_method_distribution(self) -> str:
        rev, pay = self._require(
            "payment_method_distribution", "revenue", "payment_mode",
        )
        return (
            f'SELECT "{pay.column}" AS label, '
            f'COALESCE(SUM("{rev.column}"), 0) AS value '
            f'FROM "{rev.table}" WHERE "{pay.column}" IS NOT NULL '
            f'GROUP BY "{pay.column}" ORDER BY value DESC'
        )

    # -- inventory metrics (velocity-based) ---------------------------------

    def _velocity_cte(self) -> tuple[str, ColumnRef, ColumnRef, ColumnRef, ColumnRef]:
        """Return the per-SKU velocity CTE plus the column refs callers need.
        Raises MetricNotComputable when any of the sales-velocity inputs
        (quantity / sku_key / transaction_date) does not resolve."""
        stock, sku, qty, date = self._require(
            "inventory_velocity",
            "stock_qty", "sku_key", "quantity", "transaction_date",
        )
        cte = (
            f'WITH ds AS (SELECT MAX("{date.column}") AS max_date '
            f'FROM "{qty.table}"), '
            f'v AS ( '
            f'  SELECT s."{sku.column}" AS sku, '
            f'         SUM(s."{qty.column}") * 1.0 / NULLIF( '
            f'           JULIANDAY(MAX(s."{date.column}")) '
            f'           - JULIANDAY(MIN(s."{date.column}")) + 1, 0 '
            f'         ) AS avg_daily_sales, '
            f'         MAX(s."{date.column}") AS last_sale_date '
            f'  FROM "{qty.table}" s '
            f'  GROUP BY s."{sku.column}" '
            f') '
        )
        return cte, stock, sku, qty, date

    def count_low_stock_skus(self) -> str:
        cte, stock, sku, _qty, _date = self._velocity_cte()
        return (
            f'{cte} '
            f'SELECT COUNT(*) AS value '
            f'FROM "{stock.table}" i '
            f'JOIN v ON v.sku = i."{sku.column}" '
            f'WHERE i."{stock.column}" * 1.0 / NULLIF(v.avg_daily_sales, 0) < 7'
        )

    def count_dead_stock_skus(self) -> str:
        cte, stock, sku, _qty, _date = self._velocity_cte()
        return (
            f'{cte} '
            f'SELECT COUNT(*) AS value '
            f'FROM "{stock.table}" i '
            f'LEFT JOIN v ON v.sku = i."{sku.column}", ds '
            f'WHERE v.last_sale_date IS NULL '
            f'   OR JULIANDAY(ds.max_date) '
            f'      - JULIANDAY(v.last_sale_date) > 30'
        )

    def count_overstocked_skus(self) -> str:
        cte, stock, sku, _qty, _date = self._velocity_cte()
        return (
            f'{cte} '
            f'SELECT COUNT(*) AS value '
            f'FROM "{stock.table}" i '
            f'JOIN v ON v.sku = i."{sku.column}" '
            f'WHERE i."{stock.column}" * 1.0 / NULLIF(v.avg_daily_sales, 0) > 60'
        )

    def days_of_cover_avg(self) -> str:
        cte, stock, sku, _qty, _date = self._velocity_cte()
        return (
            f'{cte} '
            f'SELECT COALESCE(AVG('
            f'  i."{stock.column}" * 1.0 / NULLIF(v.avg_daily_sales, 0)'
            f'), 0) AS value '
            f'FROM "{stock.table}" i '
            f'JOIN v ON v.sku = i."{sku.column}" '
            f'WHERE v.avg_daily_sales > 0'
        )

    def total_stock_value(self) -> str:
        """SUM(stock_qty * unit_cost) for every SKU. Requires stock_qty and
        unit_cost; SKU join key implicit if cost lives in a different
        table from stock."""
        stock, cost = self._require(
            "total_stock_value", "stock_qty", "unit_cost",
        )
        if stock.table == cost.table:
            return (
                f'SELECT COALESCE(SUM("{stock.column}" * "{cost.column}"), 0) '
                f'AS value FROM "{stock.table}"'
            )
        # Different tables → require sku_key to join.
        (sku,) = self._require("total_stock_value", "sku_key")
        return (
            f'SELECT COALESCE(SUM(s."{stock.column}" * c."{cost.column}"), 0) '
            f'AS value FROM "{stock.table}" s '
            f'JOIN "{cost.table}" c ON s."{sku.column}" = c."{sku.column}"'
        )
