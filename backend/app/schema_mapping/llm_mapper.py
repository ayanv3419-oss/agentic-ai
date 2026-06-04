"""LLM-driven schema mapping — propose + validate a concept→column map.

The deterministic :mod:`app.schema_mapping.resolver` refuses to guess when a
multi-sheet workbook has several sales-looking sheets, so the dashboard shows
nothing. This module is the fallback: it shows the LLM the tenant's REAL tables,
their columns (name + type), and a few sample rows, and asks it to PROPOSE which
``{table, column}`` is each business concept (transaction_date, revenue, …).
The proposal is then validated against the actual introspected schema so a
hallucinated table/column can never reach the dashboard.

Everything is tool/data-driven — nothing is hardcoded to a specific dataset:
the tables, columns, and samples all come from the tenant's own data via
:func:`app.dynamic_ingest.list_dynamic_tables_for_tenant`.

Shared mapping contract (one active mapping per tenant)::

    {
      "concepts": {
        "transaction_date": {"table": str, "column": str},
        "revenue":          {"table": str, "column": str},
        "customer":         {"table": str, "column": str},   # optional
        "invoice_id":       {"table": str, "column": str},   # optional
        "quantity":         {"table": str, "column": str},   # optional
      },
      "source": "llm" | "user",
      "updated_at": "<iso8601>",
    }

Only ``transaction_date`` + ``revenue`` are REQUIRED; the rest are omitted when
not found.

The LLM client reused here is exactly the one the coordinator uses:
``app.coordinator.llm.LLMClient`` + ``await client.complete(...)`` with
``temperature=0`` and ``force_json=True``, parsed by
``app.coordinator.llm.parse_strict_json`` (strips code fences, scans for the
first balanced ``{...}``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.coordinator.llm import LLMClient, parse_strict_json
from app.infrastructure import get_connection, quoted

_log = logging.getLogger("agentic_ai.schema_mapping.llm_mapper")

# The concepts the LLM is asked to locate. transaction_date + revenue are
# REQUIRED for a usable mapping; the rest are optional and dropped if absent.
_REQUIRED_CONCEPTS: tuple[str, ...] = ("transaction_date", "revenue")
_OPTIONAL_CONCEPTS: tuple[str, ...] = ("customer", "invoice_id", "quantity")
_ALL_CONCEPTS: tuple[str, ...] = _REQUIRED_CONCEPTS + _OPTIONAL_CONCEPTS

# How many sample rows to show the LLM per table. Enough to reveal value shape
# (dates, currency amounts, ids) without blowing up the prompt.
_SAMPLE_ROWS = 3

# Substrings that mark a column TYPE as numeric (case-insensitive). Used by
# validate_mapping for the revenue/quantity numeric check.
_NUMERIC_TYPE_HINTS: tuple[str, ...] = (
    "int", "real", "numeric", "double", "float", "decimal", "money",
)

# Substrings that mark a column TYPE as date/timestamp (case-insensitive).
_DATE_TYPE_HINTS: tuple[str, ...] = ("date", "time", "timestamp")

# Substrings that, in a column NAME, suggest it holds a date even when the
# stored type is TEXT (very common for ingested workbooks).
_DATE_NAME_HINTS: tuple[str, ...] = (
    "date", "day", "month", "year", "dt", "time", "period",
)

_SYSTEM_PROMPT = (
    "You are a data-schema mapping assistant for a retail analytics dashboard. "
    "You are given one or more database tables — each with its columns (name "
    "and type) and a few sample rows. Identify which single {table, column} "
    "holds each of these business concepts:\n"
    "  - transaction_date: the date a sale/transaction occurred\n"
    "  - revenue: the NET sales amount of a transaction line (money), not a "
    "unit price, cost, tax, or discount\n"
    "  - customer: the customer / party / buyer name or id\n"
    "  - invoice_id: the invoice / bill / order number identifying a sale\n"
    "  - quantity: the number of units sold\n"
    "Use ONLY the tables and columns shown. Pick the column whose NAME and "
    "SAMPLE VALUES best fit each concept. If a concept is not present, omit it "
    "entirely — never invent a table or column, and never guess. "
    "transaction_date and revenue are the most important; find them if at all "
    "possible.\n"
    "Respond with STRICT JSON ONLY, no prose, in exactly this shape:\n"
    '{"concepts": {"transaction_date": {"table": "...", "column": "..."}, '
    '"revenue": {"table": "...", "column": "..."}, '
    '"customer": {"table": "...", "column": "..."}, '
    '"invoice_id": {"table": "...", "column": "..."}, '
    '"quantity": {"table": "...", "column": "..."}}}\n'
    "Omit any concept you cannot confidently locate."
)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, computed per-call (never at import time)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_mapping() -> dict:
    """The defensive 'nothing usable' result — caller handles an empty map."""
    return {"concepts": {}, "source": "llm", "updated_at": _now_iso()}


async def _fetch_sample_rows(db: Any, table: str) -> list[dict[str, Any]]:
    """Up to ``_SAMPLE_ROWS`` rows from ``table`` as plain dicts.

    Defensive: any failure (missing table, driver quirk) yields ``[]`` so one
    bad table never aborts the whole proposal.
    """
    try:
        cur = await db.execute(
            f"SELECT * FROM {quoted(table)} LIMIT {int(_SAMPLE_ROWS)}"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]
    except Exception:
        _log.warning("sample-row fetch failed for table=%r", table, exc_info=True)
        return []


def _coerce_cell(value: Any) -> Any:
    """Make a sample cell JSON/prompt-friendly and bounded in length."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _build_user_prompt(tables: list[dict], samples: dict[str, list[dict]]) -> str:
    """Compact textual description of every table: columns (name:type) and
    its sample rows. Data-driven — purely whatever the tenant uploaded."""
    blocks: list[str] = []
    for t in tables:
        name = str(t.get("table", ""))
        cols = t.get("columns", []) or []
        col_lines = [
            f"  - {c.get('name')} ({c.get('type') or 'TEXT'})" for c in cols
        ]
        sample_rows = samples.get(name, [])
        sample_lines: list[str] = []
        for i, row in enumerate(sample_rows, 1):
            cells = {k: _coerce_cell(v) for k, v in row.items()}
            sample_lines.append(f"    row{i}: {cells}")
        block = (
            f"TABLE {name}\n"
            f"COLUMNS:\n" + ("\n".join(col_lines) if col_lines else "  (none)")
            + "\nSAMPLE ROWS:\n"
            + ("\n".join(sample_lines) if sample_lines else "    (no rows)")
        )
        blocks.append(block)
    return (
        "Here are the tables. Map the business concepts to {table, column}.\n\n"
        + "\n\n".join(blocks)
    )


