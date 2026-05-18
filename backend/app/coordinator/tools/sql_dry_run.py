"""
SqlDryRun - validate a SELECT statement without executing it.

Checks:
  * Only one statement (no semicolon-separated multi-statement payloads).
  * Starts with SELECT or WITH (no INSERT/UPDATE/DELETE/DROP/etc.).
  * No dangerous keywords anywhere.
  * SQLite parser accepts it (via EXPLAIN).

Used by the Coordinator before SqlExecutor so bad SQL is caught with no
data side-effects.
"""
from __future__ import annotations

import re
from typing import Any

from app.coordinator.tools.base import Tool, ToolContext, ToolOutcome
from app.infrastructure import get_connection


_DANGEROUS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|"
    r"pragma|vacuum|reindex|replace)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    # Strip line + block comments to keep keyword scanning honest.
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def _validate_shape(sql: str) -> str | None:
    text = sql.strip()
    if not text:
        return "SQL is empty"
    stripped = _strip_comments(text)
    # Collapse trailing semicolons, then reject multi-statement.
    stripped = stripped.rstrip(";").strip()
    if ";" in stripped:
        return "Multi-statement SQL is not allowed"
    head = stripped.split(None, 1)[0].upper() if stripped else ""
    if head not in ("SELECT", "WITH"):
        return f"Only SELECT/WITH allowed - got {head!r}"
    m = _DANGEROUS.search(stripped)
    if m:
        return f"Disallowed keyword: {m.group(0).upper()}"
    return None


class SqlDryRunTool(Tool):
    name = "SqlDryRun"
    description = (
        "Validate a SELECT (or WITH ... SELECT) statement WITHOUT running "
        "it. Returns ok=true when the SQL is safe to execute; ok=false "
        "with a reason otherwise. Always call this before SqlExecutor."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SELECT statement to validate.",
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        sql = str(args.get("sql") or "").strip()
        shape_err = _validate_shape(sql)
        if shape_err is not None:
            return ToolOutcome(
                ok=False,
                error=shape_err,
                output={"valid": False, "reason": shape_err},
            )
        try:
            async with get_connection() as db:
                cur = await db.execute(f"EXPLAIN {sql}")
                _ = await cur.fetchall()
                await cur.close()
        except Exception as e:
            return ToolOutcome(
                ok=False,
                error=f"sqlite rejected SQL: {e}",
                output={"valid": False, "reason": str(e)},
            )
        return ToolOutcome(
            ok=True,
            output={"valid": True, "sql": sql},
            state_updates={"sql_draft": sql},
        )


__all__ = ["SqlDryRunTool"]
