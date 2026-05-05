"""Database package.

Exposes the canonical schema, connection helpers, and the read/write
operations that DataCleanAgent and DashboardAgent (and the SqlExecutor
tool, indirectly) use. All financial-data persistence lives here.
"""
from app.database.schema import (
    SCHEMA_SPEC,
    SCHEMA_COLUMNS,
    REQUIRED_COLUMNS,
    OPTIONAL_COLUMNS,
    COLUMN_TYPES,
    ALLOWED_TABLES,
    HEADER_ALIASES,
    quoted,
    schema_dict,
)
from app.database.connection import (
    db_path,
    init_database,
    get_connection,
    fetch_all,
    fetch_one,
    insert_rows,
    list_uploads_meta,
    get_upload_meta,
    record_upload_meta,
    disconnect_upload,
    count_rows,
)

__all__ = [
    "SCHEMA_SPEC",
    "SCHEMA_COLUMNS",
    "REQUIRED_COLUMNS",
    "OPTIONAL_COLUMNS",
    "COLUMN_TYPES",
    "ALLOWED_TABLES",
    "HEADER_ALIASES",
    "quoted",
    "schema_dict",
    "db_path",
    "init_database",
    "get_connection",
    "fetch_all",
    "fetch_one",
    "insert_rows",
    "list_uploads_meta",
    "get_upload_meta",
    "record_upload_meta",
    "disconnect_upload",
    "count_rows",
]
