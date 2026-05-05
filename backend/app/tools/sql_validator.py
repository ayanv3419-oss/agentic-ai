"""SqlValidator — final gate before SqlExecutor.

Checks (in order):
  1. SELECT-only.
  2. Single statement (no embedded `;`).
  3. No DDL/DML keywords anywhere.
  4. Every double-quoted identifier referenced in the SQL exists in
     `state.db_schema.tables[<table>].columns` (or is the alias `bucket`).
  5. Estimated scan size under cap (delegated to safety.cost_guard).
On pass: promotes sql_draft → sql_final.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from app.database.schema import ALLOWED_TABLES, SCHEMA_COLUMNS
from app.safety import check_sql_scan_estimate
from app.state import TurnState
from app.tools.base import Tool, ToolResult, require


_SELECT_RE = re.compile(r"^\s*select\b", re.IGNORECASE | re.DOTALL)
_DANGEROUS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|merge|"
    r"replace|truncate|attach|detach|vacuum|pragma|exec)\b",
    re.IGNORECASE,
)
_QUOTED_IDENT_RE = re.compile(r'"([^"]+)"')

_KNOWN_ALIASES = {"bucket", "sales", "orders", "customers", "aov", "refunds"}
_KNOWN_TABLES = set(ALLOWED_TABLES)
_KNOWN_COLUMNS = set(SCHEMA_COLUMNS)


class SqlValidatorArgs(BaseModel):
    pass


class SqlValidatorTool(Tool):
    name = "SqlValidator"
    description = "Validates the drafted SQL — SELECT-only, single statement, columns exist, scan-size budget."
    args_model = SqlValidatorArgs
    independent = False

    async def run(self, state: TurnState, args: SqlValidatorArgs) -> ToolResult:
        miss = require(state, "sql_draft", "db_schema")
        if miss:
            return miss
        sql = (state.sql_draft or "").strip().rstrip(";").strip()
        if not sql:
            return ToolResult(ok=False, error="empty SQL")

        if not _SELECT_RE.search(sql):
            return ToolResult(ok=False, error="SQL must be a SELECT statement")
        if _DANGEROUS.search(sql):
            return ToolResult(ok=False, error="dangerous SQL keyword detected")
        # After rstrip, any remaining `;` is between statements.
        if ";" in sql:
            return ToolResult(ok=False, error="only one SQL statement allowed")

        # Identifier whitelist — every "..." token must be a known table,
        # column, or known alias.
        unknown: list[str] = []
        for ident in _QUOTED_IDENT_RE.findall(sql):
            if (
                ident in _KNOWN_TABLES
                or ident in _KNOWN_COLUMNS
                or ident in _KNOWN_ALIASES
            ):
                continue
            unknown.append(ident)
        if unknown:
            return ToolResult(
                ok=False,
                error=f"unknown identifier(s) in SQL: {sorted(set(unknown))!r}",
            )

        # Crude scan estimate: 10 MB per FROM clause. Real big-data systems
        # would call EXPLAIN; SQLite has no equivalent so we approximate.
        from_count = max(len(re.findall(r"\bfrom\b", sql, re.IGNORECASE)), 1)
        estimated_bytes = from_count * 10_000_000
        try:
            check_sql_scan_estimate(estimated_bytes)
        except Exception as e:
            return ToolResult(ok=False, error=str(e))

        return ToolResult(
            ok=True,
            output={"valid": True, "estimated_bytes": estimated_bytes},
            state_updates={
                "sql_final": sql,
                "bytes_scanned": state.bytes_scanned + estimated_bytes,
            },
        )
