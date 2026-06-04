"""Column profiler — runs after each PG sheet ingest, records per-column
data-quality stats so the resolver can prefer cleaner columns when concept
synonyms tie, and so the Schema tool can warn the LLM about thin columns.

Stats per column:
    pct_non_null    fraction of rows with a non-NULL value      (0.0–1.0)
    pct_numeric     fraction of NON-NULL values parseable as a number
    pct_date        fraction of NON-NULL values starting with YYYY-MM-DD
    distinct_count  distinct values in the column
    min_val/max_val text-cast min and max — sortable across types

Postgres-only. SQLite ingest path skips profiling (the legacy resolver still
works without it).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("agentic_ai.profiler")

# Self-provisioning DDL — mirrors infrastructure._COLUMN_PROFILE_DDL_PG exactly.
# _init_database_postgres() only creates _column_profile in the public schema;
# under a tenant search_path the table doesn't exist, so we CREATE IF NOT EXISTS
# here before every save, matching how relationships.py self-provisions _relationships.
_COLUMN_PROFILE_DDL_PG = """
CREATE TABLE IF NOT EXISTS _column_profile (
    table_name      TEXT NOT NULL,
    column_name     TEXT NOT NULL,
    pct_non_null    DOUBLE PRECISION NOT NULL DEFAULT 0,
    pct_numeric     DOUBLE PRECISION NOT NULL DEFAULT 0,
    pct_date        DOUBLE PRECISION NOT NULL DEFAULT 0,
    distinct_count  BIGINT NOT NULL DEFAULT 0,
    min_val         TEXT,
    max_val         TEXT,
    profiled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (table_name, column_name)
)
"""

_SYSTEM_COLS: frozenset[str] = frozenset({
    "_id", "_batch_id", "_source_sheet", "_inserted_at",
})

# Numeric regex matches optional sign + digits + optional decimal part.
# Comma-thousands ("1,200") are NOT recognised here — the ingest coercer
# strips commas before storage so the stored value is already "1200".
_NUMERIC_RE = r"^-?[0-9]+(\.[0-9]+)?$"
# Date regex matches ISO YYYY-MM-DD prefix; permissive (allows trailing time).
_DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}"


@dataclass(frozen=True)
class ColumnProfile:
    table_name: str
    column_name: str
    pct_non_null: float
    pct_numeric: float
    pct_date: float
    distinct_count: int
    min_val: str | None
    max_val: str | None


async def profile_table_pg(table: str, columns: list[str]) -> list[ColumnProfile]:
    """Profile every non-system column of a Postgres table.

    Runs one query per column (six aggregates). Skips system columns
    (``_id``, ``_batch_id``, ``_source_sheet``, ``_inserted_at``) and the
    table itself if empty. Failures on a single column are logged and
    skipped — never crash the upload pipeline.
    """
    from app.db_engine import pg_connection

    profiles: list[ColumnProfile] = []
    real_cols = [c for c in columns if c not in _SYSTEM_COLS]
    if not real_cols:
        return profiles

    async with pg_connection() as db:
        # Row count once for the whole table — denominator for pct_non_null.
        try:
            cur = await db.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
            row = await cur.fetchone()
            total = int(dict(row)["n"]) if row else 0
        except Exception as e:
            log.warning("profile: COUNT(*) on %r failed: %s", table, e)
            return profiles
        if total == 0:
            return profiles

        for col in real_cols:
            qc = f'"{col}"'
            sql = (
                f"SELECT "
                f"  COUNT(*) FILTER (WHERE {qc} IS NOT NULL) AS non_null, "
                f"  COUNT(*) FILTER (WHERE {qc}::text ~ '{_NUMERIC_RE}') AS numeric_ct, "
                f"  COUNT(*) FILTER (WHERE {qc}::text ~ '{_DATE_RE}') AS date_ct, "
                f"  COUNT(DISTINCT {qc}) AS distinct_ct, "
                f"  MIN({qc}::text) AS min_v, "
                f"  MAX({qc}::text) AS max_v "
                f'FROM "{table}"'
            )
            try:
                cur = await db.execute(sql)
                r = await cur.fetchone()
            except Exception as e:
                log.warning("profile: %s.%s skipped: %s", table, col, e)
                continue
            if r is None:
                continue
            d = dict(r)
            non_null = int(d.get("non_null") or 0)
            numeric_ct = int(d.get("numeric_ct") or 0)
            date_ct = int(d.get("date_ct") or 0)
            distinct_ct = int(d.get("distinct_ct") or 0)
            min_v = d.get("min_v")
            max_v = d.get("max_v")
            pct_non_null = non_null / total
            # Percentages of *non-null* values that are numeric/date — so a
            # column that's 50% NULL but 100% numeric in the non-NULLs still
            # reads as a clean numeric column (the NULL ratio is reported
            # separately via pct_non_null).
            pct_numeric = (numeric_ct / non_null) if non_null else 0.0
            pct_date = (date_ct / non_null) if non_null else 0.0
            profiles.append(ColumnProfile(
                table_name=table,
                column_name=col,
                pct_non_null=round(pct_non_null, 4),
                pct_numeric=round(pct_numeric, 4),
                pct_date=round(pct_date, 4),
                distinct_count=distinct_ct,
                min_val=str(min_v)[:200] if min_v is not None else None,
                max_val=str(max_v)[:200] if max_v is not None else None,
            ))
    return profiles


async def save_profiles_pg(profiles: list[ColumnProfile]) -> int:
    """Upsert ColumnProfile rows into the ``_column_profile`` table.

    Returns the number of rows upserted. No-op if the input list is empty.
    """
    if not profiles:
        return 0
    from app.db_engine import pg_connection
    sql = (
        "INSERT INTO _column_profile "
        "(table_name, column_name, pct_non_null, pct_numeric, pct_date, "
        " distinct_count, min_val, max_val, profiled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (table_name, column_name) DO UPDATE SET "
        "  pct_non_null = EXCLUDED.pct_non_null, "
        "  pct_numeric = EXCLUDED.pct_numeric, "
        "  pct_date = EXCLUDED.pct_date, "
        "  distinct_count = EXCLUDED.distinct_count, "
        "  min_val = EXCLUDED.min_val, "
        "  max_val = EXCLUDED.max_val, "
        "  profiled_at = EXCLUDED.profiled_at"
    )
    _now = datetime.now(timezone.utc).isoformat()
    payload = [
        (p.table_name, p.column_name, p.pct_non_null, p.pct_numeric,
         p.pct_date, p.distinct_count, p.min_val, p.max_val, _now)
        for p in profiles
    ]
    async with pg_connection() as db:
        # Self-provision: _init_database_postgres() only creates _column_profile
        # in public; under a tenant search_path the table won't exist yet.
        await db.execute(_COLUMN_PROFILE_DDL_PG)
        await db.executemany(sql, payload)
    return len(payload)


async def load_table_profile_pg(table: str) -> dict[str, dict[str, Any]]:
    """Return ``{column_name: profile_dict}`` for a Postgres table.

    profile_dict keys: pct_non_null, pct_numeric, pct_date, distinct_count,
    min_val, max_val. Empty dict if no profile rows exist (e.g. profiling
    failed or this is the first load before any upload).
    """
    from app.db_engine import pg_connection
    out: dict[str, dict[str, Any]] = {}
    async with pg_connection() as db:
        # Self-provision: _init_database_postgres() only creates _column_profile
        # in public; under a tenant search_path the table won't exist yet. A
        # never-profiled tenant should read empty, not error (mirrors the save
        # path and relationships.save_relationships_pg).
        await db.execute(_COLUMN_PROFILE_DDL_PG)
        try:
            cur = await db.execute(
                "SELECT column_name, pct_non_null, pct_numeric, pct_date, "
                "  distinct_count, min_val, max_val "
                "FROM _column_profile WHERE table_name = ?",
                (table,),
            )
            rows = await cur.fetchall()
        except Exception as e:
            log.warning("profile: load for %r failed: %s", table, e)
            return out
        for r in rows or []:
            d = dict(r)
            col = d.pop("column_name", None)
            if not col:
                continue
            out[col] = {
                "pct_non_null": float(d.get("pct_non_null") or 0.0),
                "pct_numeric":  float(d.get("pct_numeric") or 0.0),
                "pct_date":     float(d.get("pct_date") or 0.0),
                "distinct_count": int(d.get("distinct_count") or 0),
                "min_val":      d.get("min_val"),
                "max_val":      d.get("max_val"),
            }
    return out


__all__ = [
    "ColumnProfile",
    "load_table_profile_pg",
    "profile_table_pg",
    "save_profiles_pg",
]