def _index_columns(tables: list[dict]) -> dict[str, dict[str, str]]:
    """``{table: {column_name: type}}`` for existence + type checks.

    Column names are kept verbatim (their real casing) AND the lookup is done
    case-insensitively by callers, so a mapping referring to a real column with
    different casing still validates.
    """
    out: dict[str, dict[str, str]] = {}
    for t in tables:
        tname = str(t.get("table", ""))
        cols: dict[str, str] = {}
        for c in t.get("columns", []) or []:
            cname = c.get("name")
            if cname is None:
                continue
            cols[str(cname)] = str(c.get("type") or "")
        out[tname] = cols
    return out


def _lookup_column_type(
    index: dict[str, dict[str, str]], table: str, column: str
) -> str | None:
    """Return the declared type of ``table.column`` if it exists, else None.

    Existence is matched case-insensitively on BOTH table and column so a
    valid mapping is not rejected over capitalisation differences.
    """
    if table in index:
        cols = index[table]
    else:
        cols = next(
            (c for tn, c in index.items() if tn.lower() == str(table).lower()),
            None,
        )
        if cols is None:
            return None
    if column in cols:
        return cols[column]
    for cname, ctype in cols.items():
        if cname.lower() == str(column).lower():
            return ctype
    return None


def _type_is_numeric(col_type: str | None) -> bool | None:
    """True/False if the type clearly is/ isn't numeric; None if undeterminable
    (e.g. an empty/'TEXT' type, where the real values may still be numbers)."""
    if not col_type:
        return None
    low = col_type.lower()
    if any(h in low for h in _NUMERIC_TYPE_HINTS):
        return True
    # A bare TEXT/VARCHAR type is genuinely ambiguous for ingested workbooks
    # (numbers are routinely stored as text), so don't hard-fail — report None.
    if "char" in low or low.strip() in ("text", "str", "string", ""):
        return None
    return False


