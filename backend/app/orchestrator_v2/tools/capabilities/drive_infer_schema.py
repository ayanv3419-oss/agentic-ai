"""
Capability: drive_infer_schema
==============================

Planner-visible wrapper around ``app.google_drive.infer_schema``.
Returns per-column metadata (name + inferred type + non-null count +
sample values) so the v2 Worker can decide "is this ingestible?" or
"which column is the amount?" without downloading the whole file.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


InferredType = Literal["date", "number", "integer", "boolean", "string", "empty"]


class DriveInferSchemaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(..., min_length=1, max_length=200)
    sample_rows: int = Field(50, ge=5, le=200)


class DriveColumnMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    inferred_type: InferredType
    non_null_count: int
    sample_values: tuple[Any, ...] = ()


class DriveInferSchemaOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: str
    name: str = ""
    mime_type: str = ""
    columns: tuple[DriveColumnMeta, ...] = ()
    sample_size: int = 0
    total_rows_in_file: int = 0
    error: str | None = None


@register_capability
class DriveInferSchema(Capability[DriveInferSchemaArgs, DriveInferSchemaOutput]):
    name = "drive_infer_schema"
    description = (
        "Infer columns + types + sample values from a Google Drive file "
        "(CSV / XLSX / Google Sheets). Use to verify a file is ingestible "
        "and to identify which columns map to which business metric."
    )
    args_model = DriveInferSchemaArgs
    output_model = DriveInferSchemaOutput
    pure = False

    async def run(
        self,
        state: ExecutionState,
        args: DriveInferSchemaArgs,
    ) -> DriveInferSchemaOutput:
        from app import google_drive

        if not google_drive.is_configured():
            return DriveInferSchemaOutput(
                file_id=args.file_id,
                error="Google Drive is not configured on the server.",
            )
        creds = await asyncio.to_thread(google_drive.load_credentials)
        if creds is None:
            return DriveInferSchemaOutput(
                file_id=args.file_id,
                error="Google Drive not connected. Connect via the app's Upload page.",
            )
        try:
            result = await google_drive.infer_schema(
                creds, args.file_id, sample_rows=args.sample_rows,
            )
        except Exception as e:
            return DriveInferSchemaOutput(
                file_id=args.file_id,
                error=f"{type(e).__name__}: {e}",
            )
        cols = tuple(
            DriveColumnMeta(
                name=str(c.get("name") or ""),
                inferred_type=c.get("inferred_type") or "string",
                non_null_count=int(c.get("non_null_count") or 0),
                sample_values=tuple(c.get("sample_values") or ()),
            )
            for c in (result.get("columns") or [])
        )
        return DriveInferSchemaOutput(
            file_id=result.get("file_id", args.file_id),
            name=result.get("name", ""),
            mime_type=result.get("mime_type", ""),
            columns=cols,
            sample_size=int(result.get("sample_size") or 0),
            total_rows_in_file=int(result.get("total_rows_in_file") or 0),
        )
