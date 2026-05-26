"""KPI registry — persistent SQLite table + seed catalog + CRUD helpers.

Schema (single table `kpi_registry`):

    id                  TEXT PK    stable identifier (e.g. 'profit_margin')
    kpi_name            TEXT       human display name ("Profit Margin")
    kpi_category        TEXT       'sales' | 'profit' | 'orders' | ...
    description         TEXT       one-line explanation
    formula_expression  TEXT       math notation, e.g. "(Revenue - COGS)/Revenue * 100"
    required_columns    TEXT       JSON array of canonical column names
    sql_template        TEXT       SQL that returns the KPI value(s)
    aggregation_type    TEXT       'sum' | 'avg' | 'count' | 'ratio' | 'percent' | 'distribution'
    output_type         TEXT       'currency' | 'percent' | 'count' | 'ratio' | 'list'
    chart_supported     INTEGER    0 or 1
    aliases             TEXT       JSON array of natural-language aliases
    enabled             INTEGER    0 or 1
    created_at          TEXT       ISO timestamp
    updated_at          TEXT       ISO timestamp
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.infrastructure import db_path


_log = logging.getLogger("agentic_ai.kpi.registry")


KPI_TABLE = "kpi_registry"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {KPI_TABLE} (
    id                 TEXT PRIMARY KEY,
    kpi_name           TEXT NOT NULL UNIQUE,
    kpi_category       TEXT NOT NULL,
    description        TEXT NOT NULL,
    formula_expression TEXT NOT NULL,
    required_columns   TEXT NOT NULL,
    sql_template       TEXT NOT NULL,
    aggregation_type   TEXT NOT NULL,
    output_type        TEXT NOT NULL,
    chart_supported    INTEGER NOT NULL DEFAULT 0,
    aliases            TEXT NOT NULL DEFAULT '[]',
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_INDEX_DDL = (
    f'CREATE INDEX IF NOT EXISTS "idx_{KPI_TABLE}_category" ON {KPI_TABLE}(kpi_category)',
    f'CREATE INDEX IF NOT EXISTS "idx_{KPI_TABLE}_enabled"  ON {KPI_TABLE}(enabled)',
)


@dataclass
class KpiRow:
    id: str
    kpi_name: str
    kpi_category: str
    description: str
    formula_expression: str
    required_columns: list[str]
    sql_template: str
    aggregation_type: str
    output_type: str
    chart_supported: bool
    aliases: list[str]
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_db(cls, row: dict[str, Any]) -> "KpiRow":
        return cls(
            id=row["id"],
            kpi_name=row["kpi_name"],
            kpi_category=row["kpi_category"],
            description=row["description"],
            formula_expression=row["formula_expression"],
            required_columns=json.loads(row.get("required_columns") or "[]"),
            sql_template=row["sql_template"],
            aggregation_type=row["aggregation_type"],
            output_type=row["output_type"],
            chart_supported=bool(row.get("chart_supported", 0)),
            aliases=json.loads(row.get("aliases") or "[]"),
            enabled=bool(row.get("enabled", 1)),
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
        )


# ===========================================================================
# Default catalog — every KPI ships with formula + SQL + aliases.
# All templates assume the canonical schema: sales/purchase tables with
# columns Date, "Total Amount", "Party Name", "Product Name", "Order No",
# "Balance Due", "Received/Paid Amount", "Payment Type", "GSTIN",
# "Loyalty Redeemed", and the per-payment columns.
# ===========================================================================

DEFAULT_KPIS: list[dict[str, Any]] = [
    # ─── SALES ─────────────────────────────────────────────────────────────
    {
        "id": "total_revenue",
        "kpi_name": "Total Revenue",
        "kpi_category": "sales",
        "description": "Sum of all sales transactions.",
        "formula_expression": "SUM(Total Amount)",
        "required_columns": ["Total Amount"],
        "sql_template": 'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales',
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["total revenue", "total sales", "revenue", "gmv", "gross merchandise value", "topline"],
    },
    {
        "id": "total_sales_count",
        "kpi_name": "Total Sales Transactions",
        "kpi_category": "sales",
        "description": "Count of every recorded sales row.",
        "formula_expression": "COUNT(*)",
        "required_columns": [],
        "sql_template": "SELECT COUNT(*) AS value FROM sales",
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["sales count", "number of sales", "transactions", "transaction count"],
    },
    {
        "id": "average_sale_value",
        "kpi_name": "Average Sale Value",
        "kpi_category": "sales",
        "description": "Mean amount per individual sales transaction.",
        "formula_expression": "AVG(Total Amount)",
        "required_columns": ["Total Amount"],
        "sql_template": 'SELECT COALESCE(AVG("Total Amount"), 0) AS value FROM sales',
        "aggregation_type": "avg",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["average sale", "avg sale", "mean sale value", "average transaction value"],
    },
    {
        "id": "sales_today",
        "kpi_name": "Sales (latest dataset day)",
        "kpi_category": "sales",
        "description": (
            "Revenue on the dataset's most recent day "
            "(MAX(Date) — not the machine clock)."
        ),
        "formula_expression": 'SUM(Total Amount) WHERE Date = dataset_today',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE "Date" = \'{dataset_today}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": [
            "today sales", "sales today", "todays revenue", "sales for today",
            "last day sales", "last day", "latest day sales",
        ],
    },
    {
        "id": "yesterday_sales",
        "kpi_name": "Yesterday's Sales (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue one day before the dataset's most recent day.",
        "formula_expression": 'SUM(Total Amount) WHERE Date = dataset_today - 1',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE "Date" = \'{dataset_yesterday}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["yesterday sales", "yesterday", "yesterdays sales", "previous day sales"],
    },
    {
        "id": "last_7_days_sales",
        "kpi_name": "Last 7 Days Sales (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue across the 7 most recent dataset days, inclusive.",
        "formula_expression": 'SUM(Total Amount) WHERE Date BETWEEN dataset_today - 6 AND dataset_today',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE "Date" BETWEEN \'{dataset_last_7_start}\' AND \'{dataset_today}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": [
            "last 7 days sales", "last 7 days", "last week sales",
            "past 7 days", "weekly sales", "past week",
        ],
    },
    {
        "id": "last_30_days_sales",
        "kpi_name": "Last 30 Days Sales (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue across the 30 most recent dataset days, inclusive.",
        "formula_expression": 'SUM(Total Amount) WHERE Date BETWEEN dataset_today - 29 AND dataset_today',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE "Date" BETWEEN \'{dataset_last_30_start}\' AND \'{dataset_today}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["last 30 days sales", "last 30 days", "past 30 days", "last month sales", "monthly run rate"],
    },
    {
        "id": "sales_this_week",
        "kpi_name": "Sales This Week (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue for the ISO week containing dataset_today (Mon-Sun).",
        "formula_expression": 'SUM(Total Amount) WHERE Date IN current_dataset_week',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE "Date" BETWEEN \'{dataset_week_start}\' AND \'{dataset_week_end}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["this week sales", "weekly sales window", "current week sales"],
    },
    {
        "id": "sales_this_month",
        "kpi_name": "Sales This Month (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue in the calendar month of dataset_today.",
        "formula_expression": 'SUM(Total Amount) WHERE month = dataset_month',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE substr("Date", 1, 7) = \'{dataset_month}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["monthly sales", "this month sales", "mtd revenue", "month to date"],
    },
    {
        "id": "previous_month_sales",
        "kpi_name": "Previous Month Sales (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue in the calendar month before dataset_today's month.",
        "formula_expression": 'SUM(Total Amount) WHERE month = dataset_prev_month',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE substr("Date", 1, 7) = \'{dataset_prev_month}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["previous month sales", "last month sales", "prior month sales", "previous month revenue"],
    },
    {
        "id": "sales_this_year",
        "kpi_name": "Sales This Year (dataset-relative)",
        "kpi_category": "sales",
        "description": "Revenue in the calendar year of dataset_today.",
        "formula_expression": 'SUM(Total Amount) WHERE year = dataset_year',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM sales '
            'WHERE substr("Date", 1, 4) = \'{dataset_year}\''
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["yearly sales", "ytd revenue", "year to date", "this year sales", "annual revenue"],
    },

    # ─── ORDERS ────────────────────────────────────────────────────────────
    {
        "id": "total_orders",
        "kpi_name": "Total Orders",
        "kpi_category": "orders",
        "description": "Distinct order count across all sales.",
        "formula_expression": 'COUNT(DISTINCT Order No)',
        "required_columns": ["Order No"],
        "sql_template": (
            'SELECT COUNT(DISTINCT "Order No") AS value FROM sales '
            'WHERE "Order No" IS NOT NULL AND "Order No" <> \'\''
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["orders", "order count", "number of orders", "total orders"],
    },
    {
        "id": "aov",
        "kpi_name": "Average Order Value (AOV)",
        "kpi_category": "orders",
        "description": "Total revenue divided by distinct order count.",
        "formula_expression": 'SUM(Total Amount) / COUNT(DISTINCT Order No)',
        "required_columns": ["Total Amount", "Order No"],
        "sql_template": (
            'SELECT CASE WHEN COUNT(DISTINCT "Order No") = 0 THEN 0 '
            'ELSE COALESCE(SUM("Total Amount"), 0) * 1.0 / COUNT(DISTINCT "Order No") '
            'END AS value FROM sales '
            'WHERE "Order No" IS NOT NULL AND "Order No" <> \'\''
        ),
        "aggregation_type": "ratio",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["aov", "average order value", "avg order", "mean order value", "order average"],
    },
    {
        "id": "orders_today",
        "kpi_name": "Orders on Latest Dataset Day",
        "kpi_category": "orders",
        "description": "Distinct order count on the dataset's most recent day.",
        "formula_expression": 'COUNT(DISTINCT Order No) WHERE Date = dataset_today',
        "required_columns": ["Date", "Order No"],
        "sql_template": (
            'SELECT COUNT(DISTINCT "Order No") AS value FROM sales '
            'WHERE "Date" = \'{dataset_today}\' '
            'AND "Order No" IS NOT NULL AND "Order No" <> \'\''
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["today orders", "todays orders", "orders for today", "last day orders"],
    },

    # ─── CUSTOMERS ─────────────────────────────────────────────────────────
    {
        "id": "total_customers",
        "kpi_name": "Total Customers",
        "kpi_category": "customers",
        "description": "Distinct customer (Party Name) count.",
        "formula_expression": 'COUNT(DISTINCT Party Name)',
        "required_columns": ["Party Name"],
        "sql_template": (
            'SELECT COUNT(DISTINCT "Party Name") AS value FROM sales '
            'WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\''
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["customers", "total customers", "unique customers", "customer count", "buyers", "clients"],
    },
    {
        "id": "new_customers_30d",
        "kpi_name": "New Customers (last 30 dataset days)",
        "kpi_category": "customers",
        "description": (
            "Customers whose FIRST purchase falls in the 30 days ending on "
            "dataset_today."
        ),
        "formula_expression": 'COUNT(DISTINCT Party Name WHERE MIN(Date) >= dataset_today - 29)',
        "required_columns": ["Date", "Party Name"],
        "sql_template": (
            'SELECT COUNT(*) AS value FROM ('
            '  SELECT "Party Name", MIN("Date") AS first_date FROM sales '
            '  WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\' '
            '  GROUP BY "Party Name" '
            '  HAVING first_date >= \'{dataset_last_30_start}\''
            ')'
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["new customers", "new buyers last month", "acquired customers", "recent customers"],
    },
    {
        "id": "returning_customers",
        "kpi_name": "Returning Customers",
        "kpi_category": "customers",
        "description": "Customers with more than one distinct order.",
        "formula_expression": 'COUNT(DISTINCT Party Name HAVING COUNT(DISTINCT Order No) > 1)',
        "required_columns": ["Party Name", "Order No"],
        "sql_template": (
            'SELECT COUNT(*) AS value FROM ('
            '  SELECT "Party Name" FROM sales '
            '  WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\' '
            '  GROUP BY "Party Name" '
            '  HAVING COUNT(DISTINCT "Order No") > 1'
            ')'
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["repeat customers", "returning buyers", "loyal customers", "repeat buyers"],
    },
    {
        "id": "customer_retention_rate",
        "kpi_name": "Customer Retention Rate",
        "kpi_category": "customers",
        "description": "Returning customers as a percentage of total customers.",
        "formula_expression": '(Returning Customers / Total Customers) * 100',
        "required_columns": ["Party Name", "Order No"],
        "sql_template": (
            'SELECT CASE WHEN total_c.n = 0 THEN 0 '
            'ELSE returning_c.n * 100.0 / total_c.n END AS value FROM '
            '(SELECT COUNT(*) AS n FROM ('
            '  SELECT "Party Name" FROM sales WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\' '
            '  GROUP BY "Party Name" HAVING COUNT(DISTINCT "Order No") > 1)) returning_c, '
            '(SELECT COUNT(DISTINCT "Party Name") AS n FROM sales '
            ' WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\') total_c'
        ),
        "aggregation_type": "percent",
        "output_type": "percent",
        "chart_supported": False,
        "aliases": ["retention", "customer retention", "retention rate", "repeat rate"],
    },
    {
        "id": "top_customer",
        "kpi_name": "Top Customer by Revenue",
        "kpi_category": "customers",
        "description": "Customer with the highest total spend.",
        "formula_expression": 'argmax(Party Name, SUM(Total Amount))',
        "required_columns": ["Party Name", "Total Amount"],
        "sql_template": (
            'SELECT "Party Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\' '
            'GROUP BY "Party Name" ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["best customer", "top customer", "biggest customer", "highest spending customer"],
    },
    {
        "id": "revenue_per_customer",
        "kpi_name": "Revenue per Customer",
        "kpi_category": "customers",
        "description": "Total revenue divided by total unique customers.",
        "formula_expression": 'SUM(Total Amount) / COUNT(DISTINCT Party Name)',
        "required_columns": ["Total Amount", "Party Name"],
        "sql_template": (
            'SELECT CASE WHEN cu.n = 0 THEN 0 ELSE r.s * 1.0 / cu.n END AS value FROM '
            '(SELECT COALESCE(SUM("Total Amount"), 0) AS s FROM sales) r, '
            '(SELECT COUNT(DISTINCT "Party Name") AS n FROM sales '
            ' WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\') cu'
        ),
        "aggregation_type": "ratio",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["revenue per customer", "arpu", "customer lifetime spend", "average customer value"],
    },

    # ─── PRODUCTS ──────────────────────────────────────────────────────────
    {
        "id": "total_products_sold",
        "kpi_name": "Total Distinct Products Sold",
        "kpi_category": "products",
        "description": "Number of distinct products that appear in sales.",
        "formula_expression": 'COUNT(DISTINCT Product Name)',
        "required_columns": ["Product Name"],
        "sql_template": (
            'SELECT COUNT(DISTINCT "Product Name") AS value FROM sales '
            'WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\''
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["distinct products", "product count", "unique products", "skus sold"],
    },
    {
        "id": "top_product",
        "kpi_name": "Top-Selling Product",
        "kpi_category": "products",
        "description": "Product with the highest total revenue contribution.",
        "formula_expression": 'argmax(Product Name, SUM(Total Amount))',
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT "Product Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\' '
            'GROUP BY "Product Name" ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["best selling product", "top product", "best product", "highest selling product", "bestseller"],
    },
    {
        "id": "bottom_product",
        "kpi_name": "Lowest-Selling Product",
        "kpi_category": "products",
        "description": "Product with the lowest total revenue contribution.",
        "formula_expression": 'argmin(Product Name, SUM(Total Amount))',
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT "Product Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\' '
            'GROUP BY "Product Name" ORDER BY value ASC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["worst product", "lowest selling product", "bottom product", "weakest product"],
    },

    # ─── PURCHASES / PROFIT ───────────────────────────────────────────────
    {
        "id": "total_purchases",
        "kpi_name": "Total Purchases (COGS)",
        "kpi_category": "profit",
        "description": "Sum of all purchase transactions — cost of goods sold proxy.",
        "formula_expression": "SUM(Total Amount FROM purchase)",
        "required_columns": ["Total Amount"],
        "sql_template": 'SELECT COALESCE(SUM("Total Amount"), 0) AS value FROM purchase',
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["total purchases", "cogs", "cost of goods sold", "total purchase cost", "purchase total"],
    },
    {
        "id": "gross_profit",
        "kpi_name": "Gross Profit",
        "kpi_category": "profit",
        "description": "Revenue minus cost of goods sold (qty * unit_cost).",
        # formula_expression is the textual hint passed to sqlWriter. Use
        # concept names; the resolver / METRIC DEFINITIONS block maps them
        # to the actual columns the uploaded workbook is using.
        "formula_expression": "SUM(revenue) - SUM(quantity * unit_cost)",
        "required_columns": [],
        # sql_template targets the uploaded data tables when present.
        # Legacy `sales` / `purchase` tables held stale/2025 test data and
        # the formula `SUM(sales.Total Amount) - SUM(purchase.Total Amount)`
        # treated full purchase spend as COGS — which is wrong (it ignores
        # qty sold). This now uses qty*unit_cost cost-of-goods.
        "sql_template": (
            'SELECT (SUM(t.net_sales) - SUM(t.quantity * i.unit_cost)) AS value '
            'FROM u_sales_transactions t '
            'LEFT JOIN u_inventory_master i ON i.sku_id = t.sku_id'
        ),
        "aggregation_type": "ratio",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["gross profit", "profit", "net profit", "total profit"],
    },
    {
        "id": "profit_margin",
        "kpi_name": "Profit Margin",
        "kpi_category": "profit",
        "description": "Gross profit expressed as a percent of revenue.",
        # Use concept names so the LLM resolves through METRIC DEFINITIONS.
        "formula_expression": "(SUM(revenue) - SUM(quantity * unit_cost)) * 100.0 / SUM(revenue)",
        "required_columns": [],
        "sql_template": (
            'SELECT CASE WHEN SUM(t.net_sales) = 0 THEN 0 '
            '            ELSE (SUM(t.net_sales) - SUM(t.quantity * i.unit_cost)) * 100.0 '
            '                   / SUM(t.net_sales) END AS value '
            'FROM u_sales_transactions t '
            'LEFT JOIN u_inventory_master i ON i.sku_id = t.sku_id'
        ),
        "aggregation_type": "percent",
        "output_type": "percent",
        "chart_supported": False,
        "aliases": ["profit margin", "margin", "gross margin", "net margin", "margin percent", "profitability"],
    },
    {
        "id": "cost_to_revenue_ratio",
        "kpi_name": "Cost-to-Revenue Ratio",
        "kpi_category": "profit",
        "description": "Cost of goods sold divided by sales revenue.",
        "formula_expression": "SUM(quantity * unit_cost) / SUM(revenue)",
        "required_columns": [],
        "sql_template": (
            'SELECT CASE WHEN SUM(t.net_sales) = 0 THEN 0 '
            '            ELSE SUM(t.quantity * i.unit_cost) * 1.0 / SUM(t.net_sales) END AS value '
            'FROM u_sales_transactions t '
            'LEFT JOIN u_inventory_master i ON i.sku_id = t.sku_id'
        ),
        "aggregation_type": "ratio",
        "output_type": "ratio",
        "chart_supported": False,
        "aliases": ["cost ratio", "expense ratio", "cost to revenue", "cogs ratio"],
    },

    # ─── PAYMENTS ──────────────────────────────────────────────────────────
    {
        "id": "outstanding_balance",
        "kpi_name": "Outstanding Balance",
        "kpi_category": "payments",
        "description": "Sum of unpaid balances across all sales.",
        "formula_expression": "SUM(Balance Due)",
        "required_columns": ["Balance Due"],
        "sql_template": 'SELECT COALESCE(SUM("Balance Due"), 0) AS value FROM sales',
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["outstanding", "outstanding balance", "balance due", "amount owed", "receivables", "pending payment"],
    },
    {
        "id": "cash_collected",
        "kpi_name": "Total Cash Collected",
        "kpi_category": "payments",
        "description": "Sum of received-paid amounts across all sales.",
        "formula_expression": "SUM(Received/Paid Amount)",
        "required_columns": ["Received/Paid Amount"],
        "sql_template": 'SELECT COALESCE(SUM("Received/Paid Amount"), 0) AS value FROM sales',
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["cash collected", "amount received", "collections", "payments received", "received amount"],
    },
    {
        "id": "collection_rate",
        "kpi_name": "Collection Rate",
        "kpi_category": "payments",
        "description": "Received-paid amount as a percent of total amount billed.",
        "formula_expression": "(SUM(Received/Paid Amount) / SUM(Total Amount)) * 100",
        "required_columns": ["Received/Paid Amount", "Total Amount"],
        "sql_template": (
            'SELECT CASE WHEN total_billed = 0 THEN 0 '
            'ELSE total_received * 100.0 / total_billed END AS value FROM '
            '(SELECT COALESCE(SUM("Received/Paid Amount"), 0) AS total_received, '
            '        COALESCE(SUM("Total Amount"), 0) AS total_billed FROM sales)'
        ),
        "aggregation_type": "percent",
        "output_type": "percent",
        "chart_supported": False,
        "aliases": ["collection rate", "payment collection", "received rate", "payment recovery"],
    },
    {
        "id": "payment_method_distribution",
        "kpi_name": "Revenue by Payment Method",
        "kpi_category": "payments",
        "description": "Breakdown of payment amounts across cash, UPI, credit, debit, HDFC.",
        "formula_expression": "SUM(Cash), SUM(BHARAT PAY), SUM(Credit), SUM(Debit Cards), SUM(HDFC BANK)",
        "required_columns": ["Cash", "BHARAT PAY", "Credit", "Debit Cards", "HDFC BANK"],
        "sql_template": (
            'SELECT '
            '  COALESCE(SUM("Cash"), 0)        AS cash, '
            '  COALESCE(SUM("BHARAT PAY"), 0)  AS upi, '
            '  COALESCE(SUM("Credit"), 0)      AS credit, '
            '  COALESCE(SUM("Debit Cards"), 0) AS debit, '
            '  COALESCE(SUM("HDFC BANK"), 0)   AS hdfc '
            'FROM sales'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["payment method", "payment mode breakdown", "revenue by payment", "payment split"],
    },

    # ─── TAX ───────────────────────────────────────────────────────────────
    {
        "id": "gst_customers",
        "kpi_name": "GST-Registered Customers",
        "kpi_category": "tax",
        "description": "Distinct customers whose GSTIN is recorded.",
        "formula_expression": "COUNT(DISTINCT Party Name WHERE GSTIN IS NOT NULL)",
        "required_columns": ["Party Name", "GSTIN"],
        "sql_template": (
            'SELECT COUNT(DISTINCT "Party Name") AS value FROM sales '
            'WHERE "GSTIN" IS NOT NULL AND "GSTIN" <> \'\''
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["gst customers", "gst registered", "b2b customers", "gstin customers"],
    },

    # ─── LOYALTY ───────────────────────────────────────────────────────────
    {
        "id": "total_loyalty_redeemed",
        "kpi_name": "Total Loyalty Redeemed",
        "kpi_category": "loyalty",
        "description": "Sum of loyalty points / value redeemed across all sales.",
        "formula_expression": "SUM(Loyalty Redeemed)",
        "required_columns": ["Loyalty Redeemed"],
        "sql_template": 'SELECT COALESCE(SUM("Loyalty Redeemed"), 0) AS value FROM sales',
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["loyalty redeemed", "loyalty points used", "rewards redeemed", "total loyalty"],
    },

    # ─── TIME ──────────────────────────────────────────────────────────────
    {
        "id": "peak_sales_day",
        "kpi_name": "Peak Sales Day",
        "kpi_category": "time",
        "description": "Calendar date with the highest revenue.",
        "formula_expression": 'argmax(Date, SUM(Total Amount))',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT "Date" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Date" IS NOT NULL '
            'GROUP BY "Date" ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["best day", "peak day", "highest sales day", "best sales date"],
    },
    {
        "id": "sales_by_month",
        "kpi_name": "Sales by Month",
        "kpi_category": "time",
        "description": "Monthly revenue distribution (chronological).",
        "formula_expression": 'SUM(Total Amount) GROUP BY year-month',
        "required_columns": ["Date", "Total Amount"],
        "sql_template": (
            'SELECT substr("Date", 1, 7) AS label, '
            '       COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Date" IS NOT NULL '
            'GROUP BY label ORDER BY label ASC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["monthly distribution", "sales by month", "monthly revenue", "month over month"],
    },

    # ─── BUSINESS HEALTH ──────────────────────────────────────────────────
    {
        "id": "repeat_purchase_rate",
        "kpi_name": "Repeat Purchase Rate",
        "kpi_category": "business_health",
        "description": "Share of customers who placed more than one order.",
        "formula_expression": '(Customers with >1 order) / (Total Customers) * 100',
        "required_columns": ["Party Name", "Order No"],
        "sql_template": (
            'SELECT CASE WHEN total_c.n = 0 THEN 0 '
            'ELSE returning_c.n * 100.0 / total_c.n END AS value FROM '
            '(SELECT COUNT(*) AS n FROM ('
            '  SELECT "Party Name" FROM sales WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\' '
            '  GROUP BY "Party Name" HAVING COUNT(DISTINCT "Order No") > 1)) returning_c, '
            '(SELECT COUNT(DISTINCT "Party Name") AS n FROM sales '
            ' WHERE "Party Name" IS NOT NULL AND "Party Name" <> \'\') total_c'
        ),
        "aggregation_type": "percent",
        "output_type": "percent",
        "chart_supported": False,
        "aliases": ["repeat rate", "repeat purchase", "repeat buyer rate"],
    },

    # ─── HIERARCHY-AWARE (product_master JOIN product_hierarchy) ──────────
    {
        "id": "sales_by_category",
        "kpi_name": "Sales by Category",
        "kpi_category": "hierarchy",
        "description": "Revenue rolled up by product category (Shoes / Slippers / Sandals / Other).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY category",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(cat.name, \'Other\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy cat ON cat.id = pm.category_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by category", "category sales", "category breakdown", "revenue by category"],
    },
    {
        "id": "top_category",
        "kpi_name": "Top Category by Revenue",
        "kpi_category": "hierarchy",
        "description": "Product category with the highest total revenue.",
        "formula_expression": "argmax(category, SUM(Total Amount))",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(cat.name, \'Other\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy cat ON cat.id = pm.category_id '
            'GROUP BY label ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["top category", "best category", "highest selling category", "leading category"],
    },
    {
        "id": "sales_by_subcategory",
        "kpi_name": "Sales by Subcategory",
        "kpi_category": "hierarchy",
        "description": "Revenue rolled up by product subcategory (Sports Shoes, Casual Slippers, etc.).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY subcategory",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(sub.name, \'Other\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy sub ON sub.id = pm.subcategory_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by subcategory", "subcategory breakdown", "subcategory sales"],
    },
    {
        "id": "top_brand",
        "kpi_name": "Top Brand by Revenue",
        "kpi_category": "hierarchy",
        "description": "Brand contributing the highest revenue among classified products.",
        "formula_expression": "argmax(brand, SUM(Total Amount))",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT br.name AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy br ON br.id = pm.brand_id '
            'GROUP BY br.name ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["top brand", "best brand", "leading brand", "highest selling brand"],
    },
    {
        "id": "sales_by_brand",
        "kpi_name": "Sales by Brand",
        "kpi_category": "hierarchy",
        "description": "Revenue distribution across detected brands.",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY brand",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT br.name AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy br ON br.id = pm.brand_id '
            'GROUP BY br.name ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by brand", "brand sales", "brand breakdown", "revenue by brand"],
    },

    # ─── PRODUCT HIERARCHY v2 (Need → Family → Class → Line → Type → SKU) ─
    # Enterprise drilldown KPIs. They JOIN through product_sku_master, so
    # they coexist with the v1 hierarchy KPIs without conflict.
    {
        "id": "sales_by_need",
        "kpi_name": "Sales by Need (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue rolled up by the highest level of the enterprise hierarchy (Need).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY need",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(n.name, \'Unclassified\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy_v2 n ON n.id = pm.need_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by need", "revenue by need", "need breakdown"],
    },
    {
        "id": "sales_by_family",
        "kpi_name": "Sales by Family (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue grouped by merchandise family (e.g. Footwear).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY family",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(f.name, \'Unclassified\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy_v2 f ON f.id = pm.family_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by family", "revenue by family", "family breakdown"],
    },
    {
        "id": "sales_by_class_v2",
        "kpi_name": "Sales by Class (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue grouped by merchandise class (Athletic / Open / Casual ...).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY class",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(c.name, \'Unclassified\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy_v2 c ON c.id = pm.class_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by class", "class breakdown", "revenue by class"],
    },
    {
        "id": "sales_by_line",
        "kpi_name": "Sales by Line (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue grouped by product line (Running, Casual, Indoor, ...).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY line",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(l.name, \'Unclassified\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy_v2 l ON l.id = pm.line_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by line", "line breakdown", "revenue by line"],
    },
    {
        "id": "sales_by_type_v2",
        "kpi_name": "Sales by Type (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue grouped by lowest-classification level (Type).",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY type",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT COALESCE(t.name, \'Unclassified\') AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'LEFT JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'LEFT JOIN product_hierarchy_v2 t ON t.id = pm.type_id '
            'GROUP BY label ORDER BY value DESC'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["sales by type", "type breakdown", "revenue by type"],
    },
    {
        "id": "top_sku",
        "kpi_name": "Top SKU by Revenue (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Single SKU contributing the highest revenue.",
        "formula_expression": "argmax(sku_code, SUM(Total Amount))",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT pm.sku_code AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'GROUP BY pm.sku_code ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["top sku", "best sku", "highest sku revenue", "best selling sku"],
    },
    {
        "id": "top_need",
        "kpi_name": "Top Need by Revenue (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Need-level category that contributes the most revenue.",
        "formula_expression": "argmax(need, SUM(Total Amount))",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT n.name AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 n ON n.id = pm.need_id '
            'GROUP BY n.name ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["top need", "leading need", "highest need revenue"],
    },
    {
        "id": "top_family_v2",
        "kpi_name": "Top Family by Revenue (v2)",
        "kpi_category": "hierarchy_v2",
        "description": "Merchandise family with highest total revenue.",
        "formula_expression": "argmax(family, SUM(Total Amount))",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT f.name AS label, '
            '       COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 f ON f.id = pm.family_id '
            'GROUP BY f.name ORDER BY value DESC LIMIT 1'
        ),
        "aggregation_type": "sum",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["top family", "leading family", "best family"],
    },

    # ─── WINDOWED TOP-N (hierarchy × time × top combinations) ─────────────
    # These are the KPIs that answer questions like:
    #   "Top performing products this week"
    #   "Best selling Men products"
    #   "Top Women footwear"
    # The window tokens are dataset-relative — they ALWAYS contain at
    # least dataset_today, so the result set is never empty when any
    # data is uploaded.
    {
        "id": "top_products_last_7_days",
        "kpi_name": "Top Products (Last 7 Dataset Days)",
        "kpi_category": "products",
        "description": "Top 5 products by revenue across the most recent 7 dataset days.",
        "formula_expression": 'argmax(Product Name, SUM(Total Amount)) over last 7 dataset days',
        "required_columns": ["Product Name", "Total Amount", "Date"],
        "sql_template": (
            'SELECT "Product Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\' '
            'AND "Date" BETWEEN \'{dataset_last_7_start}\' AND \'{dataset_today}\' '
            'GROUP BY "Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top performing products this week", "top products this week",
            "best selling products this week", "best products this week",
            "best items this week", "top performing items this week",
            "top products last week", "top performing this week",
            "top performing products", "top products last 7 days",
        ],
    },
    {
        "id": "top_products_last_30_days",
        "kpi_name": "Top Products (Last 30 Dataset Days)",
        "kpi_category": "products",
        "description": "Top 5 products by revenue across the most recent 30 dataset days.",
        "formula_expression": 'argmax(Product Name, SUM(Total Amount)) over last 30 dataset days',
        "required_columns": ["Product Name", "Total Amount", "Date"],
        "sql_template": (
            'SELECT "Product Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\' '
            'AND "Date" BETWEEN \'{dataset_last_30_start}\' AND \'{dataset_today}\' '
            'GROUP BY "Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top products last 30 days", "top products this month",
            "best selling products this month", "best products last 30 days",
            "monthly top products", "top items this month",
        ],
    },
    {
        "id": "top_products_today",
        "kpi_name": "Top Products (Latest Dataset Day)",
        "kpi_category": "products",
        "description": "Top 5 products by revenue on the most recent dataset day.",
        "formula_expression": "argmax(Product Name, SUM(Total Amount)) for dataset_today",
        "required_columns": ["Product Name", "Total Amount", "Date"],
        "sql_template": (
            'SELECT "Product Name" AS label, COALESCE(SUM("Total Amount"), 0) AS value '
            'FROM sales WHERE "Product Name" IS NOT NULL AND "Product Name" <> \'\' '
            'AND "Date" = \'{dataset_today}\' '
            'GROUP BY "Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top products today", "best selling items today", "best items today",
            "top items today", "today top products", "top products of the day",
            "best products today",
        ],
    },
    {
        "id": "top_men_products",
        "kpi_name": "Top Men's Products",
        "kpi_category": "hierarchy_v2",
        "description": "Top 5 Men's products by revenue (all-time).",
        "formula_expression": "argmax(Product Name, SUM(Total Amount)) WHERE class = 'Men'",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT s."Product Name" AS label, COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 c ON c.id = pm.class_id AND c.name = \'Men\' '
            'GROUP BY s."Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top men products", "best men products", "best selling men products",
            "top men footwear", "best men footwear", "top performing men products",
            "best mens products", "top mens items", "top selling mens",
        ],
    },
    {
        "id": "top_women_products",
        "kpi_name": "Top Women's Products",
        "kpi_category": "hierarchy_v2",
        "description": "Top 5 Women's products by revenue (all-time).",
        "formula_expression": "argmax(Product Name, SUM(Total Amount)) WHERE class = 'Women'",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT s."Product Name" AS label, COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 c ON c.id = pm.class_id AND c.name = \'Women\' '
            'GROUP BY s."Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top women products", "best women products", "best selling women products",
            "top women footwear", "best women footwear", "top performing women products",
            "best womens products", "top womens items", "top selling womens",
            "top ladies products",
        ],
    },
    {
        "id": "top_children_products",
        "kpi_name": "Top Children's Products",
        "kpi_category": "hierarchy_v2",
        "description": "Top 5 Children's products by revenue (all-time).",
        "formula_expression": "argmax(Product Name, SUM(Total Amount)) WHERE class = 'Children'",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT s."Product Name" AS label, COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 c ON c.id = pm.class_id AND c.name = \'Children\' '
            'GROUP BY s."Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top children products", "best children products", "best selling kids products",
            "top kids products", "top kids footwear", "best children footwear",
            "top performing kids products", "best kids items",
        ],
    },

    # ─── INVENTORY LIST KPIs (returning SKU lists, not just counts) ────────
    # Status thresholds (dataset-relative, last 30 days):
    #   avg_daily = SUM(qty sold last 30d) / 30
    #   low         = stock_qty < avg_daily * 7        (under one week of cover)
    #   overstocked = stock_qty > avg_daily * 60       (over 60 days of cover)
    #   dead        = no sales in last 30 days
    # Source tables: u_inventory_master (real uploaded stock) joined with
    # u_sales_transactions (real sales velocity). Previously these queried
    # sku_inventory, which only had 7 synthetic rows derived from legacy
    # `sales` table — invisible to the uploaded workbook.
    {
        "id": "low_stock_items",
        "kpi_name": "Low-Stock Items",
        "kpi_category": "inventory",
        "description": "Top 10 SKUs below one week of cover, ordered by urgency.",
        "formula_expression": "list SKUs WHERE stock_qty < (avg_daily_sales_30d * 7) ORDER BY stock_qty ASC",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "vel AS ( "
            "  SELECT t.sku_id, SUM(t.quantity)/30.0 AS avg_daily "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "  GROUP BY t.sku_id "
            ") "
            "SELECT i.final_product AS label, "
            "       i.stock_qty AS value, "
            "       i.sku_id, ROUND(COALESCE(v.avg_daily,0),2) AS avg_daily_sales "
            "FROM u_inventory_master i "
            "LEFT JOIN vel v ON v.sku_id = i.sku_id "
            "WHERE COALESCE(v.avg_daily, 0) > 0 "
            "  AND i.stock_qty < COALESCE(v.avg_daily, 0) * 7 "
            "ORDER BY i.stock_qty ASC LIMIT 10"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "low stock items", "low stock products", "which items are low",
            "items needing restock", "products needing restock",
            "items running low", "restock list", "items low in stock",
            "which products are low in stock",
        ],
    },
    {
        "id": "dead_stock_items",
        "kpi_name": "Dead-Stock Items",
        "kpi_category": "inventory",
        "description": "Top 10 SKUs flagged as dead stock (no sales in last 30 dataset days).",
        "formula_expression": "list SKUs WHERE no sales in last 30 days ORDER BY stock_qty DESC",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "sold AS ( "
            "  SELECT DISTINCT t.sku_id "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            ") "
            "SELECT i.final_product AS label, "
            "       i.stock_qty AS value, "
            "       i.sku_id "
            "FROM u_inventory_master i "
            "WHERE i.sku_id NOT IN (SELECT sku_id FROM sold) "
            "ORDER BY i.stock_qty DESC LIMIT 10"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "dead stock items", "non moving products", "items not moving",
            "which products are not selling", "stuck products",
            "items not selling", "stale products",
        ],
    },

    # ─── FORECAST-DRIVEN TOP KPI ──────────────────────────────────────────
    {
        "id": "top_skus_forecast_7d",
        "kpi_name": "Top Projected SKUs (Next 7 Days)",
        "kpi_category": "forecast",
        "description": "Top 5 SKUs by total projected revenue across the next 7 forecast days.",
        "formula_expression": "argmax(sku_code, SUM(forecast_revenue)) for next 7 forecast days",
        "required_columns": [],
        "sql_template": (
            "SELECT f.sku_code AS label, "
            "       COALESCE(SUM(f.forecast_revenue), 0) AS value "
            "FROM sku_forecast f "
            "WHERE f.forecast_date IN ("
            "  SELECT DISTINCT forecast_date FROM sku_forecast "
            "  ORDER BY forecast_date LIMIT 7"
            ") "
            "GROUP BY f.sku_code ORDER BY value DESC LIMIT 5"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top forecast skus", "top projected skus", "top skus next week",
            "best forecast skus", "highest projected products",
            "top projected products next week",
        ],
    },

    # ─── INVENTORY (derived from uploaded u_inventory_master + sales) ──────
    # Same status thresholds as the list KPIs above:
    #   low         = stock_qty < avg_daily_sales_30d * 7
    #   overstocked = stock_qty > avg_daily_sales_30d * 60
    #   dead        = no sales in last 30 dataset days
    {
        "id": "low_stock_skus",
        "kpi_name": "Low-Stock SKUs",
        "kpi_category": "inventory",
        "description": "Number of SKUs with less than one week of cover at current sales velocity.",
        "formula_expression": "COUNT(SKUs WHERE stock_qty < avg_daily_sales_30d * 7)",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "vel AS ( "
            "  SELECT t.sku_id, SUM(t.quantity)/30.0 AS avg_daily "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "  GROUP BY t.sku_id "
            ") "
            "SELECT COUNT(*) AS value "
            "FROM u_inventory_master i "
            "LEFT JOIN vel v ON v.sku_id = i.sku_id "
            "WHERE COALESCE(v.avg_daily, 0) > 0 "
            "  AND i.stock_qty < COALESCE(v.avg_daily, 0) * 7"
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["low stock", "low stock skus", "skus running low",
                    "stock alerts", "items needing reorder", "reorder list"],
    },
    {
        "id": "overstocked_skus",
        "kpi_name": "Overstocked SKUs",
        "kpi_category": "inventory",
        "description": "Number of SKUs with more than 60 days of cover at current sales velocity.",
        "formula_expression": "COUNT(SKUs WHERE stock_qty > avg_daily_sales_30d * 60)",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "vel AS ( "
            "  SELECT t.sku_id, SUM(t.quantity)/30.0 AS avg_daily "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "  GROUP BY t.sku_id "
            ") "
            "SELECT COUNT(*) AS value "
            "FROM u_inventory_master i "
            "LEFT JOIN vel v ON v.sku_id = i.sku_id "
            "WHERE COALESCE(v.avg_daily, 0) > 0 "
            "  AND i.stock_qty > COALESCE(v.avg_daily, 0) * 60"
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["overstocked", "overstock", "excess inventory", "overstocked items"],
    },
    {
        "id": "dead_stock_skus",
        "kpi_name": "Dead-Stock SKUs",
        "kpi_category": "inventory",
        "description": "Number of SKUs with no sales in the last 30 dataset days.",
        "formula_expression": "COUNT(SKUs WHERE no sales in last 30 days)",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "sold AS ( "
            "  SELECT DISTINCT t.sku_id "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            ") "
            "SELECT COUNT(*) AS value "
            "FROM u_inventory_master i "
            "WHERE i.sku_id NOT IN (SELECT sku_id FROM sold)"
        ),
        "aggregation_type": "count",
        "output_type": "count",
        "chart_supported": False,
        "aliases": ["dead stock", "non moving items", "items not moving",
                    "stale inventory", "stuck inventory"],
    },
    {
        "id": "inventory_status_breakdown",
        "kpi_name": "Inventory Health Breakdown",
        "kpi_category": "inventory",
        "description": "SKU counts grouped by inventory health status (low/overstocked/dead/ok).",
        "formula_expression": "GROUP BY (low | overstocked | dead | ok) status derived from stock_qty + sales velocity",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "vel AS ( "
            "  SELECT t.sku_id, SUM(t.quantity)/30.0 AS avg_daily "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "  GROUP BY t.sku_id "
            ") "
            "SELECT CASE "
            "         WHEN COALESCE(v.avg_daily, 0) = 0 THEN 'dead' "
            "         WHEN i.stock_qty < v.avg_daily * 7  THEN 'low' "
            "         WHEN i.stock_qty > v.avg_daily * 60 THEN 'overstocked' "
            "         ELSE 'ok' "
            "       END AS label, "
            "       COUNT(*) AS value "
            "FROM u_inventory_master i "
            "LEFT JOIN vel v ON v.sku_id = i.sku_id "
            "GROUP BY label ORDER BY value DESC"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": ["inventory health", "stock health", "inventory breakdown",
                    "inventory by status", "stock status"],
    },

    # ─── FORECAST (14-day forward projection per SKU) ────────────────────
    {
        "id": "forecast_revenue_next_7d",
        "kpi_name": "Projected Revenue (Next 7 Days)",
        "kpi_category": "forecast",
        "description": "Total projected revenue for the next 7 days based on trailing-30-day SKU velocity.",
        "formula_expression": "SUM(sku_forecast.forecast_revenue) for next 7 days",
        "required_columns": [],
        "sql_template": (
            "SELECT COALESCE(SUM(forecast_revenue), 0) AS value "
            "FROM sku_forecast "
            "WHERE forecast_date IN ("
            "  SELECT DISTINCT forecast_date FROM sku_forecast "
            "  ORDER BY forecast_date LIMIT 7"
            ")"
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["next 7 days forecast", "forecast next 7 days",
                    "projected revenue next week", "next week forecast",
                    "projection next 7"],
    },
    {
        "id": "forecast_revenue_next_14d",
        "kpi_name": "Projected Revenue (Next 14 Days)",
        "kpi_category": "forecast",
        "description": "Total projected revenue over the full 14-day forecast horizon.",
        "formula_expression": "SUM(sku_forecast.forecast_revenue) over 14-day horizon",
        "required_columns": [],
        "sql_template": (
            "SELECT COALESCE(SUM(forecast_revenue), 0) AS value FROM sku_forecast"
        ),
        "aggregation_type": "sum",
        "output_type": "currency",
        "chart_supported": False,
        "aliases": ["next 14 days forecast", "forecast next 14 days",
                    "two week projection", "fortnight forecast",
                    "next month forecast"],
    },

    # ─── PER-PRODUCT PROFIT / MARGIN (uses product_cost_master) ────────────
    {
        "id": "highest_profit_products",
        "kpi_name": "Highest Profit Products",
        "kpi_category": "profit",
        "description": "Top 5 products by total profit = (revenue - unit_cost * quantity).",
        "formula_expression": "SUM(Total Amount - unit_cost * Quantity) GROUP BY product",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT s."Product Name" AS label, '
            '       COALESCE(SUM(s."Total Amount" - c.unit_cost * COALESCE(s."Quantity", 1)), 0) AS value '
            'FROM sales s '
            'JOIN product_cost_master c ON c.product_name = s."Product Name" '
            'GROUP BY s."Product Name" ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "highest profit products", "top profit products", "best profit products",
            "which products generate highest profit", "most profitable products",
            "top earning products",
        ],
    },
    {
        "id": "highest_margin_products",
        "kpi_name": "Highest Margin Products",
        "kpi_category": "profit",
        "description": "Top 10 products ranked by realized margin percentage.",
        # Previously: '(avg_sale_price - unit_cost) / avg_sale_price * 100'.
        # That averaged per-unit ratios — wrong. Correct margin is computed
        # on SUM totals across all units sold, not per-row averages.
        "formula_expression": (
            "(SUM(revenue) - SUM(quantity * unit_cost)) * 100.0 / SUM(revenue) "
            "GROUP BY product"
        ),
        "required_columns": [],
        "sql_template": (
            'SELECT t.final_product AS label, '
            '       (SUM(t.net_sales) - SUM(t.quantity * i.unit_cost)) * 100.0 '
            '         / NULLIF(SUM(t.net_sales), 0) AS value '
            'FROM u_sales_transactions t '
            'LEFT JOIN u_inventory_master i ON i.sku_id = t.sku_id '
            'GROUP BY t.final_product '
            'HAVING SUM(t.net_sales) > 0 '
            'ORDER BY value DESC LIMIT 10'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "highest margin products", "top high margin products", "top margin products",
            "best margin products", "which products have best margins",
            "high margin items",
        ],
    },
    {
        "id": "loss_making_products",
        "kpi_name": "Loss-Making Products",
        "kpi_category": "profit",
        "description": "Products where total profit is negative (rare, but lists them).",
        "formula_expression": "SUM(Total Amount - unit_cost * Quantity) HAVING < 0",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT s."Product Name" AS label, '
            '       COALESCE(SUM(s."Total Amount" - c.unit_cost * COALESCE(s."Quantity", 1)), 0) AS value '
            'FROM sales s '
            'JOIN product_cost_master c ON c.product_name = s."Product Name" '
            'GROUP BY s."Product Name" HAVING value < 0 ORDER BY value ASC LIMIT 10'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "loss making products", "products making losses", "which products are making losses",
            "loss products", "unprofitable products", "negative profit products",
        ],
    },

    # ─── DECLINING / GROWING (comparison: last 7d vs prior 7d) ─────────────
    {
        "id": "declining_products",
        "kpi_name": "Declining Products",
        "kpi_category": "products",
        "description": "Top 5 products with the steepest revenue drop in the last 7 days vs the 7 days before.",
        "formula_expression": "SUM(last_7) - SUM(prior_7) ASC LIMIT 5",
        "required_columns": ["Product Name", "Total Amount", "Date"],
        "sql_template": (
            'SELECT cur."Product Name" AS label, '
            '       COALESCE(cur.rev, 0) - COALESCE(prv.rev, 0) AS value '
            'FROM ('
            '  SELECT "Product Name", SUM("Total Amount") AS rev FROM sales '
            '  WHERE "Date" BETWEEN \'{dataset_last_7_start}\' AND \'{dataset_today}\' '
            '  GROUP BY "Product Name"'
            ') cur LEFT JOIN ('
            '  SELECT "Product Name", SUM("Total Amount") AS rev FROM sales '
            '  WHERE "Date" BETWEEN date(\'{dataset_last_7_start}\', \'-7 day\') '
            '                   AND date(\'{dataset_today}\',         \'-7 day\') '
            '  GROUP BY "Product Name"'
            ') prv ON prv."Product Name" = cur."Product Name" '
            'WHERE prv.rev IS NOT NULL '
            'ORDER BY value ASC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "declining products", "which products are declining",
            "products declining in sales", "products losing sales",
            "shrinking products", "weakening products",
        ],
    },
    {
        "id": "growth_driving_products",
        "kpi_name": "Growth-Driving Products",
        "kpi_category": "products",
        "description": "Top 5 products with the biggest revenue gain last 7 days vs the prior 7 days.",
        "formula_expression": "SUM(last_7) - SUM(prior_7) DESC LIMIT 5",
        "required_columns": ["Product Name", "Total Amount", "Date"],
        "sql_template": (
            'SELECT cur."Product Name" AS label, '
            '       COALESCE(cur.rev, 0) - COALESCE(prv.rev, 0) AS value '
            'FROM ('
            '  SELECT "Product Name", SUM("Total Amount") AS rev FROM sales '
            '  WHERE "Date" BETWEEN \'{dataset_last_7_start}\' AND \'{dataset_today}\' '
            '  GROUP BY "Product Name"'
            ') cur LEFT JOIN ('
            '  SELECT "Product Name", SUM("Total Amount") AS rev FROM sales '
            '  WHERE "Date" BETWEEN date(\'{dataset_last_7_start}\', \'-7 day\') '
            '                   AND date(\'{dataset_today}\',         \'-7 day\') '
            '  GROUP BY "Product Name"'
            ') prv ON prv."Product Name" = cur."Product Name" '
            'ORDER BY value DESC LIMIT 5'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "growth driving products", "products driving growth",
            "which products are driving growth", "growing products",
            "growth leaders", "rising products",
        ],
    },

    # ─── INVENTORY ADVANCED ────────────────────────────────────────────────
    {
        "id": "fast_moving_skus",
        "kpi_name": "Fast-Moving SKUs",
        "kpi_category": "inventory",
        "description": "Top 10 SKUs by daily sales velocity (qty per day, last 30 days).",
        "formula_expression": "ORDER BY (SUM(quantity last 30d) / 30) DESC LIMIT 10",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions) "
            "SELECT i.final_product AS label, "
            "       ROUND(SUM(t.quantity)/30.0, 2) AS value, "
            "       i.sku_id "
            "FROM u_sales_transactions t, ds "
            "JOIN u_inventory_master i ON i.sku_id = t.sku_id "
            "WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "GROUP BY i.sku_id, i.final_product "
            "HAVING SUM(t.quantity) > 0 "
            "ORDER BY value DESC LIMIT 10"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "fast moving products", "fast moving skus", "which products are fast moving",
            "best moving products", "top moving products", "high velocity products",
        ],
    },
    {
        "id": "business_risk_skus",
        "kpi_name": "Inventory Risk SKUs",
        "kpi_category": "inventory",
        "description": "Top 10 SKUs at highest inventory risk (dead or overstocked), ranked by on-hand quantity.",
        "formula_expression": "list SKUs WHERE status IN ('dead','overstocked') ORDER BY stock_qty DESC",
        "required_columns": [],
        "sql_template": (
            "WITH ds AS (SELECT MAX(date(transaction_date)) AS d FROM u_sales_transactions), "
            "vel AS ( "
            "  SELECT t.sku_id, SUM(t.quantity)/30.0 AS avg_daily "
            "  FROM u_sales_transactions t, ds "
            "  WHERE date(t.transaction_date) >= date(ds.d, '-30 days') "
            "  GROUP BY t.sku_id "
            ") "
            "SELECT i.final_product AS label, "
            "       i.stock_qty AS value, "
            "       CASE "
            "         WHEN COALESCE(v.avg_daily, 0) = 0 THEN 'dead' "
            "         ELSE 'overstocked' "
            "       END AS status "
            "FROM u_inventory_master i "
            "LEFT JOIN vel v ON v.sku_id = i.sku_id "
            "WHERE COALESCE(v.avg_daily, 0) = 0 "
            "   OR (v.avg_daily > 0 AND i.stock_qty > v.avg_daily * 60) "
            "ORDER BY i.stock_qty DESC LIMIT 10"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "inventory risk", "skus at risk", "which inventory is risky",
            "risky inventory", "high risk skus", "which skus have highest inventory risk",
            "top business risks",
        ],
    },

    # ─── SUB-HIERARCHY (top lines within a class) ─────────────────────────
    {
        "id": "top_women_lines",
        "kpi_name": "Top Women's Lines",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue per line within the Women class, top 7.",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY line WHERE class='Women'",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT l.name AS label, COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 c ON c.id = pm.class_id AND c.name = \'Women\' '
            'JOIN product_hierarchy_v2 l ON l.id = pm.line_id '
            'GROUP BY l.name ORDER BY value DESC LIMIT 7'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top women lines", "which women categories perform best",
            "best women categories", "women category performance",
            "women lines",
        ],
    },
    {
        "id": "top_men_lines",
        "kpi_name": "Top Men's Lines",
        "kpi_category": "hierarchy_v2",
        "description": "Revenue per line within the Men class.",
        "formula_expression": "SUM(sales.Total Amount) GROUP BY line WHERE class='Men'",
        "required_columns": ["Product Name", "Total Amount"],
        "sql_template": (
            'SELECT l.name AS label, COALESCE(SUM(s."Total Amount"), 0) AS value '
            'FROM sales s '
            'JOIN product_sku_master pm ON pm.product_name = s."Product Name" '
            'JOIN product_hierarchy_v2 c ON c.id = pm.class_id AND c.name = \'Men\' '
            'JOIN product_hierarchy_v2 l ON l.id = pm.line_id '
            'GROUP BY l.name ORDER BY value DESC LIMIT 8'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": True,
        "aliases": [
            "top men lines", "best men categories", "which men categories perform best",
            "men category performance", "men lines", "men department breakdown",
        ],
    },

    # ─── EXECUTIVE SUMMARY (multi-metric digest) ──────────────────────────
    {
        "id": "executive_summary",
        "kpi_name": "Executive Business Summary",
        "kpi_category": "dashboard",
        "description": "Headline metrics: total revenue, last-7-day revenue, low-stock count, projected next 7 days.",
        "formula_expression": "Combined snapshot of revenue + inventory + forecast",
        "required_columns": [],
        "sql_template": (
            "SELECT "
            "  (SELECT COALESCE(SUM(\"Total Amount\"), 0) FROM sales) AS total_revenue, "
            "  (SELECT COALESCE(SUM(\"Total Amount\"), 0) FROM sales "
            "    WHERE \"Date\" BETWEEN '{dataset_last_7_start}' AND '{dataset_today}') AS last_7_revenue, "
            "  (SELECT COUNT(*) FROM sku_inventory WHERE status = 'low') AS low_stock_count, "
            "  (SELECT COUNT(*) FROM sku_inventory WHERE status = 'dead') AS dead_stock_count, "
            "  (SELECT COALESCE(SUM(forecast_revenue), 0) FROM sku_forecast "
            "    WHERE forecast_date IN (SELECT DISTINCT forecast_date FROM sku_forecast "
            "                            ORDER BY forecast_date LIMIT 7)) AS projected_next_7"
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": False,
        "aliases": [
            "executive summary", "business overview", "executive business summary",
            "show kpi summary", "kpi summary", "business performance overview",
            "show business performance overview", "headline summary",
        ],
    },

    # ─── DASHBOARD QUICK STATS ────────────────────────────────────────────
    {
        "id": "dashboard_kpis",
        "kpi_name": "Dashboard Summary",
        "kpi_category": "dashboard",
        "description": "Total revenue, order count, and unique-customer count.",
        "formula_expression": "SUM(Total Amount), COUNT(DISTINCT Order No), COUNT(DISTINCT Party Name)",
        "required_columns": ["Total Amount", "Order No", "Party Name"],
        "sql_template": (
            'SELECT '
            '  COALESCE(SUM("Total Amount"), 0) AS total_sales, '
            '  COUNT(DISTINCT "Order No")       AS orders, '
            '  COUNT(DISTINCT "Party Name")     AS customers '
            'FROM sales'
        ),
        "aggregation_type": "distribution",
        "output_type": "list",
        "chart_supported": False,
        "aliases": ["dashboard summary", "key metrics", "summary stats", "headline kpis"],
    },
]


# ===========================================================================
# DATA RE-POINTING (2026-05-20)
# The shipped DEFAULT_KPIS above all target the legacy 2-table sales /
# purchase schema. Real uploads land in the dynamic workbook tables:
#   u_sales_transactions  -> net_sales, quantity, sku_id, brand, category,
#                            payment_mode, transaction_date, final_product
#   u_inventory_master    -> sku_id, unit_cost (joined for COGS / margin)
# The block below re-points every KPI that maps cleanly to that data and
# disables the ones that cannot be computed on it (no customer identity,
# loyalty, GSTIN or per-bank columns; dataset-relative time windows would
# need the time engine re-pointed first).
# ===========================================================================

_REPOINTED_SQL: dict[str, str] = {
    "total_revenue":
        "SELECT COALESCE(SUM(net_sales),0) AS value FROM u_sales_transactions",
    "total_sales_count":
        "SELECT COUNT(*) AS value FROM u_sales_transactions",
    "average_sale_value":
        "SELECT COALESCE(AVG(net_sales),0) AS value FROM u_sales_transactions",
    "total_orders":
        "SELECT COUNT(DISTINCT invoice_no) AS value FROM u_sales_transactions "
        "WHERE invoice_no IS NOT NULL AND invoice_no <> ''",
    "aov":
        "SELECT CASE WHEN COUNT(DISTINCT invoice_no)=0 THEN 0 ELSE "
        "COALESCE(SUM(net_sales),0)*1.0/COUNT(DISTINCT invoice_no) END AS value "
        "FROM u_sales_transactions WHERE invoice_no IS NOT NULL AND invoice_no <> ''",
    "total_products_sold":
        "SELECT COUNT(DISTINCT sku_id) AS value FROM u_sales_transactions",
    "total_purchases":
        "SELECT COALESCE(SUM(s.quantity*i.unit_cost),0) AS value "
        "FROM u_sales_transactions s JOIN u_inventory_master i ON s.sku_id=i.sku_id",
    "gross_profit":
        "SELECT (COALESCE(SUM(s.net_sales),0)-COALESCE(SUM(s.quantity*i.unit_cost),0)) "
        "AS value FROM u_sales_transactions s "
        "JOIN u_inventory_master i ON s.sku_id=i.sku_id",
    "profit_margin":
        "SELECT CASE WHEN COALESCE(SUM(s.net_sales),0)=0 THEN 0 ELSE "
        "(SUM(s.net_sales)-SUM(s.quantity*i.unit_cost))*100.0/SUM(s.net_sales) END "
        "AS value FROM u_sales_transactions s "
        "JOIN u_inventory_master i ON s.sku_id=i.sku_id",
    "cost_to_revenue_ratio":
        "SELECT CASE WHEN COALESCE(SUM(s.net_sales),0)=0 THEN 0 ELSE "
        "SUM(s.quantity*i.unit_cost)*1.0/SUM(s.net_sales) END AS value "
        "FROM u_sales_transactions s JOIN u_inventory_master i ON s.sku_id=i.sku_id",
    "top_product":
        "SELECT final_product AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions GROUP BY final_product ORDER BY value DESC LIMIT 1",
    "bottom_product":
        "SELECT final_product AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions GROUP BY final_product ORDER BY value ASC LIMIT 1",
    "highest_margin_products":
        "SELECT s.final_product AS label, "
        "ROUND((SUM(s.net_sales)-SUM(s.quantity*i.unit_cost))*100.0/SUM(s.net_sales),2) "
        "AS value FROM u_sales_transactions s "
        "JOIN u_inventory_master i ON s.sku_id=i.sku_id "
        "GROUP BY s.final_product HAVING SUM(s.net_sales)>0 ORDER BY value DESC LIMIT 10",
    "highest_profit_products":
        "SELECT s.final_product AS label, "
        "ROUND(SUM(s.net_sales)-SUM(s.quantity*i.unit_cost),2) AS value "
        "FROM u_sales_transactions s JOIN u_inventory_master i ON s.sku_id=i.sku_id "
        "GROUP BY s.final_product ORDER BY value DESC LIMIT 10",
    "loss_making_products":
        "SELECT s.final_product AS label, "
        "ROUND(SUM(s.net_sales)-SUM(s.quantity*i.unit_cost),2) AS value "
        "FROM u_sales_transactions s JOIN u_inventory_master i ON s.sku_id=i.sku_id "
        "GROUP BY s.final_product "
        "HAVING (SUM(s.net_sales)-SUM(s.quantity*i.unit_cost))<0 "
        "ORDER BY value ASC LIMIT 10",
    "sales_by_brand":
        "SELECT brand AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE brand IS NOT NULL "
        "GROUP BY brand ORDER BY value DESC",
    "sales_by_category":
        "SELECT category AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE category IS NOT NULL "
        "GROUP BY category ORDER BY value DESC",
    "top_brand":
        "SELECT brand AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE brand IS NOT NULL "
        "GROUP BY brand ORDER BY value DESC LIMIT 1",
    "top_category":
        "SELECT category AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE category IS NOT NULL "
        "GROUP BY category ORDER BY value DESC LIMIT 1",
    "peak_sales_day":
        "SELECT transaction_date AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE transaction_date IS NOT NULL "
        "GROUP BY transaction_date ORDER BY value DESC LIMIT 1",
    "sales_by_month":
        "SELECT substr(transaction_date,1,7) AS label, "
        "COALESCE(SUM(net_sales),0) AS value FROM u_sales_transactions "
        "WHERE transaction_date IS NOT NULL GROUP BY label ORDER BY label ASC",
    "payment_method_distribution":
        "SELECT payment_mode AS label, COALESCE(SUM(net_sales),0) AS value "
        "FROM u_sales_transactions WHERE payment_mode IS NOT NULL "
        "GROUP BY payment_mode ORDER BY value DESC",
}

for _kpi in DEFAULT_KPIS:
    if _kpi.get("id") in _REPOINTED_SQL:
        _kpi["sql_template"] = _REPOINTED_SQL[_kpi["id"]]
        _kpi["required_columns"] = []   # bypass legacy-schema column check
        _kpi["enabled"] = True
    else:
        # Cannot be computed correctly on the current uploaded data shape.
        _kpi["enabled"] = False


# ===========================================================================
# CRUD
# ===========================================================================

async def init_kpi_table() -> None:
    """Create the kpi_registry table and indexes. Idempotent."""
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(_DDL)
        for stmt in _INDEX_DDL:
            await db.execute(stmt)
        await db.commit()


async def list_kpis(category: str | None = None, enabled_only: bool = True) -> list[KpiRow]:
    sql = f"SELECT * FROM {KPI_TABLE}"
    params: list[Any] = []
    clauses: list[str] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if category:
        clauses.append("kpi_category = ?")
        params.append(category)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY kpi_category, kpi_name"
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
    return [KpiRow.from_db(dict(r)) for r in rows]


async def get_kpi(kpi_id: str) -> KpiRow | None:
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM {KPI_TABLE} WHERE id = ? OR kpi_name = ?",
            (kpi_id, kpi_id),
        )
        row = await cur.fetchone()
        await cur.close()
    return KpiRow.from_db(dict(row)) if row else None


async def upsert_kpi(kpi: dict[str, Any]) -> None:
    """Insert or update a KPI definition. `kpi` must include the columns
    listed in DEFAULT_KPIS dicts."""
    required = {
        "id", "kpi_name", "kpi_category", "description", "formula_expression",
        "required_columns", "sql_template", "aggregation_type", "output_type",
    }
    missing = required - set(kpi.keys())
    if missing:
        raise ValueError(f"upsert_kpi missing fields: {sorted(missing)}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = (
        kpi["id"],
        kpi["kpi_name"],
        kpi["kpi_category"],
        kpi["description"],
        kpi["formula_expression"],
        json.dumps(kpi.get("required_columns") or []),
        kpi["sql_template"],
        kpi["aggregation_type"],
        kpi["output_type"],
        1 if kpi.get("chart_supported") else 0,
        json.dumps(kpi.get("aliases") or []),
        1 if kpi.get("enabled", True) else 0,
        now,
    )
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            f"""INSERT INTO {KPI_TABLE}
                (id, kpi_name, kpi_category, description, formula_expression,
                 required_columns, sql_template, aggregation_type, output_type,
                 chart_supported, aliases, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  kpi_name           = excluded.kpi_name,
                  kpi_category       = excluded.kpi_category,
                  description        = excluded.description,
                  formula_expression = excluded.formula_expression,
                  required_columns   = excluded.required_columns,
                  sql_template       = excluded.sql_template,
                  aggregation_type   = excluded.aggregation_type,
                  output_type        = excluded.output_type,
                  chart_supported    = excluded.chart_supported,
                  aliases            = excluded.aliases,
                  enabled            = excluded.enabled,
                  updated_at         = excluded.updated_at""",
            payload,
        )
        await db.commit()


async def disable_kpi(kpi_id: str) -> bool:
    async with aiosqlite.connect(db_path()) as db:
        cur = await db.execute(
            f"UPDATE {KPI_TABLE} SET enabled = 0, "
            f"updated_at = datetime('now') WHERE id = ?",
            (kpi_id,),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def enable_kpi(kpi_id: str) -> bool:
    async with aiosqlite.connect(db_path()) as db:
        cur = await db.execute(
            f"UPDATE {KPI_TABLE} SET enabled = 1, "
            f"updated_at = datetime('now') WHERE id = ?",
            (kpi_id,),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def seed_default_catalog(force: bool = False) -> int:
    """Populate the default KPI catalog. By default this is a no-op when
    the table already has rows — pass `force=True` to overwrite every row
    with the shipped default (preserves user-added KPIs not in DEFAULT_KPIS).
    Returns count of rows written."""
    existing = await list_kpis(enabled_only=False)
    if existing and not force:
        _log.info("kpi_registry already populated (%d rows); skipping seed", len(existing))
        return 0
    n = 0
    for kpi in DEFAULT_KPIS:
        await upsert_kpi(kpi)
        n += 1
    _log.info("kpi_registry seeded: %d KPIs written", n)
    return n


async def rebuild_catalog() -> int:
    """Force-rebuild — overwrites every default KPI. User-added KPIs not
    in DEFAULT_KPIS are left untouched (since we don't DELETE)."""
    return await seed_default_catalog(force=True)
