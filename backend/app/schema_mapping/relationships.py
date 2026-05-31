"""Cross-table relationship detection.

After every workbook upload we scan the freshly-ingested ``u_*`` tables
for shared JOIN keys: column-name pairs that appear in two tables AND
whose value sets overlap by ≥ 80 % in the smaller table. The detected
relationships go into ``_relationships`` and are surfaced to the LLM by
the Schema tool as an explicit ``## TABLE RELATIONSHIPS`` block — so
cross-table questions ("sales by brand", "inventory by location") get
correct JOINs instead of guessed ones.

Postgres-only. The legacy SQLite path keeps relying on the hand-written
``## METRIC DEFINITIONS`` block in ``schema.py`` for the SKU join.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("agentic_ai.relationships")

_SYSTEM_COLS: frozenset[str] = frozenset({
    "_id", "_batch_id", "_source_sheet", "_inserted_at",
})

# Column names that we DO NOT treat as candidate join keys even if they
# match between two tables. These are either too generic (``id``, ``no``)
# or are universal labels (``date``, ``name``, ``description``) whose
# matching by name is meaningless.
_BLOCKLIST: frozenset[str] = frozenset({
    "id", "no", "num", "number", "name", "description", "desc",
    "date", "day", "month", "year", "time", "timestamp",
    "status", "type", "kind", "category", "label",
    "amount", "value", "qty", "quantity", "price", "cost",
    "total", "subtotal", "tax", "discount", "rate",
    "notes", "note", "remarks", "comment",
})

_OVERLAP_THRESHOLD = 0.80


def _col_key(name: str) -> str:
    """Normalise a column name for cross-table matching: lowercase,
    spaces/hyphens → underscores, strip surrounding underscores."""
    return re.sub(r"[\s\-]+", "_", name.lower()).strip("_")


@dataclass(frozen=True)
class Relationship:
    """One detected JOIN key between two tables.

    ``from_table`` / ``to_table`` are stored in alphabetical order so
    each pair is recorded once regardless of detection direction.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    overlap_pct: float            # fraction of the SMALLER side covered
    from_distinct: int
    to_distinct: int


async def detect_relationships_pg(
    tables: list[dict[str, Any]],
) -> list[Relationship]:
    """Find every shared-name column pair across ``u_*`` tables whose
    value sets overlap by ≥ 80 % in the smaller table.

    ``tables`` is the shape ``schema._list_user_tables()`` produces:
    ``[{"table": str, "columns": [{"name": str, "type": str}, ...]}, ...]``.
    Returns a list of ``Relationship`` records, alphabetised by
    ``(from_table, from_column, to_table)`` so output is deterministic.
    """
    from app.db_engine import pg_connection

    out: list[Relationship] = []
    if len(tables) < 2:
        return out

    # Group columns by normalised name → {key: [(table, original_name), ...]}.
    by_key: dict[str, list[tuple[str, str]]] = {}
    for t in tables:
        tname = t.get("table")
        if not tname:
            continue
        for c in t.get("columns", []):
            cname = c.get("name")
            if not cname or cname in _SYSTEM_COLS:
                continue
            k = _col_key(cname)
            if not k or k in _BLOCKLIST:
                continue
            by_key.setdefault(k, []).append((tname, cname))

    # Candidate pairs: same normalised key appears in ≥2 tables.
    candidate_pairs: list[tuple[str, str, str, str]] = []
    for k, holders in by_key.items():
        if len(holders) < 2:
            continue
        # Generate every (a, b) with a's table < b's table (alphabetical),
        # so each pair appears once regardless of detection direction.
        unique = sorted(set(holders))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                a_table, a_col = unique[i]
                b_table, b_col = unique[j]
                if a_table == b_table:
                    continue  # same table self-reference
                candidate_pairs.append((a_table, a_col, b_table, b_col))

    if not candidate_pairs:
        return out

    async with pg_connection() as db:
        for a_table, a_col, b_table, b_col in candidate_pairs:
            qa = f'"{a_col}"'
            qb = f'"{b_col}"'
            # Three small queries: distinct(a), distinct(b), and the
            # overlap count (distinct values present in both). Storing
            # both directions inflates the table; one canonical row per
            # pair is enough — the schema-tool consumer iterates both
            # directions when emitting JOIN hints.
            try:
                cur = await db.execute(
                    f"SELECT COUNT(DISTINCT {qa}) AS n "
                    f'FROM "{a_table}" WHERE {qa} IS NOT NULL'
                )
                r = await cur.fetchone()
                a_n = int(dict(r)["n"]) if r else 0

                cur = await db.execute(
                    f"SELECT COUNT(DISTINCT {qb}) AS n "
                    f'FROM "{b_table}" WHERE {qb} IS NOT NULL'
                )
                r = await cur.fetchone()
                b_n = int(dict(r)["n"]) if r else 0

                if a_n == 0 or b_n == 0:
                    continue

                # Overlap = distinct values present in BOTH tables. The
                # subquery is the cheap one — Postgres can hash-join
                # both DISTINCTs cheaply for small u_* sheets.
                cur = await db.execute(
                    f"SELECT COUNT(*) AS n FROM ("
                    f"  SELECT DISTINCT {qa} AS v "
                    f'  FROM "{a_table}" WHERE {qa} IS NOT NULL'
                    f") a INNER JOIN ("
                    f"  SELECT DISTINCT {qb} AS v "
                    f'  FROM "{b_table}" WHERE {qb} IS NOT NULL'
                    f") b ON a.v::text = b.v::text"
                )
                r = await cur.fetchone()
                overlap_n = int(dict(r)["n"]) if r else 0
            except Exception as e:
                log.warning(
                    "relationships: probe %s.%s ↔ %s.%s failed: %s",
                    a_table, a_col, b_table, b_col, e,
                )
                continue

            smaller = min(a_n, b_n)
            overlap_pct = overlap_n / smaller if smaller else 0.0
            if overlap_pct >= _OVERLAP_THRESHOLD:
                out.append(Relationship(
                    from_table=a_table,
                    from_column=a_col,
                    to_table=b_table,
                    to_column=b_col,
                    overlap_pct=round(overlap_pct, 4),
                    from_distinct=a_n,
                    to_distinct=b_n,
                ))
    return out


