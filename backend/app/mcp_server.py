"""FastMCP server — exposes Google Drive ingestion over the MCP protocol.

This is the *external* surface: an MCP client (Claude Desktop, the MCP
inspector, another agent) connects to `/mcp` on the running backend and can
list / ingest the user's Google Drive data files. It shares all logic with
the in-process `GoogleDriveTool` and the `/drive/*` HTTP routes via the
`app.google_drive` service module — no duplicated Drive logic.

OAuth itself is NOT done over MCP: the user connects Google Drive once
through the app's Upload page (browser OAuth flow), which persists a token
file. These MCP tools just consume that token.

`mcp_app()` returns the ASGI app mounted at `/mcp` by core_system.py. Import
of `fastmcp` is lazy + guarded there so the backend still boots if the
package is not installed.
"""
from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP

from app import google_drive
from app.infrastructure import ALLOWED_TABLES

log = logging.getLogger("agentic_ai.mcp")

mcp: FastMCP = FastMCP("MetricAi Google Drive")


@mcp.tool
async def list_drive_files() -> dict:
    """List the user's ingestible Google Drive files (CSV, XLSX, Google Sheets).

    Returns each file's id, name, mimeType, modifiedTime and size. Use the
    ids with `ingest_drive_files`.
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    creds = await asyncio.to_thread(google_drive.load_credentials)
    if creds is None:
        return {
            "ok": False,
            "error": "Google Drive not connected. Connect it once via the "
                     "app's Upload page, then retry.",
        }
    try:
        files = await google_drive.list_drive_files(creds)
    except Exception as e:
        log.exception("mcp list_drive_files failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "files": files}


@mcp.tool
async def ingest_drive_files(
    file_ids: list[str],
    target: str = "sales",
    dedup_mode: str = "skip",
) -> dict:
    """Download the given Google Drive files and load them into the database.

    Parameters
    ----------
    file_ids : list[str]
        Drive file ids (from `list_drive_files`).
    target : str
        Destination table — 'sales' or 'purchase'.
    dedup_mode : str
        Duplicate-row policy — 'skip' | 'replace' | 'append' | 'block'.

    Returns one result per file plus the total rows inserted. After a
    successful ingest the data is live for the app's analytics.
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    target = (target or "sales").strip().lower()
    if target not in ALLOWED_TABLES:
        return {"ok": False, "error": f"target must be one of {list(ALLOWED_TABLES)}"}
    if not file_ids:
        return {"ok": False, "error": "file_ids must contain at least one Drive file id."}

    creds = await asyncio.to_thread(google_drive.load_credentials)
    if creds is None:
        return {
            "ok": False,
            "error": "Google Drive not connected. Connect it once via the "
                     "app's Upload page, then retry.",
        }
    try:
        results = await google_drive.ingest_drive_files(
            creds, file_ids, target, dedup_mode or "skip",
        )
    except Exception as e:
        log.exception("mcp ingest_drive_files failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "target": target,
        "results": results,
        "rows_inserted_total": sum(r.get("rows_inserted", 0) for r in results),
    }


def mcp_app():
    """ASGI app for mounting at /mcp (streamable HTTP transport).

    stateless_http=True: each request is self-contained, so the app can be
    mounted as a sub-app without a shared session needing the parent lifespan.
    path="/": serve at the mount root so the endpoint is exactly /mcp (the
    FastMCP default of /mcp/ would otherwise nest to /mcp/mcp/).
    """
    return mcp.http_app(path="/", stateless_http=True)
