"""
EntityLoc - resolve named entities in the question (parties/customers,
products, brands, categories) using the schema-resolved column names
from u_* tables, plus a SELECT DISTINCT scan over the actual data.

Key fixes vs the original:
  - Scans u_* (uploaded) tables, not the legacy "sales"/"purchase" tables
    that are usually empty or hold stale test data.
  - Discovers entity columns (product_label, customer, brand, category,
    subcategory) from the state's resolved_schema, so it works regardless
    of what the workbook named its columns.
  - Candidate-term extraction is now case-insensitive: lowercased words
    that aren't stopwords are also tested, so "adidas" (lowercase) is
    found even though it doesn't start with a capital letter.
"""
from __future__ import annotations

import re
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.infrastructure import (
    get_connection,
    quoted,
    resolve_entities as resolve_synonyms,
)
from app.schema_mapping.resolver import ResolvedSchema


_DEFAULT_MAX_PER_KIND = 20

# Stop-words to exclude from the candidate set so we don't LIKE-scan
# "what", "last", "month", etc. against the product columns.
_STOP_WORDS: frozenset[str] = frozenset({
    # question words
    "what", "when", "why", "how", "show", "give", "list",
    "which", "where", "who", "does", "did", "has", "have",
    "had", "its", "can", "may", "all", "any", "get", "got",
    # determiners / prepositions / conjunctions
    "top", "bottom", "last", "this", "that", "the", "a", "an",
    "rca", "kpi", "api", "for", "in", "at", "on", "of", "by",
    "with", "from", "and", "or", "not", "are", "is", "was",
    "my", "our", "their", "per", "than", "too", "very", "but",
    # superlatives / adjectives that aren't entity names
    "best", "worst", "most", "least", "fast", "slow",
    "high", "low", "hot", "popular", "fastest", "slowest",
    "highest", "lowest", "biggest", "smallest", "largest",
    # time words
    "month", "week", "year", "day", "quarter", "period",
    "months", "weeks", "years", "days", "today", "now",
    # business terms that are query concepts, not entity names
    "selling", "sold", "sales", "revenue", "profit", "margin",
    "average", "total", "count", "sum", "growth", "trend",
    "change", "drop", "rise", "increase", "decrease", "compare",
    # generic product / business words (query dimensions, not entity names)
    "products", "items", "product", "item", "category", "categories",
    "subcategory", "subcategories", "brand", "brands", "region", "regions",
    "store", "stores", "channel", "channels", "customer", "customers",
    "performs", "performing", "performance", "trends", "trending",
    "compare", "comparison", "between", "versus", "over", "time",
})

# Tokenizer: words and hyphenated compounds.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9'\-]*[a-zA-Z0-9]|[a-zA-Z0-9]")


async def _list_user_tables() -> list[str]:
    """Return names of all u_* tables currently in the DB."""
    async with get_connection() as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'u\\_%' ESCAPE '\\' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
        await cur.close()
    return [dict(r)["name"] for r in rows if dict(r).get("name")]


