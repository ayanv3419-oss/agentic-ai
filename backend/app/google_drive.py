"""Google Drive integration — OAuth + Drive API + ingestion service.

Single home for every Drive-touching operation. Consumed by three thin
adapters that own no Drive logic themselves:
    - the FastAPI routes in core_system.py (`/auth/google/*`, `/drive/*`)
    - the agentic-loop `GoogleDriveTool` in analytics_engine.py
    - the FastMCP server in mcp_server.py

Design notes
------------
* The Google client libraries are imported lazily inside functions so the
  backend still boots when the packages are not installed and the feature
  is simply unused.
* OAuth credentials are persisted as a single JSON file at
  `settings.google_token_path` — right for a single-user local-first app.
* Blocking Google client calls are wrapped in `asyncio.to_thread` by the
  async helpers; the sync helpers (`load_credentials`, `save_credentials`,
  `is_connected`, `revoke_credentials`) are cheap and left for callers to
  thread if they care.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.infrastructure import settings, uploads_dir

# Google sometimes returns a superset of the requested scopes (openid, etc.);
# without this oauthlib raises on the scope mismatch during token exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

log = logging.getLogger("agentic_ai.google_drive")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# MIME types we treat as ingestible tabular data.
_MIME_CSV = "text/csv"
_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
INGESTIBLE_MIME_TYPES = (_MIME_CSV, _MIME_XLSX, _MIME_GSHEET)

# CSRF state from the most recent /auth/google/login, validated on callback.
_pending_state: str | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """True when the OAuth client credentials are present in .env."""
    return bool(settings.google_client_id and settings.google_client_secret)


def _client_config() -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def _build_flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(), scopes=DRIVE_SCOPES, state=state,
    )
    flow.redirect_uri = settings.google_redirect_uri
    return flow


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def get_authorization_url() -> tuple[str, str]:
    """Build the Google consent-screen URL. Returns (url, state)."""
    global _pending_state
    flow = _build_flow()
    url, state = flow.authorization_url(
        access_type="offline",       # get a refresh token
        include_granted_scopes="true",
        prompt="consent",            # force refresh token even on re-auth
    )
    _pending_state = state
    return url, state


def exchange_code(code: str, state: str | None = None):
    """Exchange the OAuth callback `code` for credentials and persist them.

    Raises ValueError when `state` does not match the value handed out by
    the most recent `get_authorization_url()` call (CSRF protection).
    """
    global _pending_state
    if state is not None and _pending_state is not None and state != _pending_state:
        raise ValueError("OAuth state mismatch — possible CSRF, aborting.")
    flow = _build_flow(state=state or _pending_state)
    flow.fetch_token(code=code)
    _pending_state = None
    creds = flow.credentials
    save_credentials(creds)
    return creds


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------

def _token_path() -> Path:
    return Path(settings.google_token_path)


def save_credentials(creds) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(creds.to_json(), encoding="utf-8")
    log.info("Google Drive credentials saved to %s", p)


def load_credentials():
    """Load persisted credentials, refreshing them if expired.

    Returns the `Credentials` object, or None when there is no token file
    or it can no longer be refreshed (user must re-connect).
    """
    p = _token_path()
    if not p.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(p), DRIVE_SCOPES)
    except Exception:
        log.warning("could not parse Google token file %s", p, exc_info=True)
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
        except Exception:
            log.warning("Google token refresh failed — re-connect required", exc_info=True)
            return None
    if not creds or not creds.valid:
        return None
    return creds


def is_connected() -> bool:
    """True when a usable (or refreshable) Drive credential is on disk."""
    return load_credentials() is not None


def revoke_credentials() -> None:
    """Delete the local token file. Best-effort remote revoke too."""
    creds = None
    try:
        creds = load_credentials()
    except Exception:
        pass
    if creds is not None:
        try:
            import requests

            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
        except Exception:
            log.info("remote token revoke failed (continuing with local delete)")
    p = _token_path()
    try:
        if p.exists():
            p.unlink()
    except Exception:
        log.warning("could not delete token file %s", p, exc_info=True)


# ---------------------------------------------------------------------------
# Drive API — listing + download
# ---------------------------------------------------------------------------

def _drive_service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_files_sync(creds, page_size: int) -> list[dict[str, Any]]:
    service = _drive_service(creds)
    mime_q = " or ".join(f"mimeType='{m}'" for m in INGESTIBLE_MIME_TYPES)
    resp = service.files().list(
        q=f"({mime_q}) and trashed=false",
        pageSize=page_size,
        orderBy="modifiedTime desc",
        fields="files(id,name,mimeType,modifiedTime,size)",
    ).execute()
    return resp.get("files", [])


async def list_drive_files(creds, *, page_size: int = 50) -> list[dict[str, Any]]:
    """List the user's ingestible Drive files (CSV / XLSX / Google Sheets)."""
    return await asyncio.to_thread(_list_files_sync, creds, page_size)


