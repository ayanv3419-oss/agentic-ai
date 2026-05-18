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


class DashboardAgent:
    """Reads sales rows, verifies dates, filters, groups, aggregates.
    Output matches the frontend's DashboardData type exactly:
      { month, kpis: {total_sales, orders, customers},
        series: [{bucket, sales, orders}], monthly_sales_pie: [...] }
    """

    name = "DashboardAgent"

    async def run(self, *, month: str | None = None) -> dict[str, Any]:
        where_parts: list[str] = []
        params: list[Any] = []
        if month is not None:
            if not (len(month) == 7 and month[4] == "-"):
                raise ValueError(f"invalid month format: {month!r}")
            where_parts.append('"Date" LIKE ?')
            params.append(f"{month}-%")
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = (
            f'SELECT "Date", "Total Amount", "Party Name" '
            f'FROM {quoted("sales")}'
            f'{where_sql}'
        )
        raw_rows = await fetch_all(sql, params)

        valid: list[dict[str, Any]] = []
        skipped = 0
        for r in raw_rows:
            d = r.get("Date")
            if not isinstance(d, str) or not _ISO_DATE_RE.match(d):
                skipped += 1
                continue
            valid.append(r)
        if skipped:
            _dashboard_log.warning(
                "DashboardAgent skipped %d rows with non-ISO Date", skipped,
            )

        if month:
            valid = [r for r in valid if r["Date"].startswith(month)]

        buckets: dict[str, dict[str, float]] = defaultdict(
            lambda: {"sales": 0.0, "orders": 0}
        )
        total_sales = 0.0
        orders = 0
        customers: set[str] = set()
        for r in valid:
            d = r["Date"]
            amt = float(r.get("Total Amount") or 0)
            buckets[d]["sales"] += amt
            buckets[d]["orders"] = int(buckets[d]["orders"]) + 1
            total_sales += amt
            orders += 1
            party = r.get("Party Name")
            if isinstance(party, str) and party:
                customers.add(party)

        series = [
            {"bucket": d, "sales": round(b["sales"], 2), "orders": int(b["orders"])}
            for d, b in sorted(buckets.items(), key=lambda x: x[0])
        ][:366]

        monthly_sales_pie = await self._aggregate_monthly_sales_pie()

        return {
            "month": month,
            "kpis": {
                "total_sales": round(total_sales, 2),
                "orders":      orders,
                "customers":   len(customers),
            },
            "series": series,
            "monthly_sales_pie": monthly_sales_pie,
        }

    @staticmethod
    async def _aggregate_monthly_sales_pie() -> list[dict[str, Any]]:
        """SQL GROUP BY year-month -> SUM(Total Amount)."""
        pie_sql = (
            f'SELECT '
            f'  substr("Date", 1, 7) AS ym, '
            f'  SUM("Total Amount") AS sales '
            f'FROM {quoted("sales")} '
            f'WHERE "Date" IS NOT NULL '
            f'  AND "Date" GLOB \'????-??-??\' '
            f'  AND "Total Amount" IS NOT NULL '
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
            sales = float(r.get("sales") or 0)
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
