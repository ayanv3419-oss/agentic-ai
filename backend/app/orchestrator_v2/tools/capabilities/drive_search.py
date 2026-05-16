"""
Capability: drive_search
========================

Planner-visible wrapper around ``app.google_drive.search_drive``.
Searches the user's Drive (name + content) for ingestible files, so the
Worker can find a file by description ("the March sales sheet") instead
of forcing the user to scroll a flat list.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.orchestrator_v2.state import ExecutionState
from app.orchestrator_v2.tools.base import Capability
from app.orchestrator_v2.tools.registry import register_capability


class DriveSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Free-text search across filename + content.",
    )
    limit: int = Field(25, ge=1, le=100)


class DriveSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    mime_type: str = ""
    modified_time: str | None = None
    size: str | None = None


class DriveSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    count: int = 0
    files: tuple[DriveSearchHit, ...] = ()
    error: str | None = None


@register_capability
class DriveSearch(Capability[DriveSearchArgs, DriveSearchOutput]):
    name = "drive_search"
    description = (
        "Search the user's Google Drive for ingestible files (CSV / XLSX / "
        "Google Sheets) whose name or content matches `query`. Returns up "
        "to `limit` results, newest first. Use to find a file by description "
        "before previewing or ingesting it."
    )
    args_model = DriveSearchArgs
    output_model = DriveSearchOutput
    pure = False

    async def run(
        self,
        state: ExecutionState,
        args: DriveSearchArgs,
    ) -> DriveSearchOutput:
        from app import google_drive

        if not google_drive.is_configured():
            return DriveSearchOutput(
                query=args.query,
                error="Google Drive is not configured on the server.",
            )
        creds = await asyncio.to_thread(google_drive.load_credentials)
        if creds is None:
            return DriveSearchOutput(
                query=args.query,
                error="Google Drive not connected. Connect via the app's Upload page.",
            )
        try:
            files: list[dict[str, Any]] = await google_drive.search_drive(
                creds, args.query, limit=args.limit,
            )
        except Exception as e:
            return DriveSearchOutput(
                query=args.query,
                error=f"{type(e).__name__}: {e}",
            )
        hits = tuple(
            DriveSearchHit(
                id=str(f.get("id") or ""),
                name=str(f.get("name") or ""),
                mime_type=str(f.get("mimeType") or ""),
                modified_time=f.get("modifiedTime"),
                size=str(f["size"]) if f.get("size") is not None else None,
            )
            for f in files
        )
        return DriveSearchOutput(
            query=args.query,
            count=len(hits),
            files=hits,
        )
