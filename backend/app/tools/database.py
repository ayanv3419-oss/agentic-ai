"""Database tool — RESTRICTED.

Wraps the storage layer (`app.database`) as the eighth registered tool. It is
NOT callable from the LLM coordinator path: every call must include the
server-only `pin = INGESTION_PIN` (set by DataCleanAgent) or `pin = READ_PIN`
(set by DashboardAgent). Without one of those pins the tool refuses.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from app.database import (
    ALLOWED_TABLES,
    count_rows,
    fetch_all,
    insert_rows,
)
from app.state import TurnState
from app.tools.base import Tool, ToolResult


INGESTION_PIN = "DCA_INGESTION_ONLY"
READ_PIN = "DA_READ_ONLY"


class DatabaseArgs(BaseModel):
    op: str = Field(description="'insert' or 'select'")
    pin: str = ""
    table: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    source: str = "upload"
    file_name: str | None = None
    sql: str | None = None
    params: list[Any] = Field(default_factory=list)


class DatabaseTool(Tool):
    name = "Database"
    description = (
        "Restricted storage tool. INGESTION_PIN allows insert into sales/purchase; "
        "READ_PIN allows arbitrary SELECT. Not callable from the LLM coordinator."
    )
    args_model = DatabaseArgs
    independent = True

    async def run(self, state: TurnState, args: DatabaseArgs) -> ToolResult:
        op = (args.op or "").lower()
        if op == "insert":
            if args.pin != INGESTION_PIN:
                return ToolResult(ok=False, error="Database.insert requires INGESTION_PIN")
            if args.table not in ALLOWED_TABLES:
                return ToolResult(ok=False, error=f"unknown table {args.table!r}")
            if not args.batch_id:
                return ToolResult(ok=False, error="batch_id required for insert")
            inserted = await asyncio.to_thread(
                insert_rows,
                args.table,
                list(args.rows),
                batch_id=args.batch_id,
                source=args.source or "upload",
                file_name=args.file_name,
            )
            return ToolResult(
                ok=True,
                output={
                    "table": args.table,
                    "rows_inserted": inserted,
                    "table_total": await count_rows(args.table),
                },
            )

        if op == "select":
            if args.pin != READ_PIN:
                return ToolResult(ok=False, error="Database.select requires READ_PIN")
            if not args.sql:
                return ToolResult(ok=False, error="sql required for select")
            rows = await fetch_all(args.sql, tuple(args.params))
            return ToolResult(
                ok=True,
                output={"row_count": len(rows), "rows": rows},
            )

        return ToolResult(ok=False, error=f"unknown op {op!r}")