def _get_file_metadata_sync(creds, file_id: str) -> dict[str, Any]:
    service = _drive_service(creds)
    return service.files().get(
        fileId=file_id, fields="id,name,mimeType,size",
    ).execute()


def _download_file_sync(creds, file_id: str, mime_type: str, name: str, batch_id: str) -> tuple[Path, str]:
    """Download (or export) one Drive file into data/uploads/{batch_id}.{ext}.

    Returns (local_path, filename). Google Sheets are exported to CSV; native
    CSV/XLSX are downloaded as-is.
    """
    service = _drive_service(creds)
    if mime_type == _MIME_GSHEET:
        data = service.files().export_media(fileId=file_id, mimeType=_MIME_CSV).execute()
        suffix = ".csv"
        filename = name if name.lower().endswith(".csv") else f"{name}.csv"
    elif mime_type == _MIME_XLSX:
        data = service.files().get_media(fileId=file_id).execute()
        suffix = ".xlsx"
        filename = name if name.lower().endswith(".xlsx") else f"{name}.xlsx"
    elif mime_type == _MIME_CSV:
        data = service.files().get_media(fileId=file_id).execute()
        suffix = ".csv"
        filename = name if name.lower().endswith(".csv") else f"{name}.csv"
    else:
        raise ValueError(f"unsupported Drive mime type: {mime_type!r}")

    dest = uploads_dir() / f"{batch_id}{suffix}"
    dest.write_bytes(data if isinstance(data, bytes) else bytes(data))
    return dest, filename


# ---------------------------------------------------------------------------
# Ingestion — reuses the existing DataCleanAgent pipeline
# ---------------------------------------------------------------------------

