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


def _metric_hints(user_tables: list[dict[str, Any]]) -> str:
    """Emit margin / profit guidance for the LLM, based on the uploaded
    data's column shape.

    When the data has a sales table exposing ``net_sales``, ``quantity``,
    ``sku_id`` AND ``final_product``, plus a cost table exposing
    ``unit_cost`` and ``sku_id``, emit a '## METRIC DEFINITIONS' block
    with the EXACT formulas (margin %, gross profit, profit-by-group,
    profit ranking) so the LLM copies them verbatim instead of guessing.

    ``final_product`` is required because every formula GROUPs/labels on
    it - detecting only the numeric columns and then hard-coding
    ``final_product`` in the SQL is the bug that produced 'no such
    column'.

    When that shape is ABSENT (but data IS uploaded), emit a
    '## DATA CAPABILITY' note instead, naming what is missing, so the LLM
    states plainly that margin/profit cannot be computed rather than
    emitting SQL that fails. Returns '' only when no user tables exist."""
    def cols_of(t: dict[str, Any]) -> set[str]:
        return {str(c["name"]).lower() for c in t.get("columns", [])}

    if not user_tables:
        return ""

    sales_t: str | None = None
    cost_t: str | None = None
    has_unit_cost = False
    for t in user_tables:
        cl = cols_of(t)
        if "unit_cost" in cl:
            has_unit_cost = True
        if sales_t is None and {
            "net_sales", "quantity", "sku_id", "final_product",
        } <= cl:
            sales_t = t["table"]
        if cost_t is None and {"unit_cost", "sku_id"} <= cl:
            cost_t = t["table"]

    if not sales_t or not cost_t:
        # Margin / profit cannot be computed on this dataset. Tell the LLM
        # explicitly so it does not improvise a query that will fail.
        if not has_unit_cost:
            reason = "no unit-cost column was found in any uploaded table"
        elif not sales_t:
            reason = ("no sales table exposing net_sales, quantity, sku_id "
                      "and final_product was found")
        else:
            reason = "the sales and cost tables share no sku_id key"
        return (
            "\n\n## DATA CAPABILITY - margin, profit and cost-of-goods "
            "CANNOT be computed for this dataset: " + reason + ". "
            "If the user asks about margin or profit, state plainly that "
            "the uploaded data carries no cost information - do NOT write "
            "a margin or profit query."
        )

    return (
        "\n\n## METRIC DEFINITIONS - use these EXACT formulas. "
        "Do NOT invent your own margin / profit math.\n"
        f'- Realized margin %: JOIN "{sales_t}" s to "{cost_t}" i '
        "ON s.sku_id = i.sku_id, GROUP BY s.final_product, then\n"
        "    margin_pct = (SUM(s.net_sales) - SUM(s.quantity * i.unit_cost)) "
        "* 100.0 / SUM(s.net_sales)\n"
        "  It is profit on what actually sold, after discounts. For "
        "'top products by margin' ORDER BY margin_pct DESC.\n"
        f'- Gross / total profit (ONE number, no GROUP BY): '
        f'SUM(s.net_sales) - SUM(s.quantity * i.unit_cost), JOIN "{sales_t}" '
        f's to "{cost_t}" i ON s.sku_id = i.sku_id.\n'
        "- Profit by month / category / brand: the SAME "
        "SUM(s.net_sales) - SUM(s.quantity * i.unit_cost) difference, "
        "GROUP BY the relevant column (for monthly, group on the month "
        "part of the date column).\n"
        "- Profit amount per product (ranking): the same difference, "
        "GROUP BY s.final_product, ORDER BY it DESC.\n"
        f'- Revenue / total sales: SUM(net_sales) from "{sales_t}".\n'
        f'- Units sold: SUM(quantity) from "{sales_t}".\n'
        f'- Cost of goods: SUM(s.quantity * i.unit_cost) via the join above.\n'
        f'- Never say "no cost data" - unit_cost lives in "{cost_t}".'
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

        text = "\n".join(summary_lines) + _metric_hints(user_tables)
        return ToolOutcome(
            ok=True,
            output={
                "summary": text,
                "system_tables": system,
                "user_tables": user_tables,
            },
            state_updates={"schema_summary": text},
        )


__all__ = ["SchemaTool"]
