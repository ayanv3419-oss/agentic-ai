"""DataCleanAgent — NO LLM. Deterministic ingestion pipeline.

Stages:
  1. FileParser     — auto-detects header row (handles ERP exports with
                      title rows / blank rows / multi-sheet XLSX).
  2. HeaderMapper   — closed alias map (shared with header_detection); reject
                      file when REQUIRED columns aren't all matched.
  3. DataNormalizer — per-row coercion: Date→YYYY-MM-DD, REAL→float,
                      TEXT→stripped string. Optional missing → NULL.
  4. RowValidator   — required fields parsed correctly; bad rows recorded.
  5. Database       — restricted insert via the Database tool + INGESTION_PIN.
  6. PostValidator  — checks the new batch in the DB:
                        * at least 1 row landed
                        * 0 NULL Date / Total Amount rows
                        * MIN/MAX Date sane
                      If any check fails → UploadError (no fake success).
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date_cls, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.database import (
    ALLOWED_TABLES,
    COLUMN_TYPES,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    SCHEMA_COLUMNS,
    fetch_one,
    quoted,
    record_upload_meta,
)
from app.tools import get_registry
from app.tools.database import INGESTION_PIN
from app.upload.parser import (
    UploadError,
    stream_parse_csv_with_detection,
    stream_parse_xlsx_with_detection,
)
from app.upload.header_detection import map_headers_strict

log = logging.getLogger("agentic_ai.agents.dataclean")


# ---------- DataNormalizer --------------------------------------------------

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


_AMOUNT_STRIP = (",", "₹", "$", "Rs.", "Rs", " ")


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
    """Map raw cells to canonical column dict (canonical → typed value).
    Optional missing → None (NULL in DB)."""
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


# ---------- RowValidator ----------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


# ---------- PostValidator ---------------------------------------------------

async def _validate_post_insert(target: str, batch_id: str) -> dict[str, Any]:
    """Run the dashboard-integrity sanity check on the just-inserted batch.

    Raises UploadError on any anomaly so the upload fails LOUD, never silently.
    """
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
            f"— dashboard would break"
        )
    if null_amts:
        raise UploadError(
            f"post-insert validation: {null_amts}/{n} rows have NULL Total Amount "
            f"— dashboard aggregation would break"
        )
    min_d = row.get("min_date") or ""
    max_d = row.get("max_date") or ""
    if not (_ISO_DATE_RE.match(str(min_d)) and _ISO_DATE_RE.match(str(max_d))):
        raise UploadError(
            f"post-insert validation: Date range invalid (min={min_d!r}, max={max_d!r})"
        )
    return {"batch_rows": n, "min_date": min_d, "max_date": max_d}


# ---------- DataCleanAgent --------------------------------------------------

class DataCleanAgent:
    """No LLM. Orchestrates parsing + normalization + insertion + validation."""

    name = "DataCleanAgent"

    async def run(
        self,
        *,
        tmp_path: Path,
        filename: str,
        target: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        if target not in ALLOWED_TABLES:
            raise UploadError(f"target must be one of {ALLOWED_TABLES!r}")
        suffix = tmp_path.suffix.lower()

        # 1. FileParser — auto header detection.
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

        # 2. HeaderMapper — verify and compute extras.
        _, missing_required, unmatched_extras = map_headers_strict(header)
        if missing_required:
            # find_header_row should have prevented this, but defense in depth.
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
                f"no valid rows after parsing — {rows_seen} rows seen, "
                f"{len(errors)} rejected. First reason: "
                f"{errors[0]['reason'] if errors else '(no rows in file)'}"
            )

        # 5. Database (restricted).
        if batch_id is None:
            batch_id = str(uuid4())
        registry = get_registry()
        from app.state import TurnState  # local import — avoid cycle
        dummy_state = TurnState(question="<dataclean>")
        result = await registry.execute(
            "Database",
            {
                "op":        "insert",
                "pin":       INGESTION_PIN,
                "table":     target,
                "rows":      valid_rows,
                "batch_id":  batch_id,
                "source":    "upload",
                "file_name": filename,
            },
            dummy_state,
        )
        if not result.ok:
            raise UploadError(f"database insert failed: {result.error}")
        rows_inserted = int((result.output or {}).get("rows_inserted") or 0)

        # 6. PostValidator — fails loud if dashboard would break.
        try:
            validation = await _validate_post_insert(target, batch_id)
        except UploadError:
            # Roll back this batch_id so a partial insert doesn't pollute the DB.
            from app.database import get_connection
            try:
                async with get_connection() as conn:
                    await conn.execute(
                        f'DELETE FROM {quoted(target)} WHERE batch_id = ?', (batch_id,)
                    )
                    await conn.commit()
            except Exception:
                log.exception("rollback of failed batch %s failed", batch_id)
            raise

        rows_failed = len(errors)
        await record_upload_meta(
            batch_id=batch_id,
            filename=filename,
            target=target,
            rows_inserted=rows_inserted,
            rows_failed=rows_failed,
            source="upload",
            status="active",
            min_date=validation.get("min_date"),
            max_date=validation.get("max_date"),
        )

        summary = {
            "total_sales": round(
                sum(float(r.get("Total Amount") or 0) for r in valid_rows), 2
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
            "errors":           errors,
            "summary":          summary,
            "unmatched_headers": unmatched_extras,
            "sheet_name":       sheet_name,
            "header_row_used":  header,
            "validation":       validation,
        }