async def ingest_drive_files(
    creds,
    file_ids: list[str],
    target: str,
    dedup_mode: str = "skip",
) -> list[dict[str, Any]]:
    """Download the given Drive files and run each through DataCleanAgent.

    Mirrors the POST /upload flow per file (parse → validate → dedup →
    insert, tagged source='drive'), then runs the shared post-ingest
    refresh once if anything landed. Returns one result dict per file.
    """
    # Local imports avoid an import cycle: core_system imports this module.
    from app.analytics_engine import DataCleanAgent
    from app.core_system import _post_ingest_refresh
    from app.infrastructure import UploadError, count_rows, get_connection
    from app.dedup import compute_file_hash

    results: list[dict[str, Any]] = []
    any_inserted = False

    for file_id in file_ids:
        batch_id = str(uuid4())
        local_path: Path | None = None
        try:
            meta = await asyncio.to_thread(_get_file_metadata_sync, creds, file_id)
            mime_type = meta.get("mimeType", "")
            name = meta.get("name", file_id)
            local_path, filename = await asyncio.to_thread(
                _download_file_sync, creds, file_id, mime_type, name, batch_id,
            )

            agent = DataCleanAgent()
            result = await agent.run(
                tmp_path=local_path,
                filename=filename,
                target=target,
                batch_id=batch_id,
                dedup_mode=dedup_mode,
                source="drive",
            )

            # Stamp the persisted path + file hash onto the uploads row so
            # disconnect_upload + the file-level duplicate detector work.
            try:
                file_hash, file_bytes = compute_file_hash(local_path)
                async with get_connection() as db:
                    await db.execute(
                        "UPDATE uploads SET file_path = ?, file_hash = ?, "
                        "file_bytes = ? WHERE batch_id = ?",
                        (str(local_path), file_hash, file_bytes, batch_id),
                    )
                    await db.commit()
            except Exception:
                log.warning("could not stamp file_path for drive batch %s", batch_id, exc_info=True)

            rows_inserted = int(result.get("rows_inserted") or 0)
            any_inserted = any_inserted or rows_inserted > 0
            results.append({
                "ok": True,
                "file_id": file_id,
                "batch_id": result["batch_id"],
                "filename": result["filename"],
                "target": result["target"],
                "rows_inserted": rows_inserted,
                "rows_failed": result.get("rows_failed", 0),
                "rows_skipped_duplicate": result.get("rows_skipped_duplicate", 0),
                "rows_replaced": result.get("rows_replaced", 0),
            })
        except UploadError as e:
            _cleanup(local_path)
            results.append({"ok": False, "file_id": file_id, "error": str(e)})
        except Exception as e:
            _cleanup(local_path)
            log.exception("drive ingest failed for file %s", file_id)
            results.append({
                "ok": False, "file_id": file_id,
                "error": f"{type(e).__name__}: {e}",
            })

    if any_inserted:
        try:
            await _post_ingest_refresh()
        except Exception:
            log.warning("post-ingest refresh failed after drive sync", exc_info=True)

    # Attach the final table total to every result, mirroring /upload.
    try:
        total = await count_rows(target)
        for r in results:
            r["table_total"] = total
    except Exception:
        pass

    return results


