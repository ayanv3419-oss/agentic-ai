"""Concept Resolver — the semantic schema-mapping layer.

Maps a fixed catalog of canonical business concepts (revenue, quantity,
brand, transaction_date, ...) onto the actual columns of an uploaded
workbook, whatever that workbook named them. This is what lets the
analytics + KPI layers run on arbitrary user schemas without hard-coding
column names to one dataset.

Pure and deterministic: the same tables in always yield the same
``ResolvedSchema`` out — no DB or network access. Matching is by a
curated synonym catalog ONLY — never fuzzy / substring — so an
unrecognised or genuinely ambiguous column is reported as unresolved
rather than mis-mapped. A wrong mapping silently corrupts every
downstream number, so "unresolved + reported" always beats "guessed".
"""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Column reference
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnRef:
    """A concrete ``(table, column)`` location in the uploaded data."""

    table: str
    column: str


# ---------------------------------------------------------------------------
# Canonical business schema — the concept catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Concept:
    """One canonical analytic concept. ``synonyms[0]`` is the canonical
    column name; the rest are curated aliases. ``kind`` tells the
    analytics / KPI layer how the concept may be used:
        measure   — numeric, SUM/AVG-able
        key       — a join / identity key
        label     — a human-readable name to GROUP/label by
        dimension — a categorical attribute to GROUP/filter by
        date      — a date column, for time windows and trends
    """

    name: str
    kind: str
    synonyms: tuple[str, ...]


def _c(name: str, kind: str, *aliases: str) -> Concept:
    """Build a Concept; the canonical name is always the first candidate."""
    return Concept(name, kind, (name, *aliases))


# The catalog. Adding a concept here is the ONLY place a new business
# concept needs to be declared — everything downstream discovers it.
_CATALOG: dict[str, Concept] = {c.name: c for c in (
    # --- financial measures ---
    _c("revenue", "measure",
       "net_sales", "sales_value", "sales_amount", "total_sales",
       "total_amount", "net_amount", "sale_amount", "sales", "turnover",
       "amount"),
    _c("gross_sales", "measure",
       "gross_amount", "gross_revenue", "gross_value", "mrp_value"),
    _c("discount", "measure",
       "discount_amount", "discount_value", "discount_amt", "disc"),
    _c("unit_cost", "measure",
       "cost_price", "cost", "buy_price", "purchase_price",
       "cost_per_unit", "unit_buy_price", "expense"),
    _c("selling_price", "measure",
       "sell_price", "list_price", "mrp", "retail_price", "price"),
    _c("quantity", "measure",
       "qty", "units", "units_sold", "quantity_sold", "qty_sold",
       "num_units"),
    _c("stock_qty", "measure",
       "stock_quantity", "stock", "on_hand", "qty_on_hand",
       "inventory_qty", "available_qty"),
    # --- keys ---
    _c("sku_key", "key",
       "sku_id", "sku", "item_code", "item_id", "product_code",
       "product_id", "sku_code", "article"),
    _c("invoice_id", "key",
       "invoice_no", "invoice_number", "bill_no", "order_no", "order_id",
       "txn_id", "transaction_id"),
    # --- labels ---
    _c("product_label", "label",
       "final_product", "product_name", "product", "item_name", "item",
       "description", "product_title"),
    _c("customer", "label",
       "customer_name", "party_name", "buyer", "client", "customer_id"),
    # --- dimensions ---
    _c("brand", "dimension",
       "brand_name", "make", "manufacturer"),
    _c("category", "dimension",
       "product_category", "category_name", "cat", "segment"),
    _c("subcategory", "dimension",
       "sub_category", "sub_cat", "subcat"),
    _c("region", "dimension",
       "zone", "area", "territory"),
    _c("city", "dimension",
       "town", "city_name"),
    _c("store", "dimension",
       "store_name", "outlet", "branch", "shop", "store_id"),
    _c("payment_mode", "dimension",
       "payment_type", "payment_method", "pay_mode", "tender", "payment"),
    _c("user_group", "dimension",
       "customer_group", "age_group", "customer_segment", "segment",
       "buyer_type", "shopper_type"),
    _c("color", "dimension",
       "colour", "color_name", "shade"),
    _c("size", "dimension",
       "shoe_size", "clothing_size", "size_label", "size_code"),
    # --- time ---
    _c("transaction_date", "date",
       "date", "txn_date", "order_date", "sale_date", "invoice_date",
       "bill_date", "sales_date"),
)}

