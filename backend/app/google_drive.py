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
* OAuth credentials are persisted PER-TENANT in the ``drive_tokens`` table
  (public schema, one row per ``tenant_id``) so each self-serve account links
  its OWN Google Drive — no shared global token file. The table self-provisions
  on first save and uses ``get_connection()`` so it works on Postgres AND
  SQLite, exactly like the ``uploads`` / ``conversations`` first-party tables
  (isolated by a ``tenant_id`` column, NOT by search_path).
* The OAuth ``state`` is a self-contained, HMAC-signed token carrying the
  tenant_id (``<b64u(tenant_id)>.<hmac_sha256(secret, that)>``), signed with the
  same ``settings.auth_token_secret`` as the bearer tokens via ``app.auth._sign``.
  This survives process restarts and is multi-user-safe — it replaces the old
  fragile module-global ``_pending_state`` and lets the PUBLIC OAuth callback
  trust which tenant a freshly-minted token belongs to, with no server-side
  session store.
* Blocking Google client calls are wrapped in `asyncio.to_thread` by the
  async helpers; the credential helpers (`load_credentials`, `save_credentials`,
  `is_connected`, `revoke_credentials`) are async because they touch the DB via
  ``get_connection`` — callers ``await`` them directly (no ``to_thread``).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.auth import _b64u_decode, _b64u_encode, _sign
from app.infrastructure import get_connection, settings, uploads_dir

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
# Signed OAuth state — carries the tenant_id through the (public) callback
# ---------------------------------------------------------------------------
# The state is ``<b64u(tenant_id)>.<sig>`` where ``sig = HMAC-SHA256(
# settings.auth_token_secret, b64u(tenant_id))`` — the same primitive and
# secret as the bearer tokens (app.auth / app.identity). It is self-contained
# (no server-side session), so it survives process restarts and is safe with
# many concurrent users, and the PUBLIC callback can verify it came from us and
# recover WHICH tenant the token is for. Unlike the bearer token it carries no
# expiry: the consent round-trip is short-lived and Google bounds it anyway, and
# a leaked state buys an attacker nothing without also controlling our redirect.

def sign_oauth_state(tenant_id: str) -> str:
    """Return a signed, self-contained OAuth ``state`` encoding ``tenant_id``."""
    tid_b64 = _b64u_encode((tenant_id or "").encode("utf-8"))
    sig = _sign(tid_b64, settings.auth_token_secret)
    return f"{tid_b64}.{sig}"


def verify_oauth_state(state: str | None) -> str:
    """Verify a signed OAuth ``state`` and return the embedded ``tenant_id``.

    Raises :class:`ValueError` on a missing, malformed, or tampered state
    (constant-time signature compare) — the caller turns that into a CSRF
    abort. Never trusts the payload before the signature checks out.
    """
    if not state or "." not in state:
        raise ValueError("OAuth state missing or malformed — aborting.")
    try:
        tid_b64, sig = state.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError("OAuth state missing or malformed — aborting.") from exc
    expected = _sign(tid_b64, settings.auth_token_secret)
    # Constant-time compare — a timing side-channel leaks the secret over
    # enough samples (mirrors identity.verify_token).
    if not hmac.compare_digest(expected, sig):
        raise ValueError("OAuth state signature invalid — possible CSRF, aborting.")
    try:
        tenant_id = _b64u_decode(tid_b64).decode("utf-8")
    except Exception as exc:
        raise ValueError("OAuth state payload undecodable — aborting.") from exc
    if not tenant_id:
        raise ValueError("OAuth state carries no tenant — aborting.")
    return tenant_id


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def get_authorization_url(tenant_id: str) -> tuple[str, str]:
    """Build the Google consent-screen URL for ``tenant_id``.

    The ``state`` is a signed token carrying ``tenant_id`` (see
    :func:`sign_oauth_state`), so the public callback can recover and trust the
    tenant without any module-global / session state. Returns ``(url, state)``.
    """
    state = sign_oauth_state(tenant_id)
    flow = _build_flow(state=state)
    url, returned_state = flow.authorization_url(
        access_type="offline",       # get a refresh token
        include_granted_scopes="true",
        prompt="consent",            # force refresh token even on re-auth
        state=state,                 # pin OUR signed state (don't let Flow regenerate)
    )
    return url, returned_state


async def exchange_code(code: str, state: str | None = None) -> str:
    """Exchange the OAuth callback `code` for credentials and persist them.

    Verifies the HMAC signature on ``state`` and extracts the ``tenant_id`` it
    carries (raises :class:`ValueError` on a missing/tampered state — CSRF
    protection). Exchanges ``code`` for credentials and saves them for THAT
    tenant. Returns the resolved ``tenant_id``.
    """
    tenant_id = verify_oauth_state(state)
    flow = _build_flow(state=state)
    # fetch_token is blocking network I/O — run it off the event loop.
    await asyncio.to_thread(flow.fetch_token, code=code)
    creds = flow.credentials
    await save_credentials(tenant_id, creds)
    return tenant_id


