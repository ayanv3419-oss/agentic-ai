"""
Capability: resolve_entities
============================

Canonicalise free-text product / customer / vendor / branch names into
the values stored in the database. Uses the existing fuzzy synonym dict
in ``app.infrastructure.resolve_entities`` plus, post-P5, the vector
store for semantic disambiguation.

Status
------
P1 — typed contract + stub. Body lands in P2 (delegates to
``EntityResolverTool`` primitive).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


EntityKind = Literal["product", "customer", "vendor", "branch", "unknown"]


class ResolveEntitiesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: tuple[str, ...] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Raw entity mentions extracted from the user's question.",
    )


class ResolvedEntity(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: str
    canonical: str | None = None
    kind: EntityKind = "unknown"
    confidence: float = 0.0


class ResolveEntitiesOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    entities: tuple[ResolvedEntity, ...] = ()
    placeholder: bool = True
    note: str = "stub — body implementation lands in P2"


@register_capability
class ResolveEntities(Capability[ResolveEntitiesArgs, ResolveEntitiesOutput]):
    name = "resolve_entities"
    description = (
        "Canonicalise mentioned product / customer / vendor / branch names "
        "against the database's known values via fuzzy + (later) semantic match."
    )
    args_model = ResolveEntitiesArgs
    output_model = ResolveEntitiesOutput
    requires: tuple[str, ...] = ()
    pure = False  # reads the synonym dict + DB

    async def run(
        self,
        state: ExecutionState,
        args: ResolveEntitiesArgs,
    ) -> ResolveEntitiesOutput:
        # Real body: fuzzy match each raw name against the synonyms dict
        # in app.infrastructure. Confidence is 1.0 on exact (case-insensitive)
        # alias hit, ratio-based otherwise via difflib.
        from difflib import SequenceMatcher

        from app.infrastructure import load_synonyms

        syns = load_synonyms() or {}
        # Build a flat "phrase -> canonical" index for quick lookup.
        canonical_by_phrase: dict[str, str] = {}
        for canonical, aliases in syns.items():
            canonical_by_phrase[canonical.lower()] = canonical
            for a in aliases:
                canonical_by_phrase[str(a).lower()] = canonical

        results: list[ResolvedEntity] = []
        for raw in args.names:
            raw_norm = raw.strip().lower()
            if not raw_norm:
                results.append(ResolvedEntity(raw=raw, kind="unknown"))
                continue

            # Exact match wins.
            if raw_norm in canonical_by_phrase:
                results.append(ResolvedEntity(
                    raw=raw,
                    canonical=canonical_by_phrase[raw_norm],
                    kind="product",  # synonyms dict is product-centric today
                    confidence=1.0,
                ))
                continue

            # Fuzzy fallback. Score every known phrase, take the best.
            best_phrase: str | None = None
            best_ratio = 0.0
            for phrase in canonical_by_phrase:
                ratio = SequenceMatcher(None, raw_norm, phrase).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_phrase = phrase

            if best_phrase is not None and best_ratio >= 0.7:
                results.append(ResolvedEntity(
                    raw=raw,
                    canonical=canonical_by_phrase[best_phrase],
                    kind="product",
                    confidence=round(best_ratio, 3),
                ))
            else:
                results.append(ResolvedEntity(
                    raw=raw,
                    canonical=None,
                    kind="unknown",
                    confidence=round(best_ratio, 3),
                ))

        return ResolveEntitiesOutput(
            entities=tuple(results),
            placeholder=False,
            note="",
        )
