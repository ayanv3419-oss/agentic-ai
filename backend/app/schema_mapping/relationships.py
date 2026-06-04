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
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Cross-name key concepts. The same-name pass can only relate columns that
# share a normalised name (sku_id == sku_id). Real uploads routinely use
# DIFFERENT names for the same entity key across sheets — item_code ↔ barcode,
# invoice_no_txn_no ↔ invoice_no, party_name ↔ customer_name. Mapping each
# known alias to a concept makes those pairs candidates too; the value-overlap
# gate still has the final say, so a wrong alias guess can't invent a
# relationship the data doesn't support.
# ---------------------------------------------------------------------------
_CONCEPT_ALIASES: dict[str, str] = {}


def _alias(concept: str, *names: str) -> None:
    for n in names:
        _CONCEPT_ALIASES[n] = concept


_alias("product",
       "sku_id", "sku", "sku_code", "sku_no", "item_code", "item_id",
       "product_code", "product_id", "product_key", "article_code",
       "article", "barcode", "ean", "upc", "style_code")
_alias("invoice",
       "invoice_no", "invoice_number", "invoice", "invoice_no_txn_no",
       "bill_no", "bill_number", "order_no", "order_id", "order_number",
       "receipt_no")
_alias("txn", "transaction_id", "txn_id", "txn_line_key")
_alias("customer",
       "customer_id", "customer_code", "cust_id", "customer_name",
       "party_name", "member_id", "loyalty_id")
_alias("store",
       "store_id", "store_code", "store_name", "location_id",
       "branch_id", "outlet_id")


# A shared-value column pair is only a JOIN KEY if it's selective enough to be
# one. Low-cardinality columns (brand, color, region, size…) overlap ~100% by
# name across sheets, but joining on them fans the result set out — they are
# dimension attributes, not keys. A pair is accepted when EITHER side is
# "key-like": a recognised key concept / id-suffixed name (low cardinality OK,
# e.g. store_code), OR plainly high-cardinality (an identifier by shape, e.g.
# final_product). Everything else is a dimension overlap and is dropped.
_MIN_KEY_DISTINCT = 25          # high-cardinality floor for un-named keys
_MIN_NAMED_KEY_DISTINCT = 4     # named keys may be low-cardinality, but not enums
_ID_SUFFIXES: frozenset[str] = frozenset({
    "id", "code", "key", "no", "num", "number", "sku", "barcode", "ean", "upc",
})