#: Every canonical concept name, declaration order.
CANONICAL_CONCEPTS: tuple[str, ...] = tuple(_CATALOG)

#: The concepts the realized-margin / profit formulas are built from.
_MARGIN_CORE: tuple[str, ...] = (
    "revenue", "quantity", "sku_key", "unit_cost", "product_label",
)

# Confidence assigned to each match kind.
_CONF_CANONICAL = 1.0   # column literally named like the concept
_CONF_SYNONYM = 0.75    # column matched a curated alias


def concepts_of_kind(kind: str) -> tuple[str, ...]:
    """The canonical concept names of a given kind (measure/key/label/
    dimension/date) — used by the KPI layer to pick group-bys / measures."""
    return tuple(c.name for c in _CATALOG.values() if c.kind == kind)


# ---------------------------------------------------------------------------
# Resolution result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """How one concept resolved against a dataset."""

    concept: str
    column: ColumnRef
    confidence: float       # _CONF_CANONICAL or _CONF_SYNONYM
    match: str              # "canonical" | "synonym"


@dataclass(frozen=True, eq=False)
class ResolvedSchema:
    """The schema-abstraction layer — a frozen, deterministic mapping of
    canonical concepts onto a dataset's real columns, plus the confidence
    of each mapping and a record of any concept that was too ambiguous to
    map. Downstream code reads concepts, never raw column names.

    ``eq=False`` keeps default identity equality/hash; a frozen dataclass
    with dict fields would otherwise generate a ``__hash__`` that raises."""

    _resolved: dict[str, Resolution]
    _ambiguous: dict[str, tuple[str, ...]]
    _can_compute_margin: bool

    # -- concept access ----------------------------------------------------

    def ref(self, concept: str) -> ColumnRef | None:
        """The resolved column for ``concept``, or ``None`` if unresolved."""
        r = self._resolved.get(concept)
        return r.column if r is not None else None

    def resolution(self, concept: str) -> Resolution | None:
        """The full Resolution (column + confidence + match kind), or None."""
        return self._resolved.get(concept)

    def is_resolved(self, concept: str) -> bool:
        """Whether ``concept`` resolved to a column in this dataset."""
        return concept in self._resolved

    def confidence(self, concept: str) -> float:
        """Confidence of ``concept``'s mapping; 0.0 when unresolved."""
        r = self._resolved.get(concept)
        return r.confidence if r is not None else 0.0

    def resolves_all(self, *concepts: str) -> bool:
        """True only if every named concept resolved — the precondition
        check for any metric/KPI that needs them all."""
        return all(c in self._resolved for c in concepts)

    def missing(self, *concepts: str) -> list[str]:
        """Which of the given ``concepts`` did NOT resolve, in order."""
        return [c for c in concepts if c not in self._resolved]

    def ambiguities(self) -> dict[str, tuple[str, ...]]:
        """Concepts that matched two or more columns with no clear winner,
        mapped to the competing column names — for an informative
        'could not tell which column is X' message instead of a guess."""
        return dict(self._ambiguous)

    @property
    def can_compute_margin(self) -> bool:
        """True only when the realized-margin SQL is actually buildable
        for this dataset: every margin concept resolved, the sales-side
        columns co-located in one table, and the SKU join key physically
        present in BOTH that table and the (distinct) cost table."""
        return self._can_compute_margin

    def summary(self) -> dict:
        """Plain-data report of how the upload was understood — for the
        /health endpoint, logs, or telling the user about their schema."""
        return {
            "resolved": {
                c: {
                    "table": r.column.table,
                    "column": r.column.column,
                    "confidence": r.confidence,
                    "match": r.match,
                }
                for c, r in self._resolved.items()
            },
            "unresolved": [c for c in CANONICAL_CONCEPTS
                           if c not in self._resolved],
            "ambiguous": {c: list(cols)
                          for c, cols in self._ambiguous.items()},
            "can_compute_margin": self._can_compute_margin,
        }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

import re as _re