def _looks_like_date(col_type: str | None, column: str) -> bool:
    """Whether a column plausibly holds a date — by TYPE or, failing that, by
    NAME (ingested date columns are very often stored as TEXT)."""
    low_type = (col_type or "").lower()
    if any(h in low_type for h in _DATE_TYPE_HINTS):
        return True
    low_name = str(column).lower()
    return any(h in low_name for h in _DATE_NAME_HINTS)


def _clean_proposed_concepts(
    raw_concepts: Any, index: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    """Keep only well-formed concept entries whose {table, column} actually
    exists in the introspected schema. Unknown concept keys, malformed
    entries, and references to non-existent tables/columns are dropped."""
    cleaned: dict[str, dict[str, str]] = {}
    if not isinstance(raw_concepts, dict):
        return cleaned
    for concept in _ALL_CONCEPTS:
        entry = raw_concepts.get(concept)
        if not isinstance(entry, dict):
            continue
        table = entry.get("table")
        column = entry.get("column")
        if not isinstance(table, str) or not isinstance(column, str):
            continue
        if not table or not column:
            continue
        if _lookup_column_type(index, table, column) is None:
            _log.info(
                "dropping proposed %s=%s.%s — not in introspected schema",
                concept, table, column,
            )
            continue
        cleaned[concept] = {"table": table, "column": column}
    return cleaned


async def propose_mapping(tenant_id: str) -> dict:
    """Ask the LLM to propose a concept→column mapping for ``tenant_id``.

    Binds the query tenant, introspects the tenant's ``u_*`` tables + columns,
    fetches a few sample rows per table, prompts the configured LLM for a
    STRICT-JSON proposal, then drops any concept referring to a table/column
    that does not actually exist. Returns a mapping dict in the shared contract
    (``source="llm"``).

    Defensive by construction: if there are no tables, the LLM call fails, or
    the output is unusable, an empty mapping (``{"concepts": {}, ...}``) is
    returned and the caller decides what to do.
    """
    from app.tenant_context import set_query_tenant

    set_query_tenant(tenant_id)

    # Introspect the tenant's own tables/columns (search_path now scoped).
    from app.dynamic_ingest import list_dynamic_tables_for_tenant

    try:
        tables = await list_dynamic_tables_for_tenant()
    except Exception:
        _log.exception("list_dynamic_tables_for_tenant failed for tenant=%r",
                        tenant_id)
        return _empty_mapping()

    if not tables:
        _log.info("propose_mapping: no dynamic tables for tenant=%r", tenant_id)
        return _empty_mapping()

    # Sample rows per table (each guarded so one bad table can't abort).
    samples: dict[str, list[dict[str, Any]]] = {}
    async with get_connection() as db:
        for t in tables:
            name = str(t.get("table", ""))
            if not name:
                continue
            samples[name] = await _fetch_sample_rows(db, name)

    user_prompt = _build_user_prompt(tables, samples)

    client = LLMClient()
    try:
        resp = await client.complete(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            force_json=True,
        )
    except Exception:
        _log.exception("LLM propose_mapping call raised for tenant=%r", tenant_id)
        return _empty_mapping()
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    if resp.error or not resp.content:
        _log.warning(
            "propose_mapping LLM call unusable for tenant=%r (error=%s)",
            tenant_id, resp.error,
        )
        return _empty_mapping()

    try:
        data = parse_strict_json(resp.content)
    except ValueError:
        _log.warning("propose_mapping: LLM returned non-JSON for tenant=%r",
                     tenant_id)
        return _empty_mapping()

    index = _index_columns(tables)
    cleaned = _clean_proposed_concepts(data.get("concepts"), index)

    return {
        "concepts": cleaned,
        "source": "llm",
        "updated_at": _now_iso(),
    }


def validate_mapping(mapping: dict, tables: list[dict]) -> dict:
    """Validate + clean a proposed mapping against the real introspected schema.

    Returns ``{"ok": bool, "issues": [str], "mapping": <cleaned dict>}``:

    * Every mapped ``{table, column}`` must exist in ``tables`` (case-
      insensitive); entries that don't are dropped and reported.
    * ``revenue`` (and ``quantity`` if present) should be numeric. A clearly
      non-numeric type is a hard issue; an undeterminable type (bare TEXT) is a
      soft warning, not a failure — ingested numbers are routinely TEXT.
    * ``transaction_date`` should look like a date by TYPE, or by column NAME.

    ``ok`` is True ONLY when BOTH required concepts (transaction_date AND
    revenue) are present, exist, and pass their checks. All other findings are
    collected as human-readable ``issues`` without forcing ``ok=False`` on
    their own (e.g. a non-numeric optional quantity).
    """
    issues: list[str] = []
    index = _index_columns(tables or [])

    raw_concepts = (mapping or {}).get("concepts")
    if not isinstance(raw_concepts, dict):
        raw_concepts = {}

    cleaned: dict[str, dict[str, str]] = {}

    for concept in _ALL_CONCEPTS:
        entry = raw_concepts.get(concept)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            issues.append(f"{concept}: malformed entry (expected an object)")
            continue
        table = entry.get("table")
        column = entry.get("column")
        if not isinstance(table, str) or not isinstance(column, str) \
                or not table or not column:
            issues.append(f"{concept}: missing table/column")
            continue
        col_type = _lookup_column_type(index, table, column)
        if col_type is None:
            issues.append(
                f"{concept}: {table}.{column} does not exist in the data"
            )
            continue
        cleaned[concept] = {"table": table, "column": column}

        # Per-concept type sanity checks.
        if concept in ("revenue", "quantity"):
            numeric = _type_is_numeric(col_type)
            if numeric is False:
                issues.append(
                    f"{concept}: {table}.{column} type {col_type!r} is not numeric"
                )
            elif numeric is None:
                issues.append(
                    f"{concept}: {table}.{column} type {col_type or 'unknown'!r} "
                    "could not be confirmed numeric"
                )
        if concept == "transaction_date":
            if not _looks_like_date(col_type, column):
                issues.append(
                    f"transaction_date: {table}.{column} (type {col_type or 'unknown'!r}) "
                    "does not look like a date by type or name"
                )

    # ok requires BOTH required concepts present, existing, and not flagged with
    # a hard (existence/non-numeric/not-a-date) failure.
    def _hard_failed(concept: str) -> bool:
        if concept not in cleaned:
            return True
        t = cleaned[concept]["table"]
        c = cleaned[concept]["column"]
        ct = _lookup_column_type(index, t, c)
        if concept == "revenue" and _type_is_numeric(ct) is False:
            return True
        if concept == "transaction_date" and not _looks_like_date(ct, c):
            return True
        return False

    missing_required = [c for c in _REQUIRED_CONCEPTS if c not in cleaned]
    if missing_required:
        issues.append(
            "required concept(s) not mapped: " + ", ".join(missing_required)
        )

    ok = not any(_hard_failed(c) for c in _REQUIRED_CONCEPTS)

    cleaned_mapping = {
        "concepts": cleaned,
        "source": (mapping or {}).get("source", "llm"),
        "updated_at": (mapping or {}).get("updated_at") or _now_iso(),
    }
    return {"ok": ok, "issues": issues, "mapping": cleaned_mapping}


__all__ = ["propose_mapping", "validate_mapping"]
