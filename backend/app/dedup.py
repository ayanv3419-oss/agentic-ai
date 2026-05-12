"""Persistent-data deduplication engine.

Two layers of detection:

  1. File-level — SHA256 over the raw uploaded bytes. If an existing
     active upload (uploads.status = 'active') has the same hash, the
     re-upload is rejected (default) or rerouted per the user's policy.

  2. Row-level — SHA256 over the canonical "identity" fields of each
     parsed row. The row_hash column on sales/purchase is indexed, so
     "have we seen this row before?" is a single hash-lookup.

The row hash field set is chosen so that the same business event hashes
to the same value regardless of cosmetic differences (whitespace,
trailing zeros). Today the identity is:

    target | Date | Order No | Invoice No | Party Name |
    Product Name | normalized(Total Amount)

Re-uploading the same Excel file => identical row hashes => zero new rows.
Uploading a corrected file where Total Amount changed => different hash
=> the row is classified as 'new' (or 'changed' when (Date, Order No,
Invoice No, Party Name, Product Name) match an existing row but the
amount differs).

Resolution policies expressed as `dedup_mode`:

    block    — reject the whole upload if any duplicate is detected (DEFAULT)
    skip     — insert only the new rows; never touch existing rows
    replace  — delete existing rows whose row_hash collides, then insert
               the new ones (effectively idempotent re-import)
    append   — insert everything regardless (legacy behaviour)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.infrastructure import (
    ALLOWED_TABLES,
    fetch_all,
    quoted,
)


_log = logging.getLogger("agentic_ai.dedup")


DEDUP_MODES = ("block", "skip", "replace", "append")
DEFAULT_DEDUP_MODE = "block"

# Identity fields participating in the row hash. Only these matter for
# "is this the same business event?". Total Amount IS included so that
# correcting an amount produces a different hash (otherwise corrected
# rows would silently dedupe).
_ROW_IDENTITY_FIELDS: tuple[str, ...] = (
    "Date",
    "Order No",
    "Invoice No",
    "Party Name",
    "Product Name",
    "Total Amount",
)


# ===========================================================================
# Hashing
# ===========================================================================

def compute_file_hash(path: str | Path, *, chunk: int = 1 << 20) -> tuple[str, int]:
    """SHA256 the file at `path` in 1 MB chunks. Returns (hex_digest, byte_count)."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
            total += len(buf)
    return h.hexdigest(), total


