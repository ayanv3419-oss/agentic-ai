"""DataFrame bridge — compact converters between the row-dict style we use
elsewhere and Polars/Arrow when a caller wants them.

Polars is NOT a hard dependency. The bridge functions are written so they
work without Polars present (returning plain row-dicts) but also accept
a Polars DataFrame when one is available. This keeps the option open for
a caller in the analytics pipeline to ingest a CSV via Polars (very fast)
without forcing every deployment to install it.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

_log = logging.getLogger("agentic_ai.acceleration.bridge")


def sqlite_rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """aiosqlite + DuckDB return slightly different row shapes (Row vs
    tuple). This normaliser produces list[dict] regardless. Idempotent —
    if the input already has dict-shaped rows, returns it as a list."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(dict(r))
        elif hasattr(r, "keys"):
            out.append({k: r[k] for k in r.keys()})
        else:
            # tuple / list — caller is responsible for column names elsewhere.
            out.append({i: v for i, v in enumerate(r)})
    return out


def rows_to_polars_compatible(rows: list[dict[str, Any]]) -> Any:
    """Return a Polars DataFrame when polars is installed; otherwise return
    the input list[dict] unchanged. Callers that want guaranteed Polars
    behaviour should `import polars as pl` themselves and add it to
    requirements — the bridge is intentionally optional."""
    try:
        import polars as pl  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _log.debug("polars not installed; returning list[dict] unchanged")
        return rows
    return pl.DataFrame(rows)