def _looks_like_key(norm_name: str, distinct: int) -> bool:
    """Is a column selective / identifier-like enough to be a real join key?"""
    base = norm_name.rsplit("_", 1)[-1]
    if base in _ID_SUFFIXES or norm_name in _CONCEPT_ALIASES:
        return distinct >= _MIN_NAMED_KEY_DISTINCT
    return distinct >= _MIN_KEY_DISTINCT


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
    """Find JOIN keys across ``u_*`` tables by value overlap (≥ 80 % of the
    smaller side's distinct values).

    Candidate column pairs come from TWO sources:
      1. the same normalised name in two tables (``sku_id`` == ``sku_id``), and
      2. different names that map to the same key concept (``item_code`` ↔
         ``barcode``) — see ``_CONCEPT_ALIASES``.
    A pair is kept only when the overlap clears the threshold AND at least one
    side is key-like (``_looks_like_key``) — that guard drops low-cardinality
    dimension columns (brand/color/region) that overlap by name but aren't keys.

    ``tables`` is the shape ``_list_pg_user_tables()`` produces:
    ``[{"table": str, "columns": [{"name": str, ...}, ...]}, ...]``.
    Returns ``Relationship`` records ordered by
    ``(from_table, from_column, to_table)`` so output is deterministic.
    """
    from app.db_engine import pg_connection

    out: list[Relationship] = []
    if len(tables) < 2:
        return out

    # Every non-system, non-blocklisted column as (table, original, norm_key).
    cols: list[tuple[str, str, str]] = []
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
            cols.append((tname, cname, k))

    # Build candidate pairs, canonicalised as (a < b by (table, col)) so each
    # unordered pair is probed once; the dict value keeps both norm keys so the
    # cardinality guard below can inspect either side.
    candidates: dict[tuple[str, str, str, str], tuple[str, str]] = {}

    def _add(x: tuple[str, str, str], y: tuple[str, str, str]) -> None:
        a, b = sorted((x, y), key=lambda z: (z[0], z[1]))
        if a[0] == b[0]:
            return  # same table — not a cross-table relationship
        candidates[(a[0], a[1], b[0], b[1])] = (a[2], b[2])

    # (1) same normalised name across ≥ 2 tables.
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for tc in cols:
        by_name.setdefault(tc[2], []).append(tc)
    for holders in by_name.values():
        uniq = sorted(set(holders))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                _add(uniq[i], uniq[j])

    # (2) different names sharing a key concept (cross-name keys). Pairs that
    #     are ALSO same-name simply collide on the same dict key (deduped).
    by_concept: dict[str, list[tuple[str, str, str]]] = {}
    for tc in cols:
        concept = _CONCEPT_ALIASES.get(tc[2])
        if concept:
            by_concept.setdefault(concept, []).append(tc)
    for holders in by_concept.values():
        uniq = sorted(set(holders))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                _add(uniq[i], uniq[j])

    if not candidates:
        return out

    # Probe each candidate. Distinct counts are cached per (table, column): a
    # column shows up in many pairs (sku_id links four tables), so caching
    # roughly halves the query count vs. recomputing it per pair.
    distinct_cache: dict[tuple[str, str], int] = {}

    async with pg_connection() as db:
        async def _distinct(table: str, col: str) -> int:
            ck = (table, col)
            if ck not in distinct_cache:
                q = f'"{col}"'
                cur = await db.execute(
                    f"SELECT COUNT(DISTINCT {q}) AS n "
                    f'FROM "{table}" WHERE {q} IS NOT NULL'
                )
                r = await cur.fetchone()
                distinct_cache[ck] = int(dict(r)["n"]) if r else 0
            return distinct_cache[ck]

        for (a_table, a_col, b_table, b_col), (a_key, b_key) in candidates.items():
            qa = f'"{a_col}"'
            qb = f'"{b_col}"'
            try:
                a_n = await _distinct(a_table, a_col)
                b_n = await _distinct(b_table, b_col)
                if a_n == 0 or b_n == 0:
                    continue
                # Cardinality guard BEFORE the (costlier) overlap query: a pair
                # only counts if at least one side is key-like — this is what
                # filters out shared dimension attributes.
                if not (_looks_like_key(a_key, a_n) or _looks_like_key(b_key, b_n)):
                    continue

                # Overlap = distinct values present in BOTH tables. The
                # subquery is cheap — Postgres hash-joins both DISTINCTs
                # cheaply for small u_* sheets. ::text makes the match
                # type-agnostic (bigint item_code ↔ text barcode, etc.).
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
    out.sort(key=lambda r: (r.from_table, r.from_column, r.to_table))
    return out


async def save_relationships_pg(rels: list[Relationship]) -> int:
    """Replace the current relationships set with ``rels``.

    Self-provisioning: ensures ``_relationships`` exists in the CURRENT schema
    first. ``_init_database_postgres()`` only creates it in ``public``, so a
    refresh running under a tenant's ``search_path`` would otherwise hit a
    missing table and (because the caller swallows errors) silently save
    nothing — which is exactly why tenant schemas had no detected
    relationships. The ``IF NOT EXISTS`` DDL is a no-op where the table already
    exists. Truncates then re-inserts so removed tables / relationships don't
    linger — the detector runs after every upload and its result is the
    authoritative set.

    Returns the number of relationships saved.
    """
    from app.db_engine import pg_connection
    from app.infrastructure import _RELATIONSHIPS_DDL_PG
    async with pg_connection() as db:
        async with db.transaction():
            await db.execute(_RELATIONSHIPS_DDL_PG)
            await db.execute("DELETE FROM _relationships")
            if not rels:
                return 0
            sql = (
                "INSERT INTO _relationships "
                "(from_table, from_column, to_table, to_column, "
                " overlap_pct, from_distinct, to_distinct, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            _now = datetime.now(timezone.utc).isoformat()
            payload = [
                (r.from_table, r.from_column, r.to_table, r.to_column,
                 r.overlap_pct, r.from_distinct, r.to_distinct, _now)
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
