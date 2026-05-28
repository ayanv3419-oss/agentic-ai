"""
Schema tool - returns a compact summary of every table the Coordinator
can query: the static system tables (sales, purchase) and any
dynamically-ingested user tables (``u_*``).

When the user has uploaded data, the u_* tables hold the REAL data and
must be queried. The static sales/purchase tables are a legacy fallback
that is often empty or holds stale test rows - the summary makes this
explicit with row counts so the LLM never analyses the wrong table.
"""
from __future__ import annotations

import re
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.infrastructure import (
    ALLOWED_TABLES,
    SCHEMA_SPEC,
    get_connection,
    quoted,
)
from app.schema_mapping import ResolvedSchema, resolve_schema


async def _table_row_count(db: Any, name: str) -> int:
    """Best-effort COUNT(*) for a table. Returns 0 on any error so the
    schema summary never fails just because one table is unreadable."""
    try:
        cur = await db.execute(f"SELECT COUNT(*) AS n FROM {quoted(name)}")
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return 0
        return int(dict(row).get("n", 0) or 0)
    except Exception:
        return 0


async def _date_range_for_table(
    db: Any, name: str, date_col: str,
) -> tuple[str, str] | None:
    """Return (min_date, max_date) for a table's date column, or None."""
    try:
        cur = await db.execute(
            f"SELECT MIN({quoted(date_col)}), MAX({quoted(date_col)}) "
            f"FROM {quoted(name)}"
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            vals = list(dict(row).values())
            if vals[0] and vals[1]:
                return str(vals[0])[:10], str(vals[1])[:10]
    except Exception:
        pass
    return None


async def _list_user_tables() -> list[dict[str, Any]]:
    """Inspect the DB for u_* (dynamic) tables + their columns + row counts.

    Also records date_range=(min, max) for any table that has a recognisable
    date column, so the schema summary can warn the LLM when a table's data
    is stale (e.g. only covers May–Jul 2025 while queries target 2026).

    Branches on the active engine: SQLite uses sqlite_master + PRAGMA;
    Postgres uses information_schema.
    """
    from app.db_engine import is_postgres
    out: list[dict[str, Any]] = []
    _date_col_hint = re.compile(r"(date|txn_date|transaction_date)", re.I)
    async with get_connection() as db:
        if is_postgres():
            # Postgres: list u_* tables from information_schema.
            cur = await db.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name LIKE 'u\\_%' ESCAPE '\\' "
                "ORDER BY table_name"
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                name = dict(r).get("name") or ""
                if not name:
                    continue
                cur2 = await db.execute(
                    "SELECT column_name AS name, data_type AS type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = ? "
                    "ORDER BY ordinal_position",
                    (name,),
                )
                cols = await cur2.fetchall()
                await cur2.close()
                col_defs = [
                    {"name": dict(c)["name"], "type": (dict(c)["type"] or "text").upper()}
                    for c in cols
                ]
                # Attach data-quality profile to each column when available.
                # `_column_profile` is populated by the ingest profiler; if
                # profiling failed or the row hasn't been written yet, the
                # dict is empty and we just skip the merge.
                try:
                    from app.schema_mapping.profiler import load_table_profile_pg
                    profile = await load_table_profile_pg(name)
                except Exception:
                    profile = {}
                for c in col_defs:
                    p = profile.get(c["name"])
                    if p:
                        c["profile"] = p
                date_col = next(
                    (c["name"] for c in col_defs
                     if _date_col_hint.search(c["name"])),
                    None,
                )
                date_range: tuple[str, str] | None = None
                if date_col:
                    date_range = await _date_range_for_table(db, name, date_col)
                out.append({
                    "table": name,
                    "row_count": await _table_row_count(db, name),
                    "columns": col_defs,
                    "date_range": date_range,
                })
            return out

        # SQLite path
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'u\\_%' ESCAPE '\\' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
        await cur.close()
        for r in rows:
            name = dict(r).get("name") or ""
            if not name:
                continue
            cur2 = await db.execute(f"PRAGMA table_info({quoted(name)})")
            cols = await cur2.fetchall()
            await cur2.close()
            col_defs = [
                {"name": dict(c)["name"], "type": dict(c)["type"] or "TEXT"}
                for c in cols
            ]
            date_col = next(
                (c["name"] for c in col_defs
                 if _date_col_hint.search(c["name"])),
                None,
            )
            date_range = None
            if date_col:
                date_range = await _date_range_for_table(db, name, date_col)
            out.append({
                "table": name,
                "row_count": await _table_row_count(db, name),
                "columns": col_defs,
                "date_range": date_range,
            })
    return out


def _system_schema() -> list[dict[str, Any]]:
    common = [
        {"name": "id",            "type": "INTEGER", "role": "pk"},
        {"name": "batch_id",      "type": "TEXT",    "role": "audit"},
        {"name": "source",        "type": "TEXT",    "role": "audit"},
        {"name": "file_name",     "type": "TEXT",    "role": "audit"},
        {"name": "row_hash",      "type": "TEXT",    "role": "audit"},
        {"name": "inserted_at",   "type": "TEXT",    "role": "audit"},
    ]
    cols = common + [
        {"name": n, "type": t, "required": r}
        for n, t, r in SCHEMA_SPEC
    ]
    return [
        {
            "table": t,
            "description": (
                "Customer sales transactions"
                if t == "sales" else "Supplier purchase transactions"
            ),
            "columns": cols,
        }
        for t in ALLOWED_TABLES
    ]


def _margin_hints(resolved: ResolvedSchema) -> str:
    """Emit the ## METRIC DEFINITIONS block (margin / profit / revenue).

    Returns either the full formula block when margin is computable, or
    a DATA CAPABILITY note describing what is missing. Callers always
    concatenate this block to the schema summary; it never returns ''.
    """
    if not resolved.can_compute_margin:
        # Margin / profit cannot be computed. Tell the LLM explicitly so
        # it does not improvise a query that will fail.
        missing = resolved.missing(
            "revenue", "quantity", "sku_key", "unit_cost", "product_label",
        )
        if missing:
            reason = "no column matches these concepts: " + ", ".join(missing)
        else:
            reason = ("the sales and cost data share no common key column "
                      "to join on")
        return (
            "\n\n## DATA CAPABILITY - margin, profit and cost-of-goods "
            "CANNOT be computed for this dataset: " + reason + ". "
            "If the user asks about margin or profit, state plainly that "
            "the uploaded data lacks the needed columns - do NOT write a "
            "margin or profit query."
        )

    rev = resolved.ref("revenue")
    qty = resolved.ref("quantity")
    cost = resolved.ref("unit_cost")
    prod = resolved.ref("product_label")
    sku = resolved.ref("sku_key")
    revenue = f"SUM(s.{rev.column})"
    cogs = f"SUM(s.{qty.column} * i.{cost.column})"
    join = (f'FROM "{rev.table}" s JOIN "{cost.table}" i '
            f"ON s.{sku.column} = i.{sku.column}")
    return (
        "\n\n## METRIC DEFINITIONS - use these EXACT formulas. "
        "Do NOT invent your own margin / profit math.\n"
        f"- Realized margin %: {join}, GROUP BY s.{prod.column}, then\n"
        f"    margin_pct = ({revenue} - {cogs}) * 100.0 / {revenue}\n"
        "  It is profit on what actually sold, after discounts. For "
        "'top products by margin' ORDER BY margin_pct DESC.\n"
        f"- Gross / total profit (ONE number, no GROUP BY): "
        f"{revenue} - {cogs}, {join}.\n"
        f"- Profit by month / category / brand: the SAME "
        f"{revenue} - {cogs} difference, GROUP BY the relevant column "
        "(for monthly, group on the month part of the date column).\n"
        f"- Profit amount per product (ranking): the same difference, "
        f"GROUP BY s.{prod.column}, ORDER BY it DESC.\n"
        f'- Revenue / total sales: {revenue} from "{rev.table}".\n'
        f'- Units sold: SUM(s.{qty.column}) from "{rev.table}".\n'
        f"- Cost of goods: {cogs} via the join above.\n"
        f'- Never say "no cost data" - unit cost lives in "{cost.table}".'
    )


def _inventory_hints(resolved: ResolvedSchema) -> str:
    """Emit the ## INVENTORY METRICS block (or a DATA CAPABILITY note).

    Velocity-based inventory status requires stock_qty + sku_key from the
    inventory side, plus quantity + transaction_date from the sales side
    so we can compute average daily sales and recency. When stock_qty
    resolves but the velocity inputs do not, we emit a smaller capability
    note instead so the LLM does not improvise.
    """
    stock = resolved.ref("stock_qty")
    if stock is None:
        return ""

    sku = resolved.ref("sku_key")
    qty = resolved.ref("quantity")
    txn_date = resolved.ref("transaction_date")

    if sku is None or qty is None or txn_date is None:
        return (
            "\n\n## INVENTORY DATA CAPABILITY - current stock is queryable "
            f"from \"{stock.table}\".{stock.column}, but velocity-based "
            "inventory status (low / dead / overstocked) CANNOT be "
            "derived: this dataset is missing one of "
            "quantity / sku_key / transaction_date on the sales side. "
            "Do NOT improvise a velocity formula. If asked about dead "
            "stock or low stock, state plainly that the upload lacks the "
            "sales-velocity columns needed to compute it."
        )

    # We have everything: emit explicit SQL the LLM can copy verbatim.
    inv_t = stock.table
    sales_t = qty.table
    stk = f'"{stock.column}"'
    sku_col = f'"{sku.column}"'
    qcol = f'"{qty.column}"'
    dcol = f'"{txn_date.column}"'

    # Common CTEs computing per-SKU velocity + dataset max date so the
    # individual SQL blocks below stay short and copy-pasteable.
    velocity_cte = (
        f"WITH ds AS (SELECT MAX({dcol}) AS max_date FROM \"{sales_t}\"),\n"
        f"     v AS (\n"
        f"       SELECT s.{sku_col} AS sku,\n"
        f"              SUM(s.{qcol}) * 1.0 / NULLIF(\n"
        f"                JULIANDAY(MAX(s.{dcol})) - JULIANDAY(MIN(s.{dcol})) + 1, 0\n"
        f"              ) AS avg_daily_sales,\n"
        f"              MAX(s.{dcol}) AS last_sale_date\n"
        f"       FROM \"{sales_t}\" s\n"
        f"       GROUP BY s.{sku_col}\n"
        f"     )"
    )

    return (
        "\n\n## INVENTORY METRICS - use these EXACT formulas. Inventory "
        "status is VELOCITY-BASED (dead = no sales in last 30 days; "
        "low = days_of_cover < 7; overstocked = days_of_cover > 60; else ok). "
        f"Do NOT use any \"status\" / \"availability\" column literally — "
        "the workbook value (if any) is unreliable. Always derive status "
        "from sales velocity.\n"
        f"\n- Current stock per SKU:\n"
        f"    SELECT {sku_col}, {stk} FROM \"{inv_t}\";\n"
        f"\n- Days of cover per SKU (stock / avg daily sales):\n"
        f"    {velocity_cte}\n"
        f"    SELECT i.{sku_col},\n"
        f"           i.{stk} AS stock,\n"
        f"           v.avg_daily_sales,\n"
        f"           i.{stk} * 1.0 / NULLIF(v.avg_daily_sales, 0) AS days_of_cover\n"
        f"    FROM \"{inv_t}\" i LEFT JOIN v ON v.sku = i.{sku_col};\n"
        f"\n- Inventory status per SKU (velocity-based):\n"
        f"    {velocity_cte}\n"
        f"    SELECT i.{sku_col},\n"
        f"           CASE\n"
        f"             WHEN v.last_sale_date IS NULL\n"
        f"               OR JULIANDAY((SELECT max_date FROM ds))\n"
        f"                  - JULIANDAY(v.last_sale_date) > 30 THEN 'dead'\n"
        f"             WHEN i.{stk} * 1.0 / NULLIF(v.avg_daily_sales, 0) < 7 THEN 'low'\n"
        f"             WHEN i.{stk} * 1.0 / NULLIF(v.avg_daily_sales, 0) > 60 THEN 'overstocked'\n"
        f"             ELSE 'ok'\n"
        f"           END AS inventory_status\n"
        f"    FROM \"{inv_t}\" i LEFT JOIN v ON v.sku = i.{sku_col}, ds;\n"
        f"\n- Count of LOW-stock SKUs (copy verbatim):\n"
        f"    {velocity_cte}\n"
        f"    SELECT COUNT(*) AS low_stock_skus\n"
        f"    FROM \"{inv_t}\" i JOIN v ON v.sku = i.{sku_col}\n"
        f"    WHERE i.{stk} * 1.0 / NULLIF(v.avg_daily_sales, 0) < 7;\n"
        f"\n- Count of DEAD-stock SKUs (copy verbatim):\n"
        f"    {velocity_cte}\n"
        f"    SELECT COUNT(*) AS dead_stock_skus\n"
        f"    FROM \"{inv_t}\" i LEFT JOIN v ON v.sku = i.{sku_col}, ds\n"
        f"    WHERE v.last_sale_date IS NULL\n"
        f"       OR JULIANDAY((SELECT max_date FROM ds))\n"
        f"          - JULIANDAY(v.last_sale_date) > 30;\n"
        f"\n- Count of OVERSTOCKED SKUs (copy verbatim):\n"
        f"    {velocity_cte}\n"
        f"    SELECT COUNT(*) AS overstocked_skus\n"
        f"    FROM \"{inv_t}\" i JOIN v ON v.sku = i.{sku_col}\n"
        f"    WHERE i.{stk} * 1.0 / NULLIF(v.avg_daily_sales, 0) > 60;"
    )


def _profile_note(col: dict[str, Any]) -> str:
    """Render the profile dict on a column into a short inline annotation
    the LLM can read. Returns '' when no profile is attached or all stats
    are unremarkable.

    Format: ' [null=N% num=N% date=N%]' — only includes fields that carry
    signal (e.g. omit num=100% on a pure-numeric column, but include
    null=42% which would warn the LLM the column is sparse).
    """
    p = col.get("profile")
    if not p:
        return ""
    parts: list[str] = []
    nn = float(p.get("pct_non_null") or 0.0)
    if nn < 1.0:
        parts.append(f"null={int(round((1.0 - nn) * 100))}%")
    nm = float(p.get("pct_numeric") or 0.0)
    if 0.05 < nm < 1.0:
        # Mixed-type column — partly numeric, partly text. LLM should be wary
        # of SUM() unless it casts and filters first.
        parts.append(f"num={int(round(nm * 100))}%")
    dc = float(p.get("pct_date") or 0.0)
    if 0.5 < dc < 1.0:
        parts.append(f"date={int(round(dc * 100))}%")
    return f" [{' '.join(parts)}]" if parts else ""


def _data_quality_warnings(user_tables: list[dict[str, Any]]) -> str:
    """Emit a ## DATA QUALITY block listing columns the LLM should AVOID
    using as a measure or key — typically those that are mostly NULL.

    Returns '' when no column crosses the warning threshold (≥70% null).
    """
    bad: list[str] = []
    for t in user_tables:
        for c in t.get("columns", []):
            p = c.get("profile")
            if not p:
                continue
            nn = float(p.get("pct_non_null") or 0.0)
            if nn <= 0.30 and p.get("distinct_count", 0) > 0:
                bad.append(
                    f'"{t["table"]}"."{c["name"]}" is '
                    f"{int(round((1.0 - nn) * 100))}% NULL"
                )
    if not bad:
        return ""
    return (
        "\n\n## DATA QUALITY - the columns below are mostly NULL. Do NOT "
        "use them as a measure (SUM/AVG would be misleading) or as a key "
        "(joins would drop most rows). Prefer cleaner columns when "
        "alternatives exist.\n- "
        + "\n- ".join(bad)
    )


async def _load_pg_foreign_keys(live_tables: set[str]) -> list[dict[str, Any]]:
    """Return every FOREIGN KEY whose BOTH endpoints sit in ``live_tables``.

    Read straight from information_schema so manually-added FKs (via
    Supabase SQL editor) are picked up alongside any our ingest pipeline
    might create. Each row: {from_table, from_column, to_table, to_column,
    constraint_name}. Empty list on any error so a missing GRANT never
    breaks schema emission.
    """
    if not live_tables:
        return []
    from app.db_engine import pg_connection
    out: list[dict[str, Any]] = []
    sql = (
        "SELECT tc.table_name      AS from_table, "
        "       kcu.column_name    AS from_column, "
        "       ccu.table_name     AS to_table, "
        "       ccu.column_name    AS to_column, "
        "       tc.constraint_name AS constraint_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema    = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON tc.constraint_name = ccu.constraint_name "
        " AND tc.table_schema    = ccu.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' "
        "  AND tc.table_schema    = 'public'"
    )
    try:
        async with pg_connection() as db:
            cur = await db.execute(sql)
            rows = await cur.fetchall()
            for r in rows or []:
                d = dict(r)
                ft, tt = d.get("from_table"), d.get("to_table")
                if ft in live_tables and tt in live_tables:
                    out.append(d)
    except Exception:
        return []
    return out


async def _relationship_hints(user_tables: list[dict[str, Any]]) -> str:
    """Emit the ## TABLE RELATIONSHIPS block.

    Merges TWO sources of join evidence:
      1. ``_relationships`` — value-overlap auto-detector (≥80% match).
      2. ``information_schema.referential_constraints`` — real Postgres
         FOREIGN KEY constraints, treated as AUTHORITATIVE. When a pair
         appears in both, the FK wins (auto-detector entry is dropped).

    Only emits relationships whose endpoints are CURRENTLY in
    ``user_tables`` — stale detection rows / FKs against dropped tables
    are silently filtered.

    SQLite-only deployments return '' (no Postgres info_schema).
    """
    if not user_tables:
        return ""
    from app.db_engine import is_postgres
    if not is_postgres():
        return ""

    live_tables = {t["table"] for t in user_tables}

    # Load both sources concurrently — neither blocks the other on failure.
    try:
        from app.schema_mapping.relationships import load_relationships_pg
        rels = await load_relationships_pg()
    except Exception:
        rels = []
    fks = await _load_pg_foreign_keys(live_tables)

    # Dedupe: a pair is identified by the unordered set of
    # (table.column, table.column). When an FK exists for the pair, the
    # auto-detector entry is dropped — the FK is the source of truth.
    def _pair_key(a_t: str, a_c: str, b_t: str, b_c: str) -> tuple:
        return tuple(sorted([(a_t, a_c), (b_t, b_c)]))

    fk_pairs = {
        _pair_key(f["from_table"], f["from_column"], f["to_table"], f["to_column"])
        for f in fks
    }

    visible_auto = [
        r for r in rels
        if r["from_table"] in live_tables
        and r["to_table"] in live_tables
        and _pair_key(r["from_table"], r["from_column"],
                      r["to_table"], r["to_column"]) not in fk_pairs
    ]

    if not fks and not visible_auto:
        return ""

    lines = [
        "\n\n## TABLE RELATIONSHIPS - join keys across the uploaded tables. "
        "Use these EXACT keys when a question needs data from more than one "
        "table. Prefer LEFT JOIN when the LEFT table's totals must be "
        "preserved (orphan keys are common in real uploads)."
    ]
    if fks:
        lines.append(
            "\n# Declared foreign keys (authoritative — enforced by the DB):"
        )
        for f in fks:
            lines.append(
                f'- "{f["from_table"]}"."{f["from_column"]}" '
                f'= "{f["to_table"]}"."{f["to_column"]}"'
            )
    if visible_auto:
        lines.append(
            "\n# Detected by value overlap (no FK declared — treat as a hint):"
        )
        for r in visible_auto:
            pct = int(round(float(r.get("overlap_pct") or 0.0) * 100))
            lines.append(
                f'- "{r["from_table"]}"."{r["from_column"]}" '
                f'= "{r["to_table"]}"."{r["to_column"]}"  '
                f'(overlap {pct}%, distinct {r.get("from_distinct", 0)} '
                f'/ {r.get("to_distinct", 0)})'
            )
    return "\n".join(lines)


def _metric_hints(
    user_tables: list[dict[str, Any]], resolved: ResolvedSchema,
) -> str:
    """Emit margin/profit + inventory guidance for the LLM from the resolved
    schema. Each block is built with ACTUAL resolved column names so the SQL
    snippets adapt to whatever the workbook named its columns.

    Returns '' when no user tables exist.
    """
    if not user_tables:
        return ""
    return _margin_hints(resolved) + _inventory_hints(resolved)


class SchemaTool(Tool):
    name = "Schema"
    description = (
        "Return the full database schema WITH row counts. When the user "
        "has uploaded data, the u_* tables are the REAL data to query - "
        "the sales/purchase system tables are only a legacy fallback. "
        "Call this FIRST before writing any SQL."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "include_user_tables": {
                "type": "boolean",
                "default": True,
                "description": "Whether to include dynamically-ingested u_* tables.",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        include_user = bool(args.get("include_user_tables", True))
        system = _system_schema()
        user_tables: list[dict[str, Any]] = []
        if include_user:
            try:
                user_tables = await _list_user_tables()
            except Exception as e:
                return ToolOutcome(
                    ok=False,
                    error=f"failed to list dynamic tables: {e}",
                )

        # Row counts for the system tables so the LLM can see at a glance
        # whether sales/purchase actually hold data or are stale/empty.
        sys_counts: dict[str, int] = {}
        async with get_connection() as db:
            for t in system:
                sys_counts[t["table"]] = await _table_row_count(db, t["table"])

        summary_lines = ["# Database schema (sqlite)"]

        if user_tables:
            summary_lines.append(
                "\n## PRIMARY DATA - the user's uploaded tables. "
                "These hold the real business data. ALWAYS query THESE "
                "tables to answer the question. Join them on shared keys "
                "(e.g. invoice_no) when a question spans more than one."
            )
            for t in user_tables:
                cols = ", ".join(
                    f'"{c["name"]}":{c["type"]}{_profile_note(c)}'
                    for c in t["columns"]
                )
                dr = t.get("date_range")
                date_note = (
                    f" [dates: {dr[0]} to {dr[1]}]" if dr else ""
                )
                summary_lines.append(
                    f'- "{t["table"]}" ({t["row_count"]} rows{date_note}): {cols}'
                )
            summary_lines.append(
                "\n## Legacy system tables - fallback ONLY. Do NOT query "
                "these when PRIMARY tables exist above; they hold stale or "
                "empty test data."
            )

        for t in system:
            cols = ", ".join(
                f'"{c["name"]}":{c["type"]}'
                for c in t["columns"][6:]   # skip audit cols in the summary
            )
            n = sys_counts.get(t["table"], 0)
            summary_lines.append(
                f'- "{t["table"]}" ({n} rows, {t["description"]}): {cols}'
            )

        if not user_tables:
            summary_lines.append(
                "\n# No user-uploaded tables present yet. "
                "Only system tables are available."
            )

        # Resolve canonical analytic concepts onto this dataset's columns
        # once; sqlWriter's deterministic ranking path reuses the result.
        resolved = resolve_schema(user_tables)
        rel_block = await _relationship_hints(user_tables)
        text = (
            "\n".join(summary_lines)
            + rel_block
            + _data_quality_warnings(user_tables)
            + _metric_hints(user_tables, resolved)
        )
        return ToolOutcome(
            ok=True,
            output={
                "summary": text,
                "system_tables": system,
                "user_tables": user_tables,
            },
            state_updates={"schema_summary": text, "resolved_schema": resolved},
        )


__all__ = ["SchemaTool"]
