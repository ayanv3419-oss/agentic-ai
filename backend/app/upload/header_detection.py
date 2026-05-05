"""Header-row detection — shared by CSV + XLSX parsers and by DataCleanAgent.

Two responsibilities:
  • Locate the *real* header row in a file whose first rows may be metadata,
    title text, blank rows, or merged cells. The header row is the one that
    contains canonical aliases for ALL required columns.
  • Provide the same alias resolution that DataCleanAgent uses, so we have
    a single source of truth for "is this string a valid header?".

Both are pure functions on cell lists — no I/O, no side effects.
"""
from __future__ import annotations

from typing import Any

from app.database.schema import HEADER_ALIASES, REQUIRED_COLUMNS


# --- Normalization ---------------------------------------------------------

def normalize_key(s: Any) -> str:
    """Lowercase, replace any non-alphanumeric char with space, collapse whitespace.

    Examples:
      'TOTAL AMOUNT '        -> 'total amount'
      'Total.Amount'         -> 'total amount'
      'Party-Phone-No.'      -> 'party phone no'
      'Received / Paid Amt'  -> 'received paid amt'
    """
    raw = str(s if s is not None else "").lower()
    cleaned_chars = [
        ch if ch.isalnum() or ch.isspace() else " "
        for ch in raw
    ]
    return " ".join("".join(cleaned_chars).split())


# --- Alias index (built once at import) ------------------------------------

_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _aliases in HEADER_ALIASES.items():
    _ALIAS_INDEX[normalize_key(_canonical)] = _canonical
    for _alias in _aliases:
        _ALIAS_INDEX[normalize_key(_alias)] = _canonical


def alias_lookup(raw: Any) -> str | None:
    """Return the canonical column name for a raw header cell, or None."""
    if raw is None:
        return None
    return _ALIAS_INDEX.get(normalize_key(raw))


# --- Scoring + detection ---------------------------------------------------

def score_row_as_header(
    cells: list[Any],
) -> tuple[int, dict[str, str], list[str]]:
    """Score how well `cells` could serve as a header row.

    Returns:
      score            — required matches × 100 + optional matches × 1
                         (so any row missing a REQUIRED column scores < 100)
      header_index     — raw_cell_string → canonical column name
      missing_required — REQUIRED columns not matched in this row
    """
    seen_canonical: set[str] = set()
    header_index: dict[str, str] = {}
    for raw in cells:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        canonical = _ALIAS_INDEX.get(normalize_key(s))
        if canonical is None:
            continue
        if canonical in seen_canonical:
            # First occurrence wins — duplicate header columns are reported as extras.
            continue
        header_index[s] = canonical
        seen_canonical.add(canonical)

    required_matched = sum(1 for c in REQUIRED_COLUMNS if c in seen_canonical)
    optional_matched = len(seen_canonical) - required_matched
    score = required_matched * 100 + optional_matched
    missing = [c for c in REQUIRED_COLUMNS if c not in seen_canonical]
    return score, header_index, missing


def find_header_row(
    rows_buffer: list[list[Any]],
) -> tuple[int, list[Any], dict[str, str]]:
    """Pick the best valid header row from a small buffer of candidate rows.

    A row is *valid* iff it matches all REQUIRED columns. Among valid rows,
    the one with the highest score wins; ties broken by earliest row.

    Raises ValueError with a diagnostic sample if no row qualifies.
    """
    candidates: list[tuple[int, int, list[Any], dict[str, str]]] = []
    for idx, row in enumerate(rows_buffer):
        if row is None:
            continue
        score, header_index, missing = score_row_as_header(list(row))
        if missing:
            continue
        candidates.append((score, idx, list(row), header_index))

    if not candidates:
        sample = []
        for i, row in enumerate(rows_buffer[:5]):
            preview = [str(c)[:30] for c in (row or [])][:8]
            sample.append(f"row {i + 1}: {preview}")
        raise ValueError(
            f"No row in the first {len(rows_buffer)} contained all required "
            f"columns ({REQUIRED_COLUMNS}). Sample: {' | '.join(sample)}"
        )

    candidates.sort(key=lambda x: (-x[0], x[1]))
    score, idx, header_cells, header_index = candidates[0]
    return idx, header_cells, header_index


def map_headers_strict(
    header: list[str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Map an already-chosen header row to canonical columns.

    Returns (header_index, missing_required, unmatched_extras).
    """
    seen_canonical: set[str] = set()
    header_index: dict[str, str] = {}
    unmatched: list[str] = []
    for raw in header:
        if not raw:
            continue
        canonical = _ALIAS_INDEX.get(normalize_key(raw))
        if canonical is None:
            unmatched.append(raw)
            continue
        if canonical in seen_canonical:
            unmatched.append(raw)
            continue
        header_index[raw] = canonical
        seen_canonical.add(canonical)
    missing = [c for c in REQUIRED_COLUMNS if c not in seen_canonical]
    return header_index, missing, unmatched
