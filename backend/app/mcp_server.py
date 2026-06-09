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

import logging

from fastmcp import FastMCP

from app import google_drive
from app.infrastructure import ALLOWED_TABLES
from app.tenant_context import DEFAULT_TENANT_ID

log = logging.getLogger("agentic_ai.mcp")

# The MCP surface has no per-request tenant identity — it's an operator /
# single-user integration where the user connected Drive once via the app's
# Upload page. Bind it to the default ("public") tenant so its Drive tools keep
# resolving that single connection now that credentials are tenant-keyed.
_MCP_TENANT_ID = DEFAULT_TENANT_ID

mcp: FastMCP = FastMCP("MetricAi Google Drive")


@mcp.tool
async def list_drive_files() -> dict:
    """List the user's ingestible Google Drive files (CSV, XLSX, Google Sheets).

    Returns each file's id, name, mimeType, modifiedTime and size. Use the
    ids with `ingest_drive_files`.
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    creds = await google_drive.load_credentials(_MCP_TENANT_ID)
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

    creds = await google_drive.load_credentials(_MCP_TENANT_ID)
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


@mcp.tool
async def preview_file(file_id: str, rows: int = 20) -> dict:
    """Preview the first N rows of a Drive file without ingesting it.

    Use this to check that a file is the one you mean BEFORE calling
    ``ingest_drive_files`` — peeks at column names + sample values
    without changing the database.

    Parameters
    ----------
    file_id : str
        Drive file id (from ``list_drive_files`` or ``search_drive``).
    rows : int
        How many rows to return (1..200, default 20).

    Returns ``{ok, file_id, name, mime_type, columns, rows, total_rows_in_file}``.
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    creds = await google_drive.load_credentials(_MCP_TENANT_ID)
    if creds is None:
        return {
            "ok": False,
            "error": "Google Drive not connected. Connect it once via the "
                     "app's Upload page, then retry.",
        }
    try:
        result = await google_drive.preview_file(creds, file_id, rows=rows)
    except Exception as e:
        log.exception("mcp preview_file failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, **result}


@mcp.tool
async def infer_schema(file_id: str, sample_rows: int = 50) -> dict:
    """Infer column names + types + sample values from a Drive file.

    Lighter than ``preview_file`` when you only care about "is this
    ingestible?" or "what does the schema look like?". Returns per-column
    metadata (name, inferred type, non-null count, sample values).

    Parameters
    ----------
    file_id : str
        Drive file id.
    sample_rows : int
        Rows to sample for type inference (5..200, default 50).
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    creds = await google_drive.load_credentials(_MCP_TENANT_ID)
    if creds is None:
        return {
            "ok": False,
            "error": "Google Drive not connected. Connect it once via the "
                     "app's Upload page, then retry.",
        }
    try:
        result = await google_drive.infer_schema(creds, file_id, sample_rows=sample_rows)
    except Exception as e:
        log.exception("mcp infer_schema failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, **result}


@mcp.tool
async def search_drive(query: str, limit: int = 25) -> dict:
    """Search the user's Drive for ingestible files matching ``query``.

    Searches both filenames and file content. Results are restricted to
    CSV / XLSX / Google Sheets (the MIME types ``ingest_drive_files``
    can consume), newest first.

    Parameters
    ----------
    query : str
        Free-text search query, e.g. "revenue 2025" or "march sales".
    limit : int
        Max results to return (1..100, default 25).
    """
    if not google_drive.is_configured():
        return {"ok": False, "error": "Google Drive is not configured on the server."}
    if not query or not query.strip():
        return {"ok": False, "error": "query must be a non-empty string"}
    creds = await google_drive.load_credentials(_MCP_TENANT_ID)
    if creds is None:
        return {
            "ok": False,
            "error": "Google Drive not connected. Connect it once via the "
                     "app's Upload page, then retry.",
        }
    try:
        files = await google_drive.search_drive(creds, query, limit=limit)
    except Exception as e:
        log.exception("mcp search_drive failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "query": query, "count": len(files), "files": files}


def mcp_app():
    """ASGI app for mounting at /mcp (streamable HTTP transport).

    stateless_http=True: each request is self-contained, so the app can be
    mounted as a sub-app without a shared session needing the parent lifespan.
    path="/": serve at the mount root so the endpoint is exactly /mcp (the
    FastMCP default of /mcp/ would otherwise nest to /mcp/mcp/).
    """
    return mcp.http_app(path="/", stateless_http=True)
