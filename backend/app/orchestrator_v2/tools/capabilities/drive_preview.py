"""
Capability: drive_preview
=========================

Planner-visible wrapper around ``app.google_drive.preview_file``. Lets
the v2 Worker peek at a Drive file's columns + first N rows before
deciding whether to call ``ingest_drive_files``.

Returns the same payload the MCP tool of the same name returns, but as
a strict-typed Pydantic model so the Worker's plan/narrate steps can
consume it deterministically.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


class DrivePreviewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Google Drive file id (from list_drive_files or search_drive).",
    )
    rows: int = Field(20, ge=1, le=200)


class DrivePreviewOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: str
    name: str = ""
    mime_type: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    total_rows_in_file: int = 0
    preview_row_count: int = 0
    error: str | None = None


@register_capability
class DrivePreview(Capability[DrivePreviewArgs, DrivePreviewOutput]):
    name = "drive_preview"
    description = (
        "Peek at the first N rows of a Google Drive file (CSV / XLSX / "
        "Google Sheets) WITHOUT ingesting it. Useful for confirming the "
        "right file + checking column layout before calling ingest_drive_files."
    )
    args_model = DrivePreviewArgs
    output_model = DrivePreviewOutput
    pure = False  # touches Google Drive

    async def run(
        self,
        state: ExecutionState,
        args: DrivePreviewArgs,
    ) -> DrivePreviewOutput:
        from app import google_drive

        if not google_drive.is_configured():
            return DrivePreviewOutput(
                file_id=args.file_id,
                error="Google Drive is not configured on the server.",
            )
        creds = await asyncio.to_thread(google_drive.load_credentials)
        if creds is None:
            return DrivePreviewOutput(
                file_id=args.file_id,
                error="Google Drive not connected. Connect via the app's Upload page.",
            )
        try:
            result = await google_drive.preview_file(creds, args.file_id, rows=args.rows)
        except Exception as e:
            return DrivePreviewOutput(
                file_id=args.file_id,
                error=f"{type(e).__name__}: {e}",
            )
        return DrivePreviewOutput(
            file_id=result.get("file_id", args.file_id),
            name=result.get("name", ""),
            mime_type=result.get("mime_type", ""),
            columns=tuple(result.get("columns") or ()),
            rows=tuple(result.get("rows") or ()),
            total_rows_in_file=int(result.get("total_rows_in_file") or 0),
            preview_row_count=int(result.get("preview_row_count") or 0),
        )
