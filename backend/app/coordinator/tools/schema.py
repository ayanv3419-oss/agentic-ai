"""
Schema tool - returns a compact summary of every table the Coordinator
can query: the static system tables (sales, purchase, KPIs, hierarchy)
and any dynamically-ingested user tables (``u_*``).
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


async def _list_user_tables() -> list[dict[str, Any]]:
    """Inspect SQLite for u_* (dynamic) tables + their columns."""
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


class SchemaTool(Tool):
    name = "Schema"
    description = (
        "Return the full database schema: system tables (sales, purchase) "
        "and any user-uploaded dynamic tables (u_*). Call this FIRST before "
        "writing SQL so you know exactly which tables and columns exist."
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

        summary_lines = ["# Database schema (sqlite)"]
        for t in system:
            cols = ", ".join(
                f'"{c["name"]}":{c["type"]}'
                for c in t["columns"][6:]   # skip audit cols in the summary
            )
            summary_lines.append(f'- "{t["table"]}" ({t["description"]}): {cols}')
        if user_tables:
            summary_lines.append("\n# User-uploaded dynamic tables (prefix u_)")
            for t in user_tables:
                cols = ", ".join(
                    f'"{c["name"]}":{c["type"]}' for c in t["columns"]
                )
                summary_lines.append(f'- "{t["table"]}": {cols}')
        else:
            summary_lines.append(
                "\n# No user-uploaded tables present yet. "
                "Only system tables are available."
            )

        text = "\n".join(summary_lines)
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
