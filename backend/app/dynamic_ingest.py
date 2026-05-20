"""Dynamic multi-sheet xlsx ingestion (ADR-0005).

Each sheet in an uploaded workbook becomes its own SQLite table named
``u_<snake_case_sheet_name>``. Columns are derived from the sheet's header
row; types are inferred from the first ~200 data rows.

The set of dynamic tables is recorded in ``data/dynamic_tables.json`` so the
LLM prompt and SQL validator know what's available between server restarts.

Hard separation from the legacy sales/purchase pipeline:
    • Dynamic tables ALWAYS carry the ``u_`` prefix — never collides with
      static system tables (sales, purchase, product_hierarchy, etc.).
    • Re-uploading a sheet DROPs and recreates its table (matches the
      "Replace" semantics the user picked during planning).
    • Header detection here is LIBERAL (any row with ≥3 string-looking
      cells, where the next row has at least one non-string value or
      different shape) — distinct from the strict alias-driven detection
      used by sales/purchase ingestion.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from openpyxl import load_workbook

from app.infrastructure import db_path, get_connection, settings

log = logging.getLogger("agentic_ai.dynamic_ingest")

# Tables here NEVER collide with these static system tables.
_RESERVED_TABLE_NAMES: frozenset[str] = frozenset({
    "sales", "purchase", "sales_archive", "purchase_archive",
    "uploads", "users", "memberships", "workspaces",
    "conversations", "v2_conversation_turns", "v2_execution_log",
    "v2_shadow_log", "kpi_registry", "analytics_cache",
    "audit_logs", "feedback", "error_log",
    "product_hierarchy", "product_hierarchy_v2", "product_master",
    "product_sku_master", "product_cost_master",
    "location_hierarchy", "branch_master",
    "sku_inventory", "sku_forecast",
    "sqlite_sequence",
})

# Header detection thresholds.
_MIN_STRING_CELLS_FOR_HEADER = 3
_MAX_HEADER_SCAN_ROWS = 20
_TYPE_INFER_SAMPLE = 200

_PREFIX = "u_"
_REGISTRY_LOCK = Lock()


# ---------------------------------------------------------------------------
# Identifier sanitization
# ---------------------------------------------------------------------------

_SNAKE_RE = re.compile(r"[^a-z0-9]+")


def sanitize_identifier(raw: str) -> str:
    """Lowercase, snake_case, alphanumeric+underscore only. Leading digits
    get prefixed with `_`."""
    s = (raw or "").strip().lower()
    s = _SNAKE_RE.sub("_", s)
    s = s.strip("_")
    if not s:
        s = "col"
    if s[0].isdigit():
        s = "_" + s
    return s


def sheet_to_table_name(sheet: str) -> str:
    """`Sales_Transactions` → `u_sales_transactions`.

    The ``u_`` prefix is sufficient on its own to avoid collisions with
    every system table — no further mangling needed.
    """
    return f"{_PREFIX}{sanitize_identifier(sheet)}"


def _dedupe_columns(cols: list[str]) -> list[str]:
    """Sanitize + ensure uniqueness. `Quantity` appearing twice becomes
    `quantity` and `quantity_2`."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in cols:
        c = sanitize_identifier(raw)
        if c in seen:
            seen[c] += 1
            c = f"{c}_{seen[c]}"
        else:
            seen[c] = 1
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Header detection — liberal
# ---------------------------------------------------------------------------

def _is_string_like(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float, bool)):
        return False
    if isinstance(v, datetime):
        return False
    s = str(v).strip()
    if not s:
        return False
    # Pure numbers parse as floats but openpyxl already returned int/float;
    # surviving str values here are header-like.
    return True


def _row_has_any_value(row: list[Any] | None) -> bool:
    if not row:
        return False
    return any(v is not None and str(v).strip() != "" for v in row)


def _next_non_empty(rows: list[list[Any]], i: int) -> list[Any] | None:
    """Skip blank rows after index i and return the next row that has any
    value, or None."""
    for j in range(i + 1, len(rows)):
        if _row_has_any_value(rows[j]):
            return rows[j]
    return None