def _normalize_field(name: str, value: Any) -> str:
    """Canonicalize a field value for inclusion in the row hash.

    - None / empty string both → ""
    - Numerics → trimmed string with trailing zeros stripped after the dot
    - Strings → trimmed + lowercased
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        # Format consistently regardless of int vs float origin.
        if isinstance(value, float):
            # Strip trailing zeros: 100.0 → "100", 1.50 → "1.5"
            s = f"{value:.6f}".rstrip("0").rstrip(".")
        else:
            s = str(value)
        return s
    s = str(value).strip()
    # Field-specific normalization
    if name == "Date":
        return s[:10]  # YYYY-MM-DD prefix
    return s.lower()


def compute_row_hash(target: str, row: dict[str, Any]) -> str:
    """Deterministic SHA256 over the identity fields of a canonical row.

    `target` is the table name ('sales' | 'purchase'); included in the
    hash so a sales row and a purchase row that look identical never
    collide across tables.
    """
    if target not in ALLOWED_TABLES:
        raise ValueError(f"unknown target: {target!r}")
    parts = [target] + [
        _normalize_field(f, row.get(f)) for f in _ROW_IDENTITY_FIELDS
    ]
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ===========================================================================
# Database scans
# ===========================================================================

async def existing_row_hashes_for(target: str, hashes: Iterable[str]) -> set[str]:
    """Return the subset of `hashes` that already exist in `target`.

    Chunked into batches of 500 to stay under SQLite's parameter limit.
    """
    if target not in ALLOWED_TABLES:
        raise ValueError(f"unknown target: {target!r}")
    pool = [h for h in hashes if h]
    if not pool:
        return set()

    found: set[str] = set()
    table = quoted(target)
    for i in range(0, len(pool), 500):
        chunk = pool[i:i + 500]
        placeholders = ",".join(["?"] * len(chunk))
        rows = await fetch_all(
            f"SELECT DISTINCT row_hash FROM {table} "
            f"WHERE row_hash IN ({placeholders})",
            chunk,
        )
        found.update(r["row_hash"] for r in rows if r.get("row_hash"))
    return found


async def existing_row_count_for_hashes(target: str, hashes: Iterable[str]) -> int:
    """How many rows in `target` carry one of the given hashes? Used for
    `replace` mode to report rows_replaced."""
    if target not in ALLOWED_TABLES:
        return 0
    pool = [h for h in hashes if h]
    if not pool:
        return 0
    table = quoted(target)
    total = 0
    for i in range(0, len(pool), 500):
        chunk = pool[i:i + 500]
        placeholders = ",".join(["?"] * len(chunk))
        rows = await fetch_all(
            f"SELECT COUNT(*) AS n FROM {table} "
            f"WHERE row_hash IN ({placeholders})",
            chunk,
        )
        if rows:
            total += int(rows[0].get("n") or 0)
    return total


async def delete_rows_with_hashes(target: str, hashes: Iterable[str]) -> int:
    """Delete every row whose row_hash matches one in `hashes`. Returns
    the number of rows deleted. Used by `replace` mode before inserting
    the corrected versions."""
    if target not in ALLOWED_TABLES:
        return 0
    pool = [h for h in hashes if h]
    if not pool:
        return 0

    # We use a synchronous sqlite3 transaction to mirror insert_rows()'
    # write path (faster + single commit).
    import sqlite3
    from app.infrastructure import db_path

    table = quoted(target)
    deleted = 0
    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")
        for i in range(0, len(pool), 500):
            chunk = pool[i:i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            cur = conn.execute(
                f"DELETE FROM {table} WHERE row_hash IN ({placeholders})",
                chunk,
            )
            deleted += cur.rowcount or 0
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _log.exception("delete_rows_with_hashes failed")
        deleted = 0
    finally:
        conn.close()
    return deleted


# ===========================================================================
# Classification API
# ===========================================================================

@dataclass
class DedupClassification:
    """Per-batch classification produced by classify_batch()."""
    target: str
    mode: str
    total_parsed: int
    new_rows: list[dict[str, Any]] = field(default_factory=list)
    new_row_hashes: list[str] = field(default_factory=list)
    duplicate_hashes: set[str] = field(default_factory=set)
    duplicate_count: int = 0           # number of incoming rows that are dupes
    intra_batch_dupe_count: int = 0    # rows that duplicate another row in the SAME file

    def summary(self) -> dict[str, Any]:
        return {
            "target":               self.target,
            "mode":                 self.mode,
            "rows_in_file":         self.total_parsed,
            "rows_new":             len(self.new_rows),
            "rows_duplicate":       self.duplicate_count,
            "rows_intra_batch_dupe": self.intra_batch_dupe_count,
            "existing_hash_hits":   len(self.duplicate_hashes),
        }


async def classify_batch(
    target: str,
    rows: list[dict[str, Any]],
    *,
    mode: str = DEFAULT_DEDUP_MODE,
) -> DedupClassification:
    """Split `rows` into `new` vs `duplicate` based on row_hash lookups.

    For `mode in ('block', 'skip', 'replace')`: duplicate rows are
    excluded from the `new_rows` list and reported on the result.

    For `mode == 'append'`: every row is treated as new (no dedup work
    beyond computing the hashes for later auditing).

    Intra-batch duplicates (same hash appearing twice in the SAME file)
    are deduped down to one entry regardless of mode — uploading a CSV
    with literal duplicate rows should never multiply them in the DB.
    """
    if mode not in DEDUP_MODES:
        raise ValueError(f"unknown dedup mode: {mode!r}")
    if target not in ALLOWED_TABLES:
        raise ValueError(f"unknown target: {target!r}")

    classification = DedupClassification(target=target, mode=mode, total_parsed=len(rows))
    if not rows:
        return classification

    # Compute hashes
    all_hashes = [compute_row_hash(target, r) for r in rows]

    # Drop intra-batch duplicates first (keep the first occurrence)
    seen_in_batch: set[str] = set()
    deduped_rows: list[dict[str, Any]] = []
    deduped_hashes: list[str] = []
    for r, h in zip(rows, all_hashes):
        if h in seen_in_batch:
            classification.intra_batch_dupe_count += 1
            continue
        seen_in_batch.add(h)
        deduped_rows.append(r)
        deduped_hashes.append(h)

    if mode == "append":
        # No DB scan; everything is "new"
        classification.new_rows = deduped_rows
        classification.new_row_hashes = deduped_hashes
        return classification

    # DB scan: which of the incoming hashes already exist?
    existing = await existing_row_hashes_for(target, deduped_hashes)
    classification.duplicate_hashes = existing

    for r, h in zip(deduped_rows, deduped_hashes):
        if h in existing:
            classification.duplicate_count += 1
            continue
        classification.new_rows.append(r)
        classification.new_row_hashes.append(h)

    return classification