def _col_key(name: str) -> str:
    """Normalise a column name for synonym matching.

    Replaces spaces, hyphens and other non-alphanumeric separators with
    underscores so that "Product Name", "product-name" and "product_name"
    all produce the key "product_name" and match the catalog entry.
    Lowercased for case-insensitive comparison.

    We keep the original ``name`` in the ColumnRef so downstream SQL
    always uses the column's real name (with spaces / capitalisation).
    """
    return _re.sub(r"[\s\-]+", "_", name.lower()).strip("_")


def _collect_matches(concept: Concept, tables: list[dict]) -> dict[str, ColumnRef]:
    """Collect synonym matches for a concept — per-key first-occurrence map.

    Returns ``{normalised_key: ColumnRef}`` for Phase 1 (same deduplication
    used by ``_resolve_one``).  Each normalised key maps to the first table
    that provides a column with that name.
    """
    allowed = set(concept.synonyms)
    matches: dict[str, ColumnRef] = {}
    for t in tables:
        for col in t.get("columns", []):
            name = str(col.get("name", ""))
            key = _col_key(name)
            if key in allowed and key not in matches:
                matches[key] = ColumnRef(t["table"], name)
    return matches


def _collect_all_table_matches(concept: Concept, tables: list[dict]) -> list[ColumnRef]:
    """Collect EVERY synonym match across EVERY table (no deduplication).

    Used by the Phase 2 co-location pass which needs to find whether the
    concept appears in the primary sales table, even when an identically-
    named column in an earlier (alphabetically) table already won Phase 1.
    Returns one ColumnRef per (table, column) pair that matched.
    """
    allowed = set(concept.synonyms)
    result: list[ColumnRef] = []
    seen: set[tuple[str, str]] = set()
    for t in tables:
        for col in t.get("columns", []):
            name = str(col.get("name", ""))
            key = _col_key(name)
            if key in allowed:
                pair = (t["table"], name)
                if pair not in seen:
                    seen.add(pair)
                    result.append(ColumnRef(t["table"], name))
    return result


def _resolve_one(
    concept: Concept, tables: list[dict],
) -> tuple[Resolution | None, tuple[str, ...]]:
    """Resolve one concept against the dataset.

    Returns ``(Resolution | None, ambiguous_columns)``:
      - the canonical name wins outright -> confidence 1.0;
      - a single distinct synonym match -> confidence 0.75;
      - 2+ distinct synonym matches with no canonical winner -> unresolved,
        and the competing column names are returned for an ambiguity report.

    The same column name appearing in several tables counts once (a
    denormalised key like ``sku_id``), bound to its first occurrence.

    Column names are normalised before matching (spaces/hyphens → underscores,
    lowercased) so "Product Name", "product-name", and "product_name" all hit
    the same catalog synonym.
    """
    canonical = concept.synonyms[0]
    matches = _collect_matches(concept, tables)
    if not matches:
        return None, ()
    if canonical in matches:
        return (
            Resolution(concept.name, matches[canonical],
                       _CONF_CANONICAL, "canonical"),
            (),
        )
    if len(matches) == 1:
        ref = next(iter(matches.values()))
        return (
            Resolution(concept.name, ref, _CONF_SYNONYM, "synonym"),
            (),
        )
    # 2+ synonym matches, no canonical winner -> ambiguous; never guess.
    return None, tuple(sorted(matches))


def _columns_by_table(tables: list[dict]) -> dict[str, set[str]]:
    """Map each table name to its set of lower-cased column names."""
    return {
        t["table"]: {
            str(c.get("name", "")).lower() for c in t.get("columns", [])
        }
        for t in tables
    }


def _margin_computable(
    resolved: dict[str, Resolution], tables: list[dict],
) -> bool:
    """Whether the realized-margin SQL can actually be built AND run.

    Not merely "all margin concepts resolved": the builder emits a fixed
    two-table join, so this also requires the sales-side concepts to
    share one table, a distinct cost table, and the SKU join key to
    physically exist in BOTH. Without this check the builder could emit
    SQL that fails at runtime with 'no such column'.
    """
    if not all(c in resolved for c in _MARGIN_CORE):
        return False
    sales_t = resolved["revenue"].column.table
    if (resolved["quantity"].column.table != sales_t
            or resolved["product_label"].column.table != sales_t):
        return False
    cost_t = resolved["unit_cost"].column.table
    if cost_t == sales_t:
        # The builder's two-table join would self-join; the no-join
        # single-table margin shape is out of scope for this slice.
        return False
    sku_col = resolved["sku_key"].column.column.lower()
    cols = _columns_by_table(tables)
    return (sku_col in cols.get(sales_t, set())
            and sku_col in cols.get(cost_t, set()))