def _find_header_row(rows: list[list[Any]]) -> int | None:
    """Return the 0-based index of the first row that looks like a header.

    A header is a row that:
      • has ≥ _MIN_STRING_CELLS_FOR_HEADER non-empty STRING cells, AND
      • is followed (possibly across blank rows) by a row that contains at
        least one VALUE — preferably a non-string (number/date), which is
        strong evidence the surrounding region is data.

    Picks the candidate whose NEXT non-empty row has the highest "data-like"
    score (numeric cells > string cells). Falls back to the first
    sufficiently-string-heavy row if no candidate has a clear data follower.
    """
    scored: list[tuple[float, int]] = []
    for i, row in enumerate(rows):
        if i >= _MAX_HEADER_SCAN_ROWS:
            break
        if not row:
            continue
        string_count = sum(1 for c in row if _is_string_like(c))
        if string_count < _MIN_STRING_CELLS_FOR_HEADER:
            continue
        # Header rows are mostly-strings (no numbers, no dates).
        non_string_in_row = sum(
            1 for c in row
            if c is not None and not _is_string_like(c)
        )
        if non_string_in_row > string_count:
            # More numbers than strings — looks like a data row, not header.
            continue
        next_row = _next_non_empty(rows, i)
        if next_row is None:
            # Header with nothing after it — keep as last-resort candidate.
            scored.append((string_count * 0.1, i))
            continue
        next_numeric = sum(
            1 for v in next_row
            if v is not None and not _is_string_like(v)
        )
        next_values = sum(
            1 for v in next_row
            if v is not None and str(v).strip()
        )
        # Strong signal: next row has any numeric value (= data).
        # Weak signal: next row has values at all.
        score = next_numeric * 10 + next_values + string_count
        scored.append((score, i))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][1]


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

_DATE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{2}/\d{2}/\d{4}$"),
    re.compile(r"^\d{2}-\d{2}-\d{4}$"),
)


def _parse_date_str(s: str) -> str | None:
    """Return ISO-8601 YYYY-MM-DD or None."""
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_number_str(value: Any) -> Any:
    """Strip thousands separators + surrounding whitespace from a string
    value so it parses as a number; non-strings pass through unchanged.
    Mirrors the comma-stripping ``_infer_column_type`` does — so a
    "1,200" column inferred REAL also COERCES instead of becoming NULL."""
    if isinstance(value, str):
        return value.strip().replace(",", "")
    return value


def _coerce(value: Any, sql_type: str) -> Any:
    """Convert a python value into something SQLite-friendly for the
    inferred column type."""
    if value is None:
        return None
    if isinstance(value, datetime):
        # Strip the time component on TEXT columns to normalize dates.
        if sql_type == "TEXT":
            return value.strftime("%Y-%m-%d")
        return value.isoformat()
    if sql_type == "TEXT":
        if isinstance(value, str):
            s = value.strip()
            iso = _parse_date_str(s)
            return iso if iso is not None else s
        return str(value)
    if sql_type == "INTEGER":
        try:
            return int(float(_to_number_str(value)))
        except (TypeError, ValueError):
            return None
    if sql_type == "REAL":
        try:
            return float(_to_number_str(value))
        except (TypeError, ValueError):
            return None
    return value


def _infer_column_type(samples: list[Any]) -> str:
    """Decide a SQLite type from a sample of values. REAL > INTEGER > TEXT.

    Ignores Nones. Anything that doesn't parse cleanly as a number forces TEXT.
    """
    saw_real = False
    saw_int = False
    saw_any = False
    for v in samples:
        if v is None:
            continue
        if isinstance(v, bool):
            return "INTEGER"
        if isinstance(v, datetime):
            return "TEXT"
        saw_any = True
        if isinstance(v, int):
            saw_int = True
            continue
        if isinstance(v, float):
            saw_real = True
            continue
        # str — try to parse as number; if it parses, treat as numeric
        s = str(v).strip()
        if not s:
            continue
        # Date-looking strings -> TEXT
        if any(p.match(s) for p in _DATE_PATTERNS):
            return "TEXT"
        try:
            int(s)
            saw_int = True
        except ValueError:
            try:
                float(s.replace(",", ""))
                saw_real = True
            except ValueError:
                return "TEXT"
    if not saw_any:
        return "TEXT"
    if saw_real:
        return "REAL"
    if saw_int:
        return "INTEGER"
    return "TEXT"


# ---------------------------------------------------------------------------
# Registry — persisted to data/dynamic_tables.json
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    return Path(settings.financial_db_path).parent / "dynamic_tables.json"


def load_registry() -> dict[str, dict[str, Any]]:
    """Return ``{table_name: {columns: [...], source_sheet, source_file, row_count, uploaded_at}}``.
    Missing file → empty dict."""
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("dynamic_tables.json load failed; treating as empty", exc_info=True)
        return {}


