"""Single-table CSV ingest via the dynamic u_* machinery (dynamic_ingest.ingest_csv).

Regression for the production bug where plain CSV uploads routed through the
legacy ``insert_rows`` path, which is a NO-OP on Postgres (and bypasses the
u_* dynamic tables entirely). ``ingest_csv`` instead treats the CSV as a
synthetic single-sheet workbook and routes it through the SAME engine-aware
``ingest_workbook`` dispatcher, so it creates a ``u_*`` table and inserts rows
on BOTH engines.

LOCAL SQLITE ONLY — mirrors test_drive_tokens.py's hard-force-SQLite harness so
the suite never dials the live Supabase instance configured via DATABASE_URL.

Run:
    cd backend && python -m pytest tests/test_csv_ingest.py -q
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# --- Force SQLite BEFORE importing app modules -----------------------------
_SANDBOX_DB = _BACKEND_DIR / "_csv_ingest_test.db"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ.pop("DATABASE_URL", None)

from app import db_engine  # noqa: E402
from app import dynamic_ingest  # noqa: E402
from app.infrastructure import init_database, settings  # noqa: E402


def _purge_db_files() -> None:
    for f in (
        _SANDBOX_DB,
        _SANDBOX_DB.with_suffix(".db.version"),
        Path(str(_SANDBOX_DB) + "-wal"),
        Path(str(_SANDBOX_DB) + "-shm"),
        Path(settings.financial_db_path).parent / "dynamic_tables.json",
    ):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """Hard-force the SQLite engine for every test and assert it stuck."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "database_url", "", raising=False)
    monkeypatch.setattr(settings, "financial_db_path", str(_SANDBOX_DB),
                        raising=False)

    assert db_engine.is_postgres() is False, (
        "test must run on SQLite — is_postgres() is True; DATABASE_URL leaked"
    )

    def _no_postgres(*_a, **_k):
        raise AssertionError("Postgres path reached in a SQLite-only test")

    monkeypatch.setattr(db_engine, "pg_connection", _no_postgres)
    monkeypatch.setattr(db_engine, "get_pool", _no_postgres)

    _purge_db_files()
    asyncio.run(init_database())
    yield
    _purge_db_files()


def test_ingest_csv_creates_u_table_with_right_rows():
    csv = (
        b"Order ID,Product,Quantity,Net Sales\n"
        b"1,Widget,3,29.97\n"
        b"2,Gadget,1,9.99\n"
        b"3,Widget,5,49.95\n"
    )
    summary = asyncio.run(dynamic_ingest.ingest_csv(
        file_bytes=csv,
        source_file_name="sales.csv",
        batch_id="batch-csv-1",
    ))

    # Same envelope shape as the XLSX workbook path.
    assert summary["sheet_count"] == 1
    assert summary["skipped"] == []
    assert summary["tables"] == ["u_sales"]

    entry = summary["ingested"][0]
    assert entry["table"] == "u_sales"
    assert entry["source_sheet"] == "sales"
    assert entry["rows_inserted"] == 3
    col_names = [c["name"] for c in entry["columns"]]
    assert col_names == ["order_id", "product", "quantity", "net_sales"]

    # The u_* table really exists in SQLite with the inserted rows.
    con = sqlite3.connect(str(_SANDBOX_DB))
    try:
        n = con.execute('SELECT COUNT(*) FROM "u_sales"').fetchone()[0]
        assert n == 3
        total = con.execute('SELECT SUM("quantity") FROM "u_sales"').fetchone()[0]
        assert total == 9
    finally:
        con.close()


def test_ingest_empty_csv_raises():
    with pytest.raises(dynamic_ingest.DynamicIngestError):
        asyncio.run(dynamic_ingest.ingest_csv(
            file_bytes=b"",
            source_file_name="empty.csv",
            batch_id="batch-csv-empty",
        ))