_CONF_COLOCATION = 0.85   # higher than synonym, lower than canonical


def resolve_schema(tables: list[dict]) -> ResolvedSchema:
    """Resolve every canonical concept against the uploaded ``u_*`` tables.

    ``tables`` is the shape ``SchemaTool._list_user_tables()`` produces:
    ``[{"table": str, "columns": [{"name": str, "type": str}, ...]}, ...]``.

    Two-phase resolution
    --------------------
    Phase 1 (standard): first canonical match wins; single synonym wins;
    multiple synonyms with no canonical → ambiguous.

    Phase 2 (co-location): uploaded data often comes from a single Excel
    workbook split into many sheets, so the same business column (e.g.
    ``category``) appears in multiple tables with different quality.  The
    alphabetically-first canonical match is not always correct — e.g.
    ``u_item_details.category`` might be all-NULL while the real column is
    ``u_sales_transactions.category``.

    To fix this, Phase 2 identifies the **primary sales table** — the table
    that holds ``transaction_date`` (the unambiguous anchor).  For every
    concept that is either:
      (a) ambiguous (no winner in Phase 1), OR
      (b) currently resolved to a table OTHER than the primary sales table
    Phase 2 checks whether ANY synonym match lands in the primary table.
    If one does, we use that match instead and mark it with confidence
    _CONF_COLOCATION (0.85 — above synonym, below canonical).  This keeps
    all the analytics measures, dimensions, and keys on the same table so
    SQL never needs a cross-sheet join to answer a basic question.

    Concepts whose nature is not sales-transactional (unit_cost, stock_qty,
    selling_price …) live only in the inventory table and are NOT dragged to
    the sales table because they simply have no column there.

    Graceful by construction: an unknown or ambiguous workbook simply
    yields a partially-resolved schema — concepts that map are usable,
    the rest are reported via ``missing()`` / ``ambiguities()``.
    """
    # ---- Phase 1: standard resolution ------------------------------------
    resolved: dict[str, Resolution] = {}
    ambiguous: dict[str, tuple[str, ...]] = {}
    # Also keep per-concept full match lists for Phase 2 (no deduplication,
    # so we can find the primary-table match even when another table's column
    # of the same name already won Phase 1).
    all_table_matches: dict[str, list[ColumnRef]] = {}

    for concept in _CATALOG.values():
        all_table_matches[concept.name] = _collect_all_table_matches(concept, tables)
        res, amb = _resolve_one(concept, tables)
        if res is not None:
            resolved[concept.name] = res
        elif amb:
            ambiguous[concept.name] = amb

    # ---- Phase 2: co-location preference ---------------------------------
    # Identify the primary sales table via the transaction_date concept —
    # it is canonical and unambiguous in well-formed workbooks.
    primary_table: str | None = None
    if "transaction_date" in resolved:
        primary_table = resolved["transaction_date"].column.table

    if primary_table:
        for concept in _CATALOG.values():
            cname = concept.name
            # Skip if already correctly resolved to the primary table.
            if (cname in resolved
                    and resolved[cname].column.table == primary_table):
                continue

            # Search the FULL (un-deduplicated) match list for any hit in
            # the primary table.  This finds u_sales_transactions.category
            # even when u_item_details.category already won Phase 1.
            primary_ref: ColumnRef | None = next(
                (ref for ref in all_table_matches.get(cname, [])
                 if ref.table == primary_table),
                None,
            )
            if primary_ref is None:
                continue  # concept has no column in the primary table → leave as-is

            # Override: use the primary-table match.
            resolved[cname] = Resolution(
                cname, primary_ref, _CONF_COLOCATION, "co-location",
            )
            ambiguous.pop(cname, None)

    return ResolvedSchema(
        resolved, ambiguous, _margin_computable(resolved, tables),
    )