# ---------------------------------------------------------------------------
# Credential persistence — per-tenant `drive_tokens` table
# ---------------------------------------------------------------------------
# One row per tenant in the PUBLIC schema, isolated by the tenant_id PRIMARY
# KEY (NOT by search_path) exactly like uploads/conversations/tenant_schema_maps.
# Self-provisions on first save with CREATE TABLE IF NOT EXISTS so no migration
# is required, and goes through get_connection() so the same all-TEXT DDL works
# on SQLite AND Postgres.

_DRIVE_TOKENS_DDL = (
    "CREATE TABLE IF NOT EXISTS drive_tokens (\n"
    "    tenant_id        TEXT PRIMARY KEY,\n"
    "    credentials_json TEXT NOT NULL,\n"
    "    updated_at       TEXT NOT NULL\n"
    ")"
)


def _now_iso() -> str:
    """ISO-8601 UTC, second precision — same idiom as the uploads table."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _ensure_drive_tokens_table(db) -> None:
    await db.execute(_DRIVE_TOKENS_DDL)


async def save_credentials(tenant_id: str, creds) -> None:
    """Upsert ``creds`` (as JSON) for ``tenant_id`` in the drive_tokens table.

    Uses ON CONFLICT(tenant_id) DO UPDATE, which works on SQLite >=3.24 and
    Postgres natively — matching the existing cross-dialect upserts (see
    ``record_upload`` in infrastructure.py).
    """
    creds_json = creds.to_json()
    updated_at = _now_iso()
    async with get_connection() as db:
        await _ensure_drive_tokens_table(db)
        await db.execute(
            """INSERT INTO drive_tokens (tenant_id, credentials_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT (tenant_id) DO UPDATE SET
                 credentials_json = EXCLUDED.credentials_json,
                 updated_at = EXCLUDED.updated_at""",
            (tenant_id, creds_json, updated_at),
        )
        await db.commit()
    log.info("Google Drive credentials saved for tenant=%s", tenant_id)


async def _read_credentials_json(tenant_id: str) -> str | None:
    """Return the stored credentials JSON for ``tenant_id``, or None.

    Tolerates a missing table (no tenant has connected yet) so callers can
    treat "not connected" and "table not created" identically.
    """
    try:
        async with get_connection() as db:
            await _ensure_drive_tokens_table(db)
            cur = await db.execute(
                "SELECT credentials_json FROM drive_tokens WHERE tenant_id = ?",
                (tenant_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
    except Exception:
        log.warning("could not read drive_tokens for tenant=%s", tenant_id, exc_info=True)
        return None
    if not rows:
        return None
    return dict(rows[0]).get("credentials_json")


async def load_credentials(tenant_id: str):
    """Load ``tenant_id``'s persisted credentials, refreshing them if expired.

    Returns the `Credentials` object, or None when there is no row for the
    tenant or it can no longer be refreshed (user must re-connect). On a
    successful refresh the new token is re-saved for the tenant.
    """
    creds_json = await _read_credentials_json(tenant_id)
    if not creds_json:
        return None
    try:
        import json

        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        info = json.loads(creds_json)
        creds = Credentials.from_authorized_user_info(info, DRIVE_SCOPES)
    except Exception:
        log.warning("could not parse Google token for tenant=%s", tenant_id, exc_info=True)
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            await asyncio.to_thread(creds.refresh, Request())
            await save_credentials(tenant_id, creds)
        except Exception:
            log.warning("Google token refresh failed for tenant=%s — re-connect required",
                        tenant_id, exc_info=True)
            return None
    if not creds or not creds.valid:
        return None
    return creds


async def is_connected(tenant_id: str) -> bool:
    """True when ``tenant_id`` has a usable (or refreshable) Drive credential."""
    return await load_credentials(tenant_id) is not None


async def revoke_credentials(tenant_id: str) -> None:
    """Best-effort remote revoke + delete ``tenant_id``'s drive_tokens row."""
    creds = None
    try:
        creds = await load_credentials(tenant_id)
    except Exception:
        pass
    if creds is not None:
        try:
            import requests

            await asyncio.to_thread(
                lambda: requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": creds.token},
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    timeout=5,
                )
            )
        except Exception:
            log.info("remote token revoke failed (continuing with local delete)")
    try:
        async with get_connection() as db:
            await _ensure_drive_tokens_table(db)
            await db.execute(
                "DELETE FROM drive_tokens WHERE tenant_id = ?", (tenant_id,)
            )
            await db.commit()
    except Exception:
        log.warning("could not delete drive_tokens row for tenant=%s", tenant_id, exc_info=True)


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
    from app.agents import DataCleanAgent
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
