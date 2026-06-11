"""
SqlDryRun - validate a SELECT statement without executing it.

Checks:
  * Only one statement (no semicolon-separated multi-statement payloads).
  * Starts with SELECT or WITH (no INSERT/UPDATE/DELETE/DROP/etc.).
  * No dangerous keywords anywhere (incl. MERGE/COPY/CALL/DO and
    SELECT ... INTO materialization).
  * No references to system/metadata catalogs (information_schema,
    pg_catalog / pg_*, sqlite_master / sqlite_*).
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
    r"pragma|vacuum|reindex|replace|merge|copy|call|do)\b",
    re.IGNORECASE,
)

# System / metadata catalogs. A read-only analytics SELECT never needs these;
# referencing them is a schema-introspection / data-exfiltration signal. Matched
# on word boundaries, case-insensitively. NOTE: this deliberately does NOT block
# arbitrary `schema.table` qualifiers (that would hit legit `alias.column`) — it
# only blocks the known system-metadata names. Legit tenant tables are u_*.
#   - information_schema       (ANSI catalog, both engines)
#   - pg_catalog / pg_*        (Postgres system catalogs, e.g. pg_class, pg_user)
#   - sqlite_master / sqlite_* (SQLite system tables)
_SYSTEM_SCHEMA = re.compile(
    r"\b(information_schema|pg_catalog|pg_[a-z_]+|sqlite_master|sqlite_[a-z_]+)\b",
    re.IGNORECASE,
)

# Explicit cross-schema qualifiers signal an attempt to escape the tenant's
# search_path. Legitimate LLM-generated SQL never needs `public.anything` —
# tenant data is always accessed unqualified under the tenant's own schema.
_PUBLIC_QUALIFIER = re.compile(r"\bpublic\s*\.", re.IGNORECASE)

# Sensitive internal tables that live in the public schema. Under a tenant
# search_path these are still reachable via the public fallback, so we block
# them by name as defence-in-depth.
_INTERNAL_TABLES = re.compile(
    r"\b(users|tenants|error_log|uploads|conversations|conversation_messages"
    r"|auth_tokens|_relationships|_column_profile)\b",
    re.IGNORECASE,
)

# `SELECT ... INTO <target>` materializes a new table (Postgres `SELECT INTO`)
# or writes a file (`INTO OUTFILE` / `INTO DUMPFILE`) — a write side-effect, not
# a read. _validate_shape only reaches this check once the statement is already
# confirmed to start with SELECT/WITH, so any `INTO` followed by a table/file
# target here is the dangerous materializing form. Scoped to `INTO` + an
# OUTFILE/DUMPFILE keyword or a (optionally quoted) identifier so it does not
# fire on the legitimate-but-rare absence of such a target.
_SELECT_INTO = re.compile(
    r"\binto\b\s+(?:outfile\b|dumpfile\b|temp\b|temporary\b|unlogged\b|"
    r'"|`|[a-z_])',
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
    sysm = _SYSTEM_SCHEMA.search(stripped)
    if sysm:
        return (
            "References to system/metadata catalogs are not allowed: "
            f"{sysm.group(0)}"
        )
    if _SELECT_INTO.search(stripped):
        return "SELECT ... INTO (table/file materialization) is not allowed"
    if _PUBLIC_QUALIFIER.search(stripped):
        return "Cross-schema qualifiers (public.) are not allowed in tenant queries"
    itm = _INTERNAL_TABLES.search(stripped)
    if itm:
        return f"Access to internal table {itm.group(0)!r} is not allowed"
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
            state_updates={"sql_draft": sql, "sql_validated": sql},
        )


__all__ = ["SqlDryRunTool"]
