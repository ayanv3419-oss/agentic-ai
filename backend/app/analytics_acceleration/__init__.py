"""Analytics-acceleration layer.

DuckDB sits ALONGSIDE PostgreSQL/SQLite — it is NOT a replacement. Today
SQLite is the source of truth for the financial dataset; DuckDB pulls
read-only views over the same SQLite file via its `sqlite_scanner`
extension (zero-copy scan, no ETL). Future: same code reads PG via
`postgres_scanner`.

What this layer is for:
  - heavy aggregations (monthly_sales_pie, ranking, comparison)
  - dashboard latency reduction (DuckDB optimises GROUP BY way better
    than SQLite's nested-loop aggregations)
  - upload-time post-validation analytics

What it is NOT for:
  - inserts / writes (those still go through SQLite/PG, the source of
    truth)
  - tenant-isolated queries WHEN multi-tenancy lands — DuckDB will need
    the tenant_id pushed down via parameter binding the same way the
    SQLite Database tool does it
"""
from app.analytics_acceleration.duckdb_engine import (
    DuckDBEngine,
    duckdb_status,
    get_duckdb_engine,
)
from app.analytics_acceleration.analytical_queries import (
    monthly_sales_distribution,
    top_entities,
)
from app.analytics_acceleration.dataframe_bridge import (
    rows_to_polars_compatible,
    sqlite_rows_to_dicts,
)

__all__ = [
    "DuckDBEngine",
    "duckdb_status",
    "get_duckdb_engine",
    "monthly_sales_distribution",
    "rows_to_polars_compatible",
    "sqlite_rows_to_dicts",
    "top_entities",
]
