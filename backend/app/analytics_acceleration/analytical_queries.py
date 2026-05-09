"""Pre-built fast aggregations.

Each function takes a connected `DuckDBEngine` and returns a clean
list[dict] payload that downstream tools / agents can serialize directly.
The queries are deliberately written to mirror the existing SQLite SQL
shape so a side-by-side correctness check is trivial:

    sqlite_rows  = await registry.execute("Database", {"op": "select", ...})
    duckdb_rows  = await analytical_queries.monthly_sales_distribution(...)
    assert sqlite_rows == duckdb_rows

Why DuckDB is faster here:
  - Vectorised GROUP BY (SIMD), versus SQLite's nested-loop aggregation
  - Multi-threaded parallel scan (sqlite_scanner pushes the WHERE clauses)
  - Columnar in-flight materialisation — date and amount columns are
    decoded once per chunk, not once per row
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.analytics_acceleration.duckdb_engine import DuckDBEngine


_log = logging.getLogger("agentic_ai.acceleration.queries")


async def monthly_sales_distribution(
    engine: DuckDBEngine,
    *,
    table: str = "sales",
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Sum sales by year-month, ascending. Mirrors `DashboardAgent
    ._aggregate_monthly_sales_pie`.

    Tenant scope: when `tenant_id` is provided, only that tenant's rows
    participate. None = legacy single-admin (count everything).

    Returns: [{"month": "Jan 2025", "sales": 120000.0}, ...]
    Empty when no aggregable rows exist (NULL/malformed dates filtered
    at the SQL layer)."""
    where_parts = [
        '"Date" IS NOT NULL',
        'length("Date") = 10',
        "substr(\"Date\", 5, 1) = '-'",
        "substr(\"Date\", 8, 1) = '-'",
        '"Total Amount" IS NOT NULL',
    ]
    params: list[Any] = []
    if tenant_id:
        where_parts.append("tenant_id = ?")
        params.append(tenant_id)
    where_sql = " AND ".join(where_parts)
    sql = (
        f'SELECT '
        f'  substr("Date", 1, 7) AS ym, '
        f'  SUM("Total Amount") AS sales '
        f'FROM {table} '
        f'WHERE {where_sql} '
        f'GROUP BY ym '
        f'HAVING SUM("Total Amount") > 0 '
        f'ORDER BY ym ASC'
    )
    raw = await engine.query(sql, params)
    out: list[dict[str, Any]] = []
    for r in raw:
        ym = r.get("ym")
        if not isinstance(ym, str) or len(ym) != 7 or ym[4] != "-":
            continue
        try:
            label = datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
        except ValueError:
            continue
        out.append({"month": label, "sales": round(float(r.get("sales") or 0), 2)})
    return out


async def top_entities(
    engine: DuckDBEngine,
    *,
    table: str = "sales",
    group_col: str,
    start_date: str | None = None,
    end_date: str | None = None,
    direction: str = "desc",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Generic top-N (or bottom-N) ranking by SUM(Total Amount). Mirrors
    `SqlPlannerTool._plan_ranking` so the same call site can swap between
    SQLite and DuckDB transparently.

    `group_col` MUST be a known schema column (e.g. "Product Name" or
    "Party Name") — passed through `quoted` by the caller. We do not
    validate here because the caller is the ranking-plan code, which
    already constrains it."""
    direction = direction.lower()
    sort_dir = "ASC" if direction == "asc" else "DESC"
    where_parts = [
        f'"{group_col}" IS NOT NULL',
        f'"{group_col}" != \'\'',
    ]
    params: list[Any] = []
    if start_date:
        where_parts.append('"Date" >= ?')
        params.append(start_date)
    if end_date:
        where_parts.append('"Date" <= ?')
        params.append(end_date)
    where_sql = " AND ".join(where_parts)

    sql = (
        f'SELECT '
        f'  "{group_col}" AS bucket, '
        f'  SUM("Total Amount") AS sales, '
        f'  COUNT(*) AS orders '
        f'FROM {table} '
        f'WHERE {where_sql} '
        f'GROUP BY "{group_col}" '
        f'ORDER BY sales {sort_dir} '
        f'LIMIT {max(1, min(int(limit), 50))}'
    )
    rows = await engine.query(sql, params)
    return [
        {
            "name":   str(r.get("bucket") or ""),
            "sales":  round(float(r.get("sales") or 0), 2),
            "orders": int(r.get("orders") or 0),
        }
        for r in rows
        if r.get("bucket")
    ]