def _save_registry(reg: dict[str, dict[str, Any]]) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def register_table(
    table: str,
    *,
    columns: list[dict[str, str]],
    source_sheet: str,
    source_file: str,
    row_count: int,
) -> None:
    """Upsert a dynamic table entry. Thread-safe."""
    with _REGISTRY_LOCK:
        reg = load_registry()
        reg[table] = {
            "columns":      columns,
            "source_sheet": source_sheet,
            "source_file":  source_file,
            "row_count":    row_count,
            "uploaded_at":  datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _save_registry(reg)


def unregister_table(table: str) -> None:
    with _REGISTRY_LOCK:
        reg = load_registry()
        reg.pop(table, None)
        _save_registry(reg)


def reconcile_registry() -> dict[str, int]:
    """Reconcile data/dynamic_tables.json against the live SQLite DB.

    On every startup:
    - Adds entries for u_* tables that exist in the DB but not the registry
      (covers the case where dynamic_tables.json was deleted or corrupted).
    - Removes entries whose table no longer exists in the DB.

    Column metadata is recovered from PRAGMA table_info; source_file and
    uploaded_at are cross-referenced from the uploads table via _batch_id.

    Returns {'added': N, 'removed': M}.
    """
    with _REGISTRY_LOCK:
        reg = load_registry()

        conn = sqlite3.connect(str(db_path()))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'u_%'"
            ).fetchall()
            live_tables: set[str] = {r[0] for r in rows}
            reg_tables: set[str] = set(reg.keys())

            missing = live_tables - reg_tables    # in DB, not in JSON
            orphaned = reg_tables - live_tables   # in JSON, not in DB

            if not missing and not orphaned:
                return {"added": 0, "removed": 0}

            for t in orphaned:
                reg.pop(t, None)

            try:
                upload_rows = conn.execute(
                    "SELECT batch_id, filename, uploaded_at FROM uploads"
                ).fetchall()
                uploads_by_batch: dict[str, tuple[str, str]] = {
                    r[0]: (r[1] or "(recovered)", r[2] or "")
                    for r in upload_rows
                }
            except Exception:
                log.warning(
                    "reconcile_registry: uploads table read failed; "
                    "recovered tables will lack source-file metadata",
                    exc_info=True,
                )
                uploads_by_batch = {}

            for table in sorted(missing):
                try:
                    col_rows = conn.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                    columns = [
                        {"name": r[1], "type": r[2] or "TEXT"}
                        for r in col_rows
                        if not r[1].startswith("_")
                    ]

                    try:
                        row_count: int = conn.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    except Exception:
                        log.warning(
                            "reconcile_registry: COUNT failed for %r; "
                            "registering with row_count=0", table, exc_info=True,
                        )
                        row_count = 0

                    source_file = "(recovered)"
                    uploaded_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    try:
                        batch_row = conn.execute(
                            f'SELECT DISTINCT _batch_id FROM "{table}" LIMIT 1'
                        ).fetchone()
                        if batch_row and batch_row[0] in uploads_by_batch:
                            fname, u_at = uploads_by_batch[batch_row[0]]
                            source_file = fname
                            if u_at:
                                uploaded_at = u_at
                    except Exception:
                        log.debug(
                            "reconcile_registry: _batch_id lookup failed for %r; "
                            "leaving source_file as '(recovered)'",
                            table, exc_info=True,
                        )

                    sheet_name = (
                        table[2:].replace("_", " ").title()
                        if table.startswith("u_") else table
                    )

                    reg[table] = {
                        "columns":      columns,
                        "source_sheet": sheet_name,
                        "source_file":  source_file,
                        "row_count":    row_count,
                        "uploaded_at":  uploaded_at,
                    }
                except Exception:
                    log.warning(
                        "reconcile_registry: could not rebuild entry for %r (skipping)",
                        table, exc_info=True,
                    )
        finally:
            conn.close()

        _save_registry(reg)

    added = len(missing)
    removed = len(orphaned)
    if added or removed:
        log.info(
            "dynamic table registry reconciled: added=%d removed=%d live=%d",
            added, removed, len(live_tables),
        )
    return {"added": added, "removed": removed}


# ---------------------------------------------------------------------------
# Per-sheet ingestion
# ---------------------------------------------------------------------------

class DynamicIngestError(ValueError):
    """Sheet-level failure: no detectable header / no usable rows."""


def _iter_sheet_rows(ws) -> Iterator[list[Any]]:
    for row in ws.iter_rows(values_only=True):
        yield list(row) if row is not None else []


