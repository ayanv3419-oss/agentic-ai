"""
EntityLoc - resolve named entities in the question (parties/customers,
products, branches) using the synonym index in infrastructure.py plus
a SELECT DISTINCT scan over the actual data so we catch entities the
user typed verbatim.
"""
from __future__ import annotations

from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.infrastructure import (
    ALLOWED_TABLES,
    get_connection,
    quoted,
    resolve_entities as resolve_synonyms,
)


_DEFAULT_MAX_PER_KIND = 20


async def _scan_distinct(column: str, like: str, limit: int) -> list[str]:
    """Cheap LIKE scan across the live system tables for distinct values."""
    out: set[str] = set()
    async with get_connection() as db:
        for tbl in ALLOWED_TABLES:
            try:
                cur = await db.execute(
                    f'SELECT DISTINCT {quoted(column)} '
                    f'FROM {quoted(tbl)} '
                    f'WHERE {quoted(column)} IS NOT NULL '
                    f'AND lower({quoted(column)}) LIKE ? '
                    f'LIMIT ?',
                    (f"%{like.lower()}%", limit),
                )
                rows = await cur.fetchall()
                await cur.close()
                for r in rows:
                    val = list(dict(r).values())[0]
                    if isinstance(val, str) and val.strip():
                        out.add(val.strip())
            except Exception:
                continue
    return sorted(out)[:limit]


def _candidate_terms(question: str) -> list[str]:
    """Pull capitalized words / multi-word noun phrases out as candidates."""
    if not question:
        return []
    parts = question.replace("?", " ").replace(",", " ").split()
    out: list[str] = []
    buf: list[str] = []
    for w in parts:
        if w and (w[0].isupper() or any(ch.isdigit() for ch in w)):
            buf.append(w)
        else:
            if buf:
                out.append(" ".join(buf))
                buf = []
    if buf:
        out.append(" ".join(buf))
    # Filter out common stopwords / route keywords.
    drop = {"What", "When", "Why", "How", "Show", "Give", "List", "Top",
            "Bottom", "Last", "This", "RCA", "KPI", "API"}
    return [t for t in out if t not in drop and len(t) > 2]


class EntityLocTool(Tool):
    name = "EntityLoc"
    description = (
        "Resolve named entities mentioned in the question (parties / "
        "customers, products, branches) into canonical values present in "
        "the data. Returns synonym matches AND data-row matches so the "
        "downstream SQL writer can use the exact strings stored in DB."
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
        synonyms = resolve_synonyms(question)
        candidates = _candidate_terms(question)
        product_matches: list[str] = []
        party_matches: list[str] = []
        for term in candidates[:5]:
            try:
                product_matches.extend(
                    await _scan_distinct("Product Name", term, limit)
                )
                party_matches.extend(
                    await _scan_distinct("Party Name", term, limit)
                )
            except Exception:
                continue
        # Deduplicate while preserving order.
        product_matches = list(dict.fromkeys(product_matches))[:limit]
        party_matches = list(dict.fromkeys(party_matches))[:limit]

        entities = []
        for s in synonyms:
            entities.append({"kind": "synonym", "value": s})
        for p in product_matches:
            entities.append({"kind": "product", "value": p})
        for p in party_matches:
            entities.append({"kind": "party", "value": p})

        return ToolOutcome(
            ok=True,
            output={
                "entities": entities,
                "synonyms": synonyms,
                "products": product_matches,
                "parties": party_matches,
                "candidates": candidates,
            },
            state_updates={"entities": entities},
        )


__all__ = ["EntityLocTool"]
