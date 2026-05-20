"""Concept Resolver — maps canonical analytic concepts onto the actual
columns of the uploaded dynamic (``u_*``) tables.

Pure and deterministic: the same tables in always yield the same
``ResolvedSchema`` out. No DB or network access.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnRef:
    """A concrete ``(table, column)`` location in the uploaded data."""

    table: str
    column: str


@dataclass(frozen=True)
class ResolvedSchema:
    """Outcome of resolving canonical concepts against a dataset. Each
    resolved concept maps to its ``ColumnRef``; unresolved concepts are
    absent. Use ``ref()`` rather than touching the mapping directly."""

    _resolved: dict[str, ColumnRef]

    def ref(self, concept: str) -> ColumnRef | None:
        """The resolved column for ``concept``, or ``None`` if unresolved."""
        return self._resolved.get(concept)

    def is_resolved(self, concept: str) -> bool:
        """Whether ``concept`` resolved to a column in this dataset."""
        return concept in self._resolved

    def missing(self, *concepts: str) -> list[str]:
        """Which of the given ``concepts`` did NOT resolve, in argument order."""
        return [c for c in concepts if c not in self._resolved]

    @property
    def can_compute_margin(self) -> bool:
        """True when every concept the realized-margin formula needs has
        resolved."""
        return all(c in self._resolved for c in _MARGIN_CORE)


# The concepts the realized-margin / profit formulas are built from.
_MARGIN_CORE: tuple[str, ...] = (
    "revenue", "quantity", "sku_key", "unit_cost", "product_label",
)

# concept -> ordered candidate column names. The concept's own name comes
# first (the "canonical" column name); curated synonyms follow. Earlier
# candidate = preferred.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "revenue", "net_sales", "sales_value", "sales_amount",
        "total_sales", "net_amount", "sale_amount", "sales",
    ),
    "quantity": (
        "quantity", "qty", "units", "units_sold", "quantity_sold",
        "qty_sold", "num_units",
    ),
    "sku_key": (
        "sku_key", "sku_id", "sku", "item_code", "item_id",
        "product_code", "product_id", "sku_code",
    ),
    "unit_cost": (
        "unit_cost", "cost_price", "cost", "buy_price", "purchase_price",
        "cost_per_unit", "unit_buy_price",
    ),
    "product_label": (
        "product_label", "final_product", "product_name", "product",
        "item_name", "item", "description", "product_title",
    ),
}


def _resolve_concept(
    tables: list[dict], candidates: tuple[str, ...],
) -> ColumnRef | None:
    """Resolve one concept against the dataset.

    Gathers every distinct column name that matches a candidate, then:
      - the canonical name (``candidates[0]``) wins outright if present;
      - otherwise a single distinct match resolves;
      - two or more distinct synonym matches with no canonical name are
        ambiguous and left unresolved - the resolver never guesses.

    The same column name appearing in several tables counts once (a
    denormalised key like ``sku_id``), bound to its first occurrence.
    """
    canonical = candidates[0]
    allowed = set(candidates)
    matches: dict[str, ColumnRef] = {}   # lower-cased name -> first ref
    for t in tables:
        for col in t.get("columns", []):
            name = str(col.get("name", ""))
            low = name.lower()
            if low in allowed and low not in matches:
                matches[low] = ColumnRef(t["table"], name)
    if not matches:
        return None
    if canonical in matches:
        return matches[canonical]
    if len(matches) == 1:
        return next(iter(matches.values()))
    return None


def resolve_schema(tables: list[dict]) -> ResolvedSchema:
    """Resolve canonical concepts against the uploaded ``u_*`` tables.

    ``tables`` is the shape ``SchemaTool._list_user_tables()`` produces:
    ``[{"table": str, "columns": [{"name": str, "type": str}, ...]}, ...]``.
    """
    resolved = {
        concept: ref
        for concept, candidates in _SYNONYMS.items()
        if (ref := _resolve_concept(tables, candidates)) is not None
    }
    return ResolvedSchema(resolved)
