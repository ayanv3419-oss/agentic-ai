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


async def _list_user_tables() -> list[dict[str, Any]]:
    """Inspect SQLite for u_* (dynamic) tables + their columns + row counts."""
    out: list[dict[str, Any]] = []
    async with get_connection() as db:
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
            out.append({
                "table": name,
                "row_count": await _table_row_count(db, name),
                "columns": [
                    {"name": dict(c)["name"], "type": dict(c)["type"] or "TEXT"}
                    for c in cols
                ],
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


def _metric_hints(
    user_tables: list[dict[str, Any]], resolved: ResolvedSchema,
) -> str:
    """Emit margin / profit guidance for the LLM from the resolved schema.

    When the realized-margin formula is computable on this dataset
    (``resolved.can_compute_margin``), emit a '## METRIC DEFINITIONS'
    block with the EXACT formulas, written with the ACTUAL resolved
    column names so it adapts to whatever the workbook called them.

    Otherwise emit a '## DATA CAPABILITY' note naming what is missing, so
    the LLM states plainly that margin/profit cannot be computed rather
    than emitting SQL that fails. Returns '' when no user tables exist."""
    if not user_tables:
        return ""

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
                    f'"{c["name"]}":{c["type"]}' for c in t["columns"]
                )
                summary_lines.append(
                    f'- "{t["table"]}" ({t["row_count"]} rows): {cols}'
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
        text = "\n".join(summary_lines) + _metric_hints(user_tables, resolved)
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