def _build_create_sql(table: str, cols: list[tuple[str, str]]) -> str:
    col_defs = ",\n  ".join(f'"{name}" {sqltype}' for name, sqltype in cols)
    return (
        f'CREATE TABLE "{table}" (\n'
        f'  _id INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        f'  _batch_id TEXT NOT NULL,\n'
        f'  _source_sheet TEXT NOT NULL,\n'
        f'  _inserted_at TEXT NOT NULL DEFAULT (datetime(\'now\')),\n'
        f'  {col_defs}\n'
        f')'
    )


def ingest_sheet(
    *,
    wb_path: Path,
    sheet_name: str,
    source_file_name: str,
    batch_id: str,
) -> dict[str, Any]:
    """Drop + create + insert one sheet into ``u_<sheet_name>``.

    Synchronous SQLite. Called from inside the async upload route via
    ``run_in_executor`` if needed; for the multi-sheet loop a synchronous
    call inside the request is fine (single-user MVP).

    Returns
    -------
    summary : dict
        {table, columns, header_row, rows_inserted, dropped_existing, skipped_rows}
    """
    wb = load_workbook(filename=str(wb_path), read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise DynamicIngestError(f"sheet not found: {sheet_name!r}")
        ws = wb[sheet_name]

        # Collect first ~20 rows for header detection
        buffered: list[list[Any]] = []
        row_iter = _iter_sheet_rows(ws)
        for _ in range(_MAX_HEADER_SCAN_ROWS):
            try:
                buffered.append(next(row_iter))
            except StopIteration:
                break
        if not buffered:
            raise DynamicIngestError(f"sheet {sheet_name!r} is empty")

        hdr_idx = _find_header_row(buffered)
        if hdr_idx is None:
            raise DynamicIngestError(
                f"could not detect a header row in first {_MAX_HEADER_SCAN_ROWS} "
                f"rows of sheet {sheet_name!r}"
            )
        header_raw = [str(c).strip() if c is not None else "" for c in buffered[hdr_idx]]
        # Drop trailing all-empty header cells, keep first non-empty span.
        while header_raw and not header_raw[-1]:
            header_raw.pop()
        if not header_raw:
            raise DynamicIngestError(f"sheet {sheet_name!r} has empty header row")
        col_names = _dedupe_columns(header_raw)
        n_cols = len(col_names)

        # Collect data rows (post-header) for type inference + insert
        data_rows: list[list[Any]] = []
        for r in buffered[hdr_idx + 1:]:
            if r is None:
                continue
            trimmed = r[:n_cols] + [None] * max(0, n_cols - len(r))
            if any(v is not None and str(v).strip() != "" for v in trimmed):
                data_rows.append(trimmed)
        # Continue draining the iterator
        for r in row_iter:
            trimmed = r[:n_cols] + [None] * max(0, n_cols - len(r))
            if any(v is not None and str(v).strip() != "" for v in trimmed):
                data_rows.append(trimmed)

        if not data_rows:
            raise DynamicIngestError(f"sheet {sheet_name!r} has no data rows after header")

        # Infer types from first N rows per column
        col_types: list[str] = []
        sample = data_rows[:_TYPE_INFER_SAMPLE]
        for i in range(n_cols):
            samples = [row[i] for row in sample]
            col_types.append(_infer_column_type(samples))

        table = sheet_to_table_name(sheet_name)

        # DROP + CREATE in raw sqlite3 (sync; simpler than aiosqlite here)
        con = sqlite3.connect(str(db_path()))
        try:
            cur = con.cursor()
            dropped = False
            try:
                row = cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is not None:
                    cur.execute(f'DROP TABLE "{table}"')
                    dropped = True
            except sqlite3.Error:
                pass
            cur.execute(_build_create_sql(table, list(zip(col_names, col_types))))

            # Bulk insert
            placeholders = ", ".join("?" * (n_cols + 2))  # +2 for _batch_id, _source_sheet
            quoted_cols = ", ".join(f'"{c}"' for c in col_names)
            insert_sql = (
                f'INSERT INTO "{table}" (_batch_id, _source_sheet, {quoted_cols}) '
                f'VALUES ({placeholders})'
            )
            payload = []
            skipped = 0
            for row in data_rows:
                try:
                    coerced = [_coerce(row[i], col_types[i]) for i in range(n_cols)]
                    payload.append((batch_id, sheet_name, *coerced))
                except Exception:
                    skipped += 1
            cur.executemany(insert_sql, payload)
            con.commit()
            rows_inserted = len(payload)
        finally:
            con.close()

        # Persist registry entry
        columns_meta = [
            {"name": name, "type": sqltype}
            for name, sqltype in zip(col_names, col_types)
        ]
        register_table(
            table,
            columns=columns_meta,
            source_sheet=sheet_name,
            source_file=source_file_name,
            row_count=rows_inserted,
        )

        log.info(
            "dynamic_ingest: sheet=%r → table=%r rows=%d cols=%d dropped=%s skipped=%d",
            sheet_name, table, rows_inserted, n_cols, dropped, skipped,
        )

        return {
            "table":             table,
            "source_sheet":      sheet_name,
            "header_row":        hdr_idx + 1,
            "columns":           columns_meta,
            "rows_inserted":     rows_inserted,
            "dropped_existing":  dropped,
            "skipped_rows":      skipped,
        }
    finally:
        try:
            wb.close()
        except Exception:
            pass


def ingest_workbook(
    *,
    wb_path: Path,
    source_file_name: str,
    batch_id: str,
) -> dict[str, Any]:
    """Loop every sheet in the workbook, ingest each as a dynamic table.

    Sheets where header detection fails are recorded under ``skipped`` with
    the reason, never aborting the whole upload.
    """
    wb = load_workbook(filename=str(wb_path), read_only=True, data_only=True)
    try:
        sheet_names = list(wb.sheetnames)
    finally:
        try:
            wb.close()
        except Exception:
            pass

    ingested: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for sheet in sheet_names:
        try:
            summary = ingest_sheet(
                wb_path=wb_path,
                sheet_name=sheet,
                source_file_name=source_file_name,
                batch_id=batch_id,
            )
            ingested.append(summary)
        except DynamicIngestError as e:
            log.warning("dynamic_ingest: skip sheet=%r reason=%s", sheet, e)
            skipped.append({"sheet": sheet, "reason": str(e)})
        except Exception as e:
            log.exception("dynamic_ingest: sheet=%r crashed", sheet)
            skipped.append({"sheet": sheet, "reason": f"{type(e).__name__}: {e}"})

    return {
        "batch_id":      batch_id,
        "source_file":   source_file_name,
        "sheet_count":   len(sheet_names),
        "ingested":      ingested,
        "skipped":       skipped,
        "tables":        [s["table"] for s in ingested],
    }


# ---------------------------------------------------------------------------
# Public helpers used by analytics_engine + core_system
# ---------------------------------------------------------------------------

def list_dynamic_tables() -> list[dict[str, Any]]:
    """All currently-registered dynamic tables with metadata."""
    reg = load_registry()
    return [
        {"table": name, **meta}
        for name, meta in sorted(reg.items())
    ]


def dynamic_table_columns(table: str) -> list[dict[str, str]] | None:
    reg = load_registry()
    entry = reg.get(table)
    if entry is None:
        return None
    return entry.get("columns") or []


def dynamic_schema_summary() -> str:
    """Compact human-readable schema injected into the LLM system prompt.

    Returns "" when no dynamic tables exist so we don't pollute the prompt.
    """
    tables = list_dynamic_tables()
    if not tables:
        return ""
    lines = ["", "User-uploaded tables (raw, queryable via raw SQL):"]
    for t in tables:
        cols = ", ".join(
            f'"{c["name"]}" ({c["type"]})'
            for c in t.get("columns", [])
        )
        lines.append(
            f'  - "{t["table"]}" (from sheet "{t["source_sheet"]}", '
            f'{t["row_count"]} rows): {cols}'
        )
    return "\n".join(lines)


def known_dynamic_identifiers() -> set[str]:
    """All table + column names from the registry — for SQL validator
    whitelist."""
    out: set[str] = set()
    for t in list_dynamic_tables():
        out.add(t["table"])
        for c in t.get("columns", []):
            out.add(c["name"])
        # Built-in metadata columns
        out.update({"_id", "_batch_id", "_source_sheet", "_inserted_at"})
    return out


async def drop_all_dynamic_tables() -> int:
    """Delete every registered dynamic table from the DB and clear the
    registry. Returns the count dropped. Used by /upload/disconnect_all."""
    reg = load_registry()
    count = 0
    async with get_connection() as conn:
        for name in list(reg.keys()):
            try:
                await conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                count += 1
            except Exception:
                log.warning("drop_all_dynamic_tables: failed on %r", name, exc_info=True)
        await conn.commit()
    with _REGISTRY_LOCK:
        _save_registry({})
    return count