async def _scan_distinct(
    table: str, column: str, like: str, limit: int,
) -> list[str]:
    """LIKE scan for ``like`` in ``column`` of ``table``. Returns distinct,
    non-empty string values, case-insensitive, up to ``limit``."""
    out: set[str] = set()
    try:
        async with get_connection() as db:
            cur = await db.execute(
                f"SELECT DISTINCT {quoted(column)} "
                f"FROM {quoted(table)} "
                f"WHERE {quoted(column)} IS NOT NULL "
                f"AND lower({quoted(column)}) LIKE ? "
                f"LIMIT ?",
                (f"%{like.lower()}%", limit),
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                val = list(dict(r).values())[0]
                if isinstance(val, str) and val.strip():
                    out.add(val.strip())
    except Exception:
        pass
    return sorted(out)[:limit]


def _candidate_terms(question: str) -> list[str]:
    """Extract potential entity terms from the question.

    Returns both:
    - multi-word capitalized noun phrases (e.g. "Adidas Shoes")
    - lowercased non-stopword tokens (e.g. "adidas") so brand names
      that happen to be lowercase are still found.

    All terms are de-duped and sorted by length (longer first, so more
    specific terms take priority).
    """
    if not question:
        return []

    seen: set[str] = set()
    out: list[str] = []

    # 1. Capitalized runs (original behaviour, catches "Adidas Shoes")
    parts = question.replace("?", " ").replace(",", " ").split()
    buf: list[str] = []
    drop_caps = {
        "What", "When", "Why", "How", "Show", "Give", "List", "Top",
        "Bottom", "Last", "This", "That", "The", "A", "An",
        "RCA", "KPI", "API", "Which", "Where", "Who",
    }
    for w in parts:
        if w and (w[0].isupper() or any(ch.isdigit() for ch in w)):
            buf.append(w)
        else:
            if buf:
                phrase = " ".join(buf)
                if phrase not in drop_caps and len(phrase) > 2:
                    if phrase.lower() not in seen:
                        seen.add(phrase.lower())
                        out.append(phrase)
            buf = []
    if buf:
        phrase = " ".join(buf)
        if phrase not in drop_caps and len(phrase) > 2:
            if phrase.lower() not in seen:
                seen.add(phrase.lower())
                out.append(phrase)

    # 2. Individual lowercased tokens that aren't stop-words.
    # Minimum length 4: short tokens like "has", "the", "got" are almost
    # never entity names and their 3-char substrings cause false LIKE hits
    # against party/product columns (e.g. "has" matches "Raghavendra Sharma").
    for tok in _TOKEN_RE.findall(question):
        t = tok.lower()
        if t in _STOP_WORDS or len(t) < 4:
            continue
        if t not in seen:
            seen.add(t)
            out.append(tok)

    # Sort: longer first (more specific), then alpha.
    out.sort(key=lambda s: (-len(s), s.lower()))
    return out


def _entity_columns(resolved: ResolvedSchema | None) -> list[tuple[str, str, str]]:
    """Return (table, column, kind) triples for entity-holding concepts.

    Checks: product_label, customer, brand, category, subcategory — the
    columns a user is most likely to filter by when asking about specific
    entities. Falls back to an empty list when no resolved schema exists.
    """
    if resolved is None:
        return []
    result: list[tuple[str, str, str]] = []
    for concept, kind in (
        ("product_label", "product"),
        ("customer",      "party"),
        ("brand",         "brand"),
        ("category",      "category"),
        ("subcategory",   "subcategory"),
    ):
        ref = resolved.ref(concept)
        if ref is not None:
            result.append((ref.table, ref.column, kind))
    return result


class EntityLocTool(Tool):
    name = "EntityLoc"
    description = (
        "Resolve named entities mentioned in the question (parties / "
        "customers, products, brands, categories) into canonical values "
        "present in the uploaded data. Scans u_* tables using the actual "
        "column names from the resolved schema. Returns synonym matches "
        "AND data-row matches so the downstream SQL writer can use the "
        "exact strings stored in the DB."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Defaults to the current turn's question.",
            },
            "max_per_kind": {
                "type": "integer",
                "default": _DEFAULT_MAX_PER_KIND,
                "minimum": 1,
                "maximum": 100,
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        question = args.get("question") or ctx.state.question or ""
        limit = int(args.get("max_per_kind") or _DEFAULT_MAX_PER_KIND)
        resolved: ResolvedSchema | None = ctx.state.resolved_schema

        # Synonym index (lightweight, always runs).
        synonyms = resolve_synonyms(question)

        # Extract candidate search terms from the question.
        candidates = _candidate_terms(question)

        # Determine which (table, column, kind) pairs to search.
        entity_cols = _entity_columns(resolved)

        # Fallback: if schema tool hasn't run yet, scan all u_* tables
        # for any column named like a product or party identifier.
        if not entity_cols:
            user_tables = await _list_user_tables()
            # No resolved schema — do a best-effort scan of common column
            # name patterns: any column whose lowercased name contains
            # "product", "item", "name", "party", "customer", "brand".
            _entity_col_hints = re.compile(
                r"(product|item|name|party|customer|brand|category)", re.I
            )
            async with get_connection() as db:
                for tbl in user_tables:
                    try:
                        cur = await db.execute(
                            f"PRAGMA table_info({quoted(tbl)})"
                        )
                        cols = await cur.fetchall()
                        await cur.close()
                        for c in cols:
                            col_name = dict(c).get("name", "")
                            if _entity_col_hints.search(col_name):
                                kind = (
                                    "party" if re.search(r"party|customer", col_name, re.I)
                                    else "brand" if re.search(r"brand", col_name, re.I)
                                    else "category" if re.search(r"category", col_name, re.I)
                                    else "product"
                                )
                                entity_cols.append((tbl, col_name, kind))
                    except Exception:
                        continue

        # Scan the resolved columns for each candidate term.
        matches_by_kind: dict[str, list[str]] = {}
        for term in candidates[:6]:  # cap at 6 terms to avoid DB hammering
            for tbl, col, kind in entity_cols:
                hits = await _scan_distinct(tbl, col, term, limit)
                if hits:
                    bucket = matches_by_kind.setdefault(kind, [])
                    bucket.extend(hits)

        # De-duplicate within each kind.
        # resolve_synonyms returns list[dict] with "canonical" and
        # "matched_aliases" keys — extract the canonical string as the value.
        entities: list[dict[str, Any]] = []
        for s in synonyms:
            canonical = s.get("canonical") if isinstance(s, dict) else str(s)
            if canonical:
                entities.append({"kind": "synonym", "value": canonical})
        for kind, vals in matches_by_kind.items():
            deduped = list(dict.fromkeys(vals))[:limit]
            matches_by_kind[kind] = deduped
            for v in deduped:
                entities.append({"kind": kind, "value": v})

        return ToolOutcome(
            ok=True,
            output={
                "entities": entities,
                "synonyms": synonyms,
                "matches": matches_by_kind,
                "candidates": candidates[:10],
                "columns_searched": [
                    {"table": t, "column": c, "kind": k}
                    for t, c, k in entity_cols
                ],
            },
            state_updates={"entities": entities},
        )


__all__ = ["EntityLocTool"]
