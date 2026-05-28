"""
Deterministic UI-triggered agents.

These two agents never touch the LLM and never go through the
Coordinator. They're invoked directly by their respective HTTP routes
in core_system.py:

  * DashboardAgent -> GET /dashboard
  * DataCleanAgent -> POST /upload, POST /upload/preview, POST /drive/sync

Ported out of the deleted analytics_engine module so the Coordinator
package is the only orchestrator in the codebase.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date as _date_cls, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.infrastructure import (
    ALLOWED_TABLES,
    COLUMN_TYPES,
    REQUIRED_COLUMNS,
    SCHEMA_COLUMNS,
    UploadError,
    fetch_all,
    fetch_one,
    get_connection,
    insert_rows,
    map_headers_strict,
    quoted,
    record_upload_meta,
    stream_parse_csv_with_detection,
    stream_parse_xlsx_with_detection,
)


_dashboard_log = logging.getLogger("agentic_ai.agents.dashboard")
_dataclean_log = logging.getLogger("agentic_ai.agents.dataclean")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ===========================================================================
# Normalization helpers (formerly analytics_engine top-level helpers).
# ===========================================================================

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y%m%d",
)

_AMOUNT_STRIP = (",", "₹", "$", "Rs.", "Rs", " ")


def _parse_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date_cls):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return None
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    for ch in _AMOUNT_STRIP:
        s = s.replace(ch, "")
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    s = str(value).strip()
    return s if s else None


def normalize_row(raw: dict[str, Any], header_index: dict[str, str]) -> dict[str, Any]:
    """Map raw cells to canonical column dict (canonical -> typed value)."""
    out: dict[str, Any] = {c: None for c in SCHEMA_COLUMNS}
    for raw_key, canonical in header_index.items():
        v = raw.get(raw_key)
        col_type = COLUMN_TYPES[canonical]
        if canonical == "Date":
            out[canonical] = _parse_date(v)
        elif col_type == "REAL":
            out[canonical] = _parse_amount(v)
        else:
            out[canonical] = _parse_text(v)
    return out


def validate_row(normalized: dict[str, Any]) -> str | None:
    """Return None if valid, else a human-readable rejection reason."""
    for req in REQUIRED_COLUMNS:
        v = normalized.get(req)
        if v is None or (isinstance(v, str) and not v):
            return f"required field '{req}' missing or unparseable"
    if not isinstance(normalized.get("Date"), str) or not _ISO_DATE_RE.match(
        normalized["Date"]
    ):
        return "Date is not ISO YYYY-MM-DD"
    amt = normalized.get("Total Amount")
    if not isinstance(amt, (int, float)):
        return "Total Amount is not numeric"
    return None


async def _validate_post_insert(target: str, batch_id: str) -> dict[str, Any]:
    """Dashboard-integrity sanity check on the just-inserted batch."""
    row = await fetch_one(
        f'SELECT '
        f'  COUNT(*) AS n, '
        f'  MIN("Date") AS min_date, '
        f'  MAX("Date") AS max_date, '
        f'  COUNT(CASE WHEN "Date" IS NULL OR "Date" = \'\' THEN 1 END) AS null_dates, '
        f'  COUNT(CASE WHEN "Total Amount" IS NULL THEN 1 END) AS null_amts '
        f'FROM {quoted(target)} WHERE batch_id = ?',
        (batch_id,),
    )
    if not row or int(row.get("n") or 0) == 0:
        raise UploadError("post-insert validation: 0 rows persisted from this batch")
    n = int(row["n"])
    null_dates = int(row.get("null_dates") or 0)
    null_amts = int(row.get("null_amts") or 0)
    if null_dates:
        raise UploadError(
            f"post-insert validation: {null_dates}/{n} rows have NULL Date "
            f"- dashboard would break"
        )
    if null_amts:
        raise UploadError(
            f"post-insert validation: {null_amts}/{n} rows have NULL Total Amount "
            f"- dashboard aggregation would break"
        )
    min_d = row.get("min_date") or ""
    max_d = row.get("max_date") or ""
    if not (_ISO_DATE_RE.match(str(min_d)) and _ISO_DATE_RE.match(str(max_d))):
        raise UploadError(
            f"post-insert validation: Date range invalid (min={min_d!r}, max={max_d!r})"
        )
    return {"batch_rows": n, "min_date": min_d, "max_date": max_d}


# ===========================================================================
# DashboardAgent - deterministic, no LLM.
# ===========================================================================


# Postgres data-type names that behave like text in LIKE / substr expressions.
# Anything outside this set (date, timestamp, timestamp with time zone, …) is
# treated as a typed temporal value and must use to_char() / native operators.
_TEXTLIKE_TYPES: frozenset[str] = frozenset({
    "text", "varchar", "character varying", "character", "char",
    "citext", "name",
})


def _column_data_type(
    user_tables: list[dict[str, Any]], table: str, column: str,
) -> str:
    """Return the lower-cased data_type of one column from the schema
    snapshot, or '' when the column isn't found. Tolerates the
    SQLite-path entries (where 'type' may be uppercase like 'TEXT')."""
    for t in user_tables:
        if t.get("table") != table:
            continue
        for c in t.get("columns", []):
            if c.get("name") == column:
                return str(c.get("type") or "").lower()
        return ""
    return ""


def _is_text_type(data_type: str) -> bool:
    """True when LIKE / substr applied directly to the column works."""
    t = (data_type or "").lower().strip()
    if not t:
        # Unknown type: assume text — that's the safer guess for legacy
        # SQLite paths where types were stored loosely.
        return True
    return t in _TEXTLIKE_TYPES


class DashboardAgent:
    """Reads the user's uploaded dataset and aggregates KPIs + time series.

    Uses the schema-mapping resolver so it works on ANY workbook the user
    uploads — the resolver maps `transaction_date` / `revenue` / `customer`
    / `invoice_id` concepts to whatever columns the workbook actually has.
    Works on both SQLite and Postgres (Supabase).

    Output shape (frozen contract, frontend reads it):
        { month, kpis: {total_sales, orders, customers},
          series: [{bucket, sales, orders}], monthly_sales_pie: [...] }
    """

    name = "DashboardAgent"

    @staticmethod
    def _empty_payload(month: str | None, reason: str) -> dict[str, Any]:
        _dashboard_log.info("DashboardAgent empty payload: %s", reason)
        return {
            "month": month,
            "kpis": {"total_sales": 0.0, "orders": 0, "customers": 0},
            "series": [],
            "monthly_sales_pie": [],
        }

    @staticmethod
    async def _list_user_tables_for_resolver() -> list[dict[str, Any]]:
        """Return the same shape SchemaTool uses, so resolve_schema works.

        Each entry: {"table": name, "columns": [{"name": col, "type": t}, ...]}
        """
        from app.db_engine import is_postgres
        out: list[dict[str, Any]] = []
        async with get_connection() as db:
            if is_postgres():
                cur = await db.execute(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name LIKE 'u\\_%' ESCAPE '\\' "
                    "ORDER BY table_name"
                )
            else:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'u\\_%' ESCAPE '\\' "
                    "ORDER BY name"
                )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                name = dict(r).get("name") or ""
                if not name:
                    continue
                if is_postgres():
                    cur2 = await db.execute(
                        "SELECT column_name AS name, data_type AS type "
                        "FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=? "
                        "ORDER BY ordinal_position",
                        (name,),
                    )
                else:
                    cur2 = await db.execute(f"PRAGMA table_info({quoted(name)})")
                cols = await cur2.fetchall()
                await cur2.close()
                col_defs = [
                    {"name": dict(c)["name"], "type": (dict(c).get("type") or "TEXT")}
                    for c in cols
                ]
                out.append({"table": name, "columns": col_defs})
        return out

    async def run(self, *, month: str | None = None) -> dict[str, Any]:
        # 1. Validate month format if supplied.
        if month is not None and not (len(month) == 7 and month[4] == "-"):
            raise ValueError(f"invalid month format: {month!r}")

        # 2. Resolve the dataset schema. If no u_* tables exist or required
        #    concepts can't be resolved, return an empty (but well-shaped)
        #    payload so the frontend renders zero-state instead of crashing.
        from app.schema_mapping import resolve_schema
        user_tables = await self._list_user_tables_for_resolver()
        if not user_tables:
            return self._empty_payload(month, "no u_* tables uploaded yet")
        resolved = resolve_schema(user_tables)

        date_ref = resolved.ref("transaction_date")
        rev_ref = resolved.ref("revenue")
        cust_ref = resolved.ref("customer")
        inv_ref = resolved.ref("invoice_id")
        if date_ref is None or rev_ref is None:
            return self._empty_payload(
                month,
                f"dataset missing required concepts "
                f"(date={date_ref}, revenue={rev_ref})",
            )

        sales_t = date_ref.table
        date_col = date_ref.column
        rev_col = rev_ref.column
        # Customer / invoice_id are usable when they live on the SAME table as
        # the sales rows. If they're on a different table (common in
        # normalized schemas where party_name lives in u_item_details),
        # we run a separate cross-table query below.
        cust_same_t = (cust_ref and cust_ref.table == sales_t)
        inv_same_t = (inv_ref and inv_ref.table == sales_t)

        # Look up the date column's actual data type. Postgres rejects
        # `LIKE '____-__-__'` on a real DATE/TIMESTAMPTZ column with
        # "operator does not exist: date ~~ unknown". The legacy SQLite
        # path stores dates as TEXT so the LIKE works there. Branch on
        # type — for typed date columns the type itself guarantees a
        # valid date, so we can skip the shape filter.
        date_type = _column_data_type(user_tables, sales_t, date_col)
        date_is_text = _is_text_type(date_type)
        # Expression used wherever we need a YYYY-MM-DD string out of the
        # date column (month filter, monthly pie, series formatting).
        date_text_expr = (
            quoted(date_col)
            if date_is_text
            else f"to_char({quoted(date_col)}, 'YYYY-MM-DD')"
        )

        where_parts = [
            f'{quoted(date_col)} IS NOT NULL',
            f'{quoted(rev_col)} IS NOT NULL',
        ]
        if date_is_text:
            where_parts.append(f"{quoted(date_col)} LIKE '____-__-__'")
        params: list[Any] = []
        if month is not None:
            where_parts.append(f"{date_text_expr} LIKE ?")
            params.append(f"{month}-%")
        where_sql = " WHERE " + " AND ".join(where_parts)

        # 3. KPI aggregation — single SQL call. Push the work to Postgres.
        kpi_select_parts = [
            f'COALESCE(SUM({quoted(rev_col)}), 0) AS total_sales',
        ]
        if inv_same_t and inv_ref is not None:
            kpi_select_parts.append(
                f'COUNT(DISTINCT {quoted(inv_ref.column)}) AS orders'
            )
        else:
            kpi_select_parts.append('COUNT(*) AS orders')
        if cust_same_t and cust_ref is not None:
            kpi_select_parts.append(
                f'COUNT(DISTINCT {quoted(cust_ref.column)}) AS customers'
            )
        else:
            kpi_select_parts.append('0 AS customers')

        kpi_sql = (
            f'SELECT {", ".join(kpi_select_parts)} '
            f'FROM {quoted(sales_t)}'
            f'{where_sql}'
        )
        try:
            kpi_row = await fetch_one(kpi_sql, params)
        except Exception as e:
            _dashboard_log.warning("DashboardAgent KPI query failed: %s", e)
            return self._empty_payload(month, f"KPI query failed: {e}")
        kpi_data = dict(kpi_row or {})
        total_sales = float(kpi_data.get("total_sales") or 0)
        orders = int(kpi_data.get("orders") or 0)
        customers = int(kpi_data.get("customers") or 0)

        # 4. Cross-table customer count — when party_name lives on a
        #    different table than the sales rows (e.g. u_item_details).
        if customers == 0 and cust_ref and not cust_same_t:
            try:
                cust_sql = (
                    f'SELECT COUNT(DISTINCT {quoted(cust_ref.column)}) AS n '
                    f'FROM {quoted(cust_ref.table)} '
                    f'WHERE {quoted(cust_ref.column)} IS NOT NULL'
                )
                cr = await fetch_one(cust_sql, [])
                if cr:
                    customers = int(dict(cr).get("n") or 0)
            except Exception as e:
                _dashboard_log.warning(
                    "DashboardAgent cross-table customer count failed: %s", e,
                )

        # 5. Time series — per-day aggregation in SQL.
        series_sql = (
            f'SELECT {quoted(date_col)} AS day, '
            f'  SUM({quoted(rev_col)}) AS sales, '
            f'  COUNT(*) AS orders '
            f'FROM {quoted(sales_t)}'
            f'{where_sql} '
            f'GROUP BY {quoted(date_col)} '
            f'ORDER BY {quoted(date_col)} '
            f'LIMIT 366'
        )
        try:
            series_rows = await fetch_all(series_sql, params)
        except Exception as e:
            _dashboard_log.warning("DashboardAgent series query failed: %s", e)
            series_rows = []

        series: list[dict[str, Any]] = []
        for r in series_rows:
            d = r.get("day")
            if isinstance(d, (datetime, _date_cls)):
                d = d.strftime("%Y-%m-%d")
            if not isinstance(d, str) or not _ISO_DATE_RE.match(d):
                continue
            try:
                s = float(r.get("sales") or 0)
            except (TypeError, ValueError):
                s = 0.0
            o = int(r.get("orders") or 0)
            series.append({"bucket": d, "sales": round(s, 2), "orders": o})

        # 6. Monthly pie.
        monthly_sales_pie = await self._aggregate_monthly_sales_pie(
            sales_t, date_col, rev_col, date_is_text,
        )

        _dashboard_log.info(
            "DashboardAgent ok table=%s total_sales=%.2f orders=%d customers=%d series=%d",
            sales_t, total_sales, orders, customers, len(series),
        )

        return {
            "month": month,
            "kpis": {
                "total_sales": round(total_sales, 2),
                "orders":      orders,
                "customers":   customers,
            },
            "series": series,
            "monthly_sales_pie": monthly_sales_pie,
        }

    @staticmethod
    async def _aggregate_monthly_sales_pie(
        sales_t: str, date_col: str, rev_col: str, date_is_text: bool,
    ) -> list[dict[str, Any]]:
        """SQL GROUP BY year-month -> SUM(amount).

        Branches on date column type:
          - TEXT (SQLite path, or text columns on Postgres): substr() + LIKE
          - DATE/TIMESTAMP (Postgres typed columns): to_char(date, 'YYYY-MM')
            — substr() and LIKE don't work on date types in Postgres.
        """
        if date_is_text:
            ym_expr = f"substr({quoted(date_col)}, 1, 7)"
            shape_filter = (
                f"AND {quoted(date_col)} LIKE '____-__-__' "
            )
        else:
            ym_expr = f"to_char({quoted(date_col)}, 'YYYY-MM')"
            shape_filter = ""
        pie_sql = (
            f'SELECT '
            f'  {ym_expr} AS ym, '
            f'  SUM({quoted(rev_col)}) AS sales '
            f'FROM {quoted(sales_t)} '
            f'WHERE {quoted(date_col)} IS NOT NULL '
            f'  {shape_filter}'
            f'  AND {quoted(rev_col)} IS NOT NULL '
            f'GROUP BY ym '
            f'ORDER BY ym ASC'
        )
        _dashboard_log.info("DashboardAgent monthly_sales_pie aggregation start")
        try:
            pie_rows = await fetch_all(pie_sql, [])
        except Exception as e:
            _dashboard_log.warning(
                "DashboardAgent monthly_sales_pie query failed: %s", e,
            )
            return []

        out: list[dict[str, Any]] = []
        for r in pie_rows:
            ym = r.get("ym")
            if not isinstance(ym, str) or len(ym) != 7 or ym[4] != "-":
                continue
            try:
                label = datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
            except ValueError:
                continue
            try:
                sales = float(r.get("sales") or 0)
            except (TypeError, ValueError):
                continue
            if sales <= 0:
                continue
            out.append({"month": label, "sales": round(sales, 2)})

        if not out:
            _dashboard_log.info(
                "DashboardAgent monthly_sales_pie empty - no aggregable rows"
            )
        else:
            _dashboard_log.info(
                "DashboardAgent monthly_sales_pie ok rows=%d span=%s..%s",
                len(out), out[0]["month"], out[-1]["month"],
            )
        return out


# ===========================================================================
# DataCleanAgent - deterministic ingestion pipeline.
# ===========================================================================


class DataCleanAgent:
    """Parsing + normalization + insertion + validation."""

    name = "DataCleanAgent"

    async def run(
        self,
        *,
        tmp_path: Path,
        filename: str,
        target: str,
        batch_id: str | None = None,
        dedup_mode: str = "block",
        preview_only: bool = False,
        source: str = "upload",
    ) -> dict[str, Any]:
        from app.dedup import (
            DEDUP_MODES, classify_batch, delete_rows_with_hashes,
        )
        if target not in ALLOWED_TABLES:
            raise UploadError(f"target must be one of {ALLOWED_TABLES!r}")
        if dedup_mode not in DEDUP_MODES:
            raise UploadError(
                f"dedup_mode must be one of {DEDUP_MODES!r}, got {dedup_mode!r}"
            )
        suffix = tmp_path.suffix.lower()

        # 1. FileParser - auto header detection.
        sheet_name: str | None = None
        if suffix == ".csv":
            header, header_index, row_iter = stream_parse_csv_with_detection(tmp_path)
        elif suffix == ".xlsx":
            (
                header,
                header_index,
                row_iter,
                sheet_name,
            ) = stream_parse_xlsx_with_detection(tmp_path)
        else:
            raise UploadError(f"unsupported file type: {suffix}")

        # 2. HeaderMapper - verify and compute extras.
        _, missing_required, unmatched_extras = map_headers_strict(header)
        if missing_required:
            raise UploadError(
                f"required column(s) without matching header: {missing_required}; "
                f"file headers: {header}"
            )

        # 3 + 4. Normalize + validate, accumulating errors.
        valid_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        rows_seen = 0
        try:
            for raw in row_iter:
                rows_seen += 1
                normalized = normalize_row(raw, header_index)
                reason = validate_row(normalized)
                if reason:
                    errors.append({"row": rows_seen, "reason": reason})
                    continue
                valid_rows.append(normalized)
        finally:
            close = getattr(row_iter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        if not valid_rows:
            raise UploadError(
                f"no valid rows after parsing - {rows_seen} rows seen, "
                f"{len(errors)} rejected. First reason: "
                f"{errors[0]['reason'] if errors else '(no rows in file)'}"
            )

        # 5. Deduplication classification.
        classification = await classify_batch(target, valid_rows, mode=dedup_mode)
        dedup_summary = classification.summary()
        _dataclean_log.info("dedup classification: %s", dedup_summary)

        # 5a. Apply policy: block / skip / replace / append.
        if dedup_mode == "block" and (
            classification.duplicate_count > 0
            or classification.intra_batch_dupe_count > 0
        ):
            sample = []
            for r in valid_rows[:5]:
                if r in classification.new_rows:
                    continue
                sample.append({
                    "date": r.get("Date"),
                    "order_no": r.get("Order No"),
                    "party": r.get("Party Name"),
                    "amount": r.get("Total Amount"),
                })
            raise UploadError(
                "duplicate data detected: "
                f"{classification.duplicate_count} rows already exist in {target!r} "
                f"and {classification.intra_batch_dupe_count} rows are repeated in the "
                f"file itself (intra-batch). "
                f"Re-upload with dedup_mode=skip / replace / append to override. "
                f"Sample of conflicting rows: {sample}"
            )

        # 5b. Preview-only? Return the classification without inserting.
        if preview_only:
            return {
                "preview":          True,
                "filename":         filename,
                "target":           target,
                "dedup_mode":       dedup_mode,
                "errors":           errors,
                "rows_failed":      len(errors),
                "header_row_used":  header,
                "unmatched_headers": unmatched_extras,
                "sheet_name":       sheet_name,
                "dedup":            dedup_summary,
            }

        # 5c. Replace mode: delete the colliding rows first.
        rows_replaced = 0
        if dedup_mode == "replace" and classification.duplicate_hashes:
            rows_replaced = await delete_rows_with_hashes(
                target, classification.duplicate_hashes,
            )

        # 5d. Pick the rows we'll actually insert based on policy.
        if dedup_mode == "skip":
            rows_to_insert = classification.new_rows
            hashes_to_insert = classification.new_row_hashes
        elif dedup_mode == "replace":
            from app.dedup import compute_row_hash
            seen: set[str] = set()
            rows_to_insert = []
            hashes_to_insert = []
            for r in valid_rows:
                h = compute_row_hash(target, r)
                if h in seen:
                    continue
                seen.add(h)
                rows_to_insert.append(r)
                hashes_to_insert.append(h)
        elif dedup_mode == "append":
            from app.dedup import compute_row_hash
            rows_to_insert = valid_rows
            hashes_to_insert = [compute_row_hash(target, r) for r in valid_rows]
        else:
            rows_to_insert = classification.new_rows
            hashes_to_insert = classification.new_row_hashes

        # 6. Database insert (direct, no PIN indirection).
        if batch_id is None:
            batch_id = str(uuid4())

        if not rows_to_insert:
            await record_upload_meta(
                batch_id=batch_id, filename=filename, target=target,
                rows_inserted=0, rows_failed=len(errors), source=source,
                status="active",
                dedup_mode=dedup_mode,
                rows_skipped_duplicate=classification.duplicate_count
                                     + classification.intra_batch_dupe_count,
                rows_replaced=rows_replaced,
            )
            return {
                "batch_id":         batch_id,
                "filename":         filename,
                "target":           target,
                "rows_inserted":    0,
                "rows_failed":      len(errors),
                "rows_skipped_duplicate": classification.duplicate_count
                                       + classification.intra_batch_dupe_count,
                "rows_replaced":    rows_replaced,
                "dedup_mode":       dedup_mode,
                "errors":           errors,
                "summary":          {"total_sales": 0.0, "min_date": "", "max_date": ""},
                "unmatched_headers": unmatched_extras,
                "sheet_name":       sheet_name,
                "header_row_used":  header,
                "validation":       None,
                "dedup":            dedup_summary,
            }

        import asyncio
        rows_inserted = await asyncio.to_thread(
            insert_rows,
            target,
            rows_to_insert,
            batch_id=batch_id,
            source=source,
            file_name=filename,
            row_hashes=hashes_to_insert,
        )

        # 7. PostValidator - fails loud if dashboard would break.
        try:
            validation = await _validate_post_insert(target, batch_id)
        except UploadError:
            try:
                async with get_connection() as conn:
                    await conn.execute(
                        f'DELETE FROM {quoted(target)} WHERE batch_id = ?',
                        (batch_id,),
                    )
                    await conn.commit()
            except Exception:
                _dataclean_log.exception(
                    "rollback of failed batch %s failed", batch_id,
                )
            raise

        rows_failed = len(errors)
        await record_upload_meta(
            batch_id=batch_id,
            filename=filename,
            target=target,
            rows_inserted=rows_inserted,
            rows_failed=rows_failed,
            source=source,
            status="active",
            min_date=validation.get("min_date"),
            max_date=validation.get("max_date"),
            dedup_mode=dedup_mode,
            rows_skipped_duplicate=classification.duplicate_count
                                 + classification.intra_batch_dupe_count,
            rows_replaced=rows_replaced,
        )

        summary = {
            "total_sales": round(
                sum(float(r.get("Total Amount") or 0) for r in rows_to_insert), 2
            ),
            "min_date": validation["min_date"],
            "max_date": validation["max_date"],
        }

        return {
            "batch_id":         batch_id,
            "filename":         filename,
            "target":           target,
            "rows_inserted":    rows_inserted,
            "rows_failed":      rows_failed,
            "rows_skipped_duplicate": classification.duplicate_count
                                   + classification.intra_batch_dupe_count,
            "rows_replaced":    rows_replaced,
            "dedup_mode":       dedup_mode,
            "errors":           errors,
            "summary":          summary,
            "unmatched_headers": unmatched_extras,
            "sheet_name":       sheet_name,
            "header_row_used":  header,
            "validation":       validation,
            "dedup":            dedup_summary,
        }


__all__ = [
    "DashboardAgent",
    "DataCleanAgent",
    "normalize_row",
    "validate_row",
]