async def save_relationships_pg(rels: list[Relationship]) -> int:
    """Replace the current relationships set with ``rels``.

    Truncates the ``_relationships`` table first so removed tables /
    relationships don't linger — the detector is run after every
    upload and the result is the authoritative set.

    Returns the number of relationships saved.
    """
    from app.db_engine import pg_connection
    async with pg_connection() as db:
        async with db.transaction():
            await db.execute("DELETE FROM _relationships")
            if not rels:
                return 0
            sql = (
                "INSERT INTO _relationships "
                "(from_table, from_column, to_table, to_column, "
                " overlap_pct, from_distinct, to_distinct, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NOW())"
            )
            payload = [
                (r.from_table, r.from_column, r.to_table, r.to_column,
                 r.overlap_pct, r.from_distinct, r.to_distinct)
                for r in rels
            ]
            await db.executemany(sql, payload)
    return len(rels)


async def load_relationships_pg() -> list[dict[str, Any]]:
    """Return every saved relationship as a list of dicts. Empty list when
    the table is empty or the load fails (best-effort)."""
    from app.db_engine import pg_connection
    out: list[dict[str, Any]] = []
    async with pg_connection() as db:
        try:
            cur = await db.execute(
                "SELECT from_table, from_column, to_table, to_column, "
                "  overlap_pct, from_distinct, to_distinct "
                "FROM _relationships "
                "ORDER BY from_table, from_column, to_table"
            )
            rows = await cur.fetchall()
        except Exception as e:
            log.warning("relationships: load failed: %s", e)
            return out
        for r in rows or []:
            d = dict(r)
            out.append({
                "from_table":    d.get("from_table"),
                "from_column":   d.get("from_column"),
                "to_table":      d.get("to_table"),
                "to_column":     d.get("to_column"),
                "overlap_pct":   float(d.get("overlap_pct") or 0.0),
                "from_distinct": int(d.get("from_distinct") or 0),
                "to_distinct":   int(d.get("to_distinct") or 0),
            })
    return out


async def _list_pg_user_tables() -> list[dict[str, Any]]:
    """Compact listing of every ``u_*`` table + its columns for the detector.

    Local copy of the schema-tool helper so this module doesn't depend on
    the coordinator package. Only the fields the detector reads.
    """
    from app.db_engine import pg_connection
    out: list[dict[str, Any]] = []
    async with pg_connection() as db:
        cur = await db.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name LIKE 'u\\_%' ESCAPE '\\' "
            "ORDER BY table_name"
        )
        rows = await cur.fetchall()
        for r in rows or []:
            name = dict(r).get("name") or ""
            if not name:
                continue
            cur2 = await db.execute(
                "SELECT column_name AS name "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? "
                "ORDER BY ordinal_position",
                (name,),
            )
            cols = await cur2.fetchall()
            col_defs = [
                {"name": dict(c).get("name") or "", "type": "TEXT"}
                for c in (cols or [])
            ]
            out.append({"table": name, "columns": col_defs})
    return out


async def refresh_relationships_pg() -> int:
    """Recompute relationships across every ``u_*`` table and replace the
    contents of ``_relationships``. Returns the number of relationships
    saved. Best-effort: any internal failure is logged + swallowed so an
    upload never fails because of relationship detection."""
    try:
        tables = await _list_pg_user_tables()
    except Exception:
        log.warning("relationships: list_user_tables failed", exc_info=True)
        return 0
    if not tables:
        return 0
    try:
        rels = await detect_relationships_pg(tables)
    except Exception:
        log.warning("relationships: detection failed", exc_info=True)
        return 0
    try:
        return await save_relationships_pg(rels)
    except Exception:
        log.warning("relationships: save failed", exc_info=True)
        return 0


__all__ = [
    "Relationship",
    "detect_relationships_pg",
    "load_relationships_pg",
    "refresh_relationships_pg",
    "save_relationships_pg",
]