def _cleanup(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.warning("could not clean up partial drive file %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# Phase B — Granular drive tools: preview, infer_schema, search
# ---------------------------------------------------------------------------
# These let an MCP client (Claude Desktop, Cursor) or the v2 Worker
# planner peek at a Drive file BEFORE deciding whether to ingest it.
# All three are read-only (drive.readonly scope) and reuse the existing
# OAuth credential storage.

def _bytes_for_inspection_sync(creds, file_id: str) -> tuple[str, str, bytes]:
    """Fetch a Drive file's bytes for inspection (preview/schema-infer).
    Returns ``(name, mime_type, raw_bytes)``. Google Sheets export to CSV
    so the downstream parser can read them with the CSV path. Never
    writes to disk — inspection never lands in data/uploads/.
    """
    service = _drive_service(creds)
    meta = service.files().get(
        fileId=file_id, fields="id,name,mimeType,size",
    ).execute()
    mime_type = meta.get("mimeType", "")
    name = meta.get("name", file_id)
    if mime_type == _MIME_GSHEET:
        data = service.files().export_media(fileId=file_id, mimeType=_MIME_CSV).execute()
        if not name.lower().endswith(".csv"):
            name = f"{name}.csv"
    elif mime_type in (_MIME_CSV, _MIME_XLSX):
        data = service.files().get_media(fileId=file_id).execute()
    else:
        raise ValueError(f"unsupported Drive mime type: {mime_type!r}")
    raw = data if isinstance(data, bytes) else bytes(data)
    return name, mime_type, raw


async def preview_file(
    creds,
    file_id: str,
    *,
    rows: int = 20,
) -> dict[str, Any]:
    """Preview the first N rows of a Drive file without ingesting it.

    Returns ``{name, mime_type, columns, rows, total_rows_in_file}``.
    Useful for the AI Assistant + external MCP clients to confirm a
    file is the one the user means before committing to an ingest.
    """
    from app.infrastructure import parse_file

    rows = max(1, min(int(rows or 20), 200))   # clamp 1..200
    name, mime_type, raw = await asyncio.to_thread(
        _bytes_for_inspection_sync, creds, file_id,
    )
    # parse_file returns (canonical_columns, alias_map, rows[]) — the
    # alias map describes the header normalization the importer would
    # apply. For previewing we only surface columns + rows.
    canonical_columns, _alias_map, all_rows = await asyncio.to_thread(
        parse_file, name, raw,
    )
    return {
        "file_id":             file_id,
        "name":                name,
        "mime_type":           mime_type,
        "columns":             list(canonical_columns),
        "rows":                all_rows[:rows],
        "total_rows_in_file":  len(all_rows),
        "preview_row_count":   min(rows, len(all_rows)),
    }


def _infer_column_type(values: list[Any]) -> str:
    """Coarse type inference from a sample of column values.
    Returns one of: 'date', 'number', 'integer', 'boolean', 'string', 'empty'.
    """
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return "empty"
    # Boolean check first — strict literal match.
    bool_like = {"true", "false", "yes", "no", "0", "1"}
    if all(str(v).strip().lower() in bool_like for v in non_null):
        return "boolean"
    # Date check — ISO YYYY-MM-DD is the canonical form used elsewhere.
    import re
    if all(re.match(r"^\d{4}-\d{2}-\d{2}", str(v).strip()) for v in non_null):
        return "date"
    # Numeric check.
    all_int = True
    all_num = True
    for v in non_null:
        s = str(v).strip().replace(",", "")
        try:
            f = float(s)
        except (ValueError, TypeError):
            all_num = False
            all_int = False
            break
        if f != int(f):
            all_int = False
    if all_int:
        return "integer"
    if all_num:
        return "number"
    return "string"


async def infer_schema(
    creds,
    file_id: str,
    *,
    sample_rows: int = 50,
) -> dict[str, Any]:
    """Infer column names + types + sample values from a Drive file.

    Lighter than preview_file when the caller only cares about schema
    (e.g., "is this ingestible?" / "which column is the amount?").
    """
    from app.infrastructure import parse_file

    sample_rows = max(5, min(int(sample_rows or 50), 200))
    name, mime_type, raw = await asyncio.to_thread(
        _bytes_for_inspection_sync, creds, file_id,
    )
    canonical_columns, _alias_map, all_rows = await asyncio.to_thread(
        parse_file, name, raw,
    )
    sample = all_rows[:sample_rows]
    columns_meta: list[dict[str, Any]] = []
    for col in canonical_columns:
        values = [r.get(col) for r in sample]
        non_null_count = sum(1 for v in values if v not in (None, ""))
        columns_meta.append({
            "name":            col,
            "inferred_type":   _infer_column_type(values),
            "non_null_count":  non_null_count,
            "sample_values":   [v for v in values if v not in (None, "")][:5],
        })
    return {
        "file_id":            file_id,
        "name":               name,
        "mime_type":          mime_type,
        "columns":            columns_meta,
        "sample_size":        len(sample),
        "total_rows_in_file": len(all_rows),
    }


def _search_drive_sync(creds, query: str, limit: int) -> list[dict[str, Any]]:
    """Drive `q=` search across the user's files. Restricted to
    ingestible MIME types so results match what ingest_drive_files can
    consume."""
    service = _drive_service(creds)
    mime_q = " or ".join(f"mimeType='{m}'" for m in INGESTIBLE_MIME_TYPES)
    # Escape single quotes in user query (Drive q-syntax uses '...').
    safe = (query or "").replace("'", "\\'")
    full_q = f"({mime_q}) and trashed=false and (name contains '{safe}' or fullText contains '{safe}')"
    resp = service.files().list(
        q=full_q,
        pageSize=max(1, min(int(limit), 100)),
        orderBy="modifiedTime desc",
        fields="files(id,name,mimeType,modifiedTime,size)",
    ).execute()
    return resp.get("files", [])


async def search_drive(
    creds,
    query: str,
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search the user's Drive by name + content. Returns up to `limit`
    ingestible files (CSV / XLSX / Google Sheets), newest first."""
    if not query or not query.strip():
        return []
    return await asyncio.to_thread(_search_drive_sync, creds, query.strip(), limit)
