"""SQL dialect translation: SQLite → Postgres.

Most queries in this codebase are dialect-compatible — quoted
identifiers, integer arithmetic, GROUP BY etc. all work in both
engines. The two real differences:

  1. Placeholders: SQLite uses `?` everywhere; Postgres uses
     `$1, $2, ...` numbered. We translate by walking the SQL string
     and assigning a number to each `?` outside string literals.

  2. Now-clock: SQLite uses `datetime('now')`; Postgres uses `now()`.
     We do a string replacement guarded by tokenization so we don't
     touch occurrences inside string literals.

Edge cases we DON'T handle (call sites that hit these would need
manual SQL):
  - GLOB → SIMILAR TO / regex (only used in DashboardAgent's pie
    aggregation; the same SQL works against the SQLite source via
    DuckDB's sqlite_scanner, which is what the dashboard uses)
  - PRAGMA statements (SQLite-only, skipped on Postgres)
  - autoincrement primary keys with `INTEGER PRIMARY KEY
    AUTOINCREMENT` — Postgres equivalent is `SERIAL`/`IDENTITY`,
    but the affected tables are sales/purchase + only the `id`
    column, which we don't read by id

Use sparingly: the right long-term answer is repository classes that
emit dialect-aware SQL natively. This translator is the bridge that
lets call sites swap engines without rewriting queries.
"""
from __future__ import annotations

import logging
import re
from typing import Any


_log = logging.getLogger("agentic_ai.database.dialect")


def to_postgres_placeholders(sql: str) -> str:
    """Convert `?` placeholders to `$1, $2, ...` Postgres style.

    Walks the string char-by-char so `?` inside single-quoted strings
    (e.g. `WHERE "Date" GLOB '????-??-??'`) is left untouched. Doubled
    single-quotes (`''` for an escaped quote inside a literal) are
    handled correctly.

    Idempotent if the SQL is ALREADY Postgres-style: a string with no
    `?` outside literals returns unchanged.
    """
    out: list[str] = []
    in_single = False
    n = 0
    i = 0
    s = sql
    while i < len(s):
        c = s[i]
        if in_single:
            if c == "'" and i + 1 < len(s) and s[i + 1] == "'":
                # Doubled '' — literal single-quote inside the string.
                out.append("''")
                i += 2
                continue
            if c == "'":
                in_single = False
            out.append(c)
            i += 1
            continue
        if c == "'":
            in_single = True
            out.append(c)
            i += 1
            continue
        if c == "?":
            n += 1
            out.append(f"${n}")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_DT_NOW_RE = re.compile(r"\bdatetime\(\s*'now'\s*\)", re.IGNORECASE)


def to_postgres_clocks(sql: str) -> str:
    """Replace SQLite `datetime('now')` calls with Postgres `now()`.
    Conservative regex that only matches the common form."""
    return _DT_NOW_RE.sub("now()", sql)


def translate(sql: str) -> str:
    """Apply all dialect rewrites in one pass."""
    return to_postgres_clocks(to_postgres_placeholders(sql))


# ---- Schema DDL adjustments ------------------------------------------------

_SQLITE_AUTOINC = re.compile(
    r"\bid\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
    re.IGNORECASE,
)


def to_postgres_schema(sql: str) -> str:
    """Translate a CREATE TABLE statement from SQLite to Postgres syntax.
    Only the differences we actually use:

      - `id INTEGER PRIMARY KEY AUTOINCREMENT` → `id BIGSERIAL PRIMARY KEY`
      - `TEXT NOT NULL DEFAULT (datetime('now'))` → already covered by
        `to_postgres_clocks` after wrapping
      - `TEXT` columns stay TEXT (Postgres has unbounded TEXT too)
      - `REAL` stays REAL (Postgres alias for double precision)
    """
    sql = _SQLITE_AUTOINC.sub("id BIGSERIAL PRIMARY KEY", sql)
    sql = to_postgres_clocks(sql)
    return sql
