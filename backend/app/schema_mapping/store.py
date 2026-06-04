"""Per-tenant schema-mapping persistence — the active LLM/user mapping.

One row per tenant in the first-party ``tenant_schema_maps`` table (lives in
``public``, like ``uploads`` / ``conversations`` / ``users``). The mapping —
the dict that says which ``{table, column}`` is the tenant's revenue, date,
customer, etc. — is stored as JSON text so the SAME schema works on SQLite and
Postgres.

Isolation here is by the ``tenant_id`` PRIMARY KEY, NOT by Postgres
``search_path``: these are first-party public tables, so — exactly like the
``uploads`` registry in :mod:`app.identity` / ``app.infrastructure`` — we do
**not** call :func:`app.tenant_context.set_query_tenant`. (The data-query path
that reads the tenant's ``u_*`` tables is the only place that binds the
search_path; see :mod:`app.schema_mapping.llm_mapper`.)

Portable SQL, mirroring the rest of the backend:

* Every statement uses ``?`` placeholders through
  :func:`app.infrastructure.get_connection`, which transparently translates to
  ``$1, $2, ...`` on Postgres.
* Upsert via ``INSERT ... ON CONFLICT (tenant_id) DO UPDATE`` — works on
  SQLite (>=3.24) and Postgres natively, the same idiom
  ``record_upload_meta`` uses.
* Timestamps are Python-supplied ISO-8601 TEXT strings (computed inside the
  function, never at import) so there is no ``datetime('now')`` vs ``NOW()``
  divergence between engines.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.infrastructure import get_connection

_log = logging.getLogger("agentic_ai.schema_mapping.store")


def _now_iso() -> str:
    """ISO-8601 UTC, second precision — same idiom as the other first-party
    tables. Computed per-call so nothing touches the clock at import time."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def save_tenant_mapping(tenant_id: str, mapping: dict) -> None:
    """Upsert the active schema mapping for ``tenant_id``.

    Serialises ``mapping`` to JSON text and writes one ``tenant_schema_maps``
    row keyed by ``tenant_id``; a second save for the same tenant overwrites
    the previous mapping (ON CONFLICT DO UPDATE). The ``updated_at`` column is
    always refreshed to "now". Raises ``ValueError`` if ``tenant_id`` is empty.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    mapping_json = json.dumps(mapping or {}, separators=(",", ":"))
    updated_at = _now_iso()
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO public.tenant_schema_maps "
            "(tenant_id, mapping_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "  mapping_json = EXCLUDED.mapping_json, "
            "  updated_at   = EXCLUDED.updated_at",
            (tenant_id, mapping_json, updated_at),
        )
        await db.commit()


async def load_tenant_mapping(tenant_id: str) -> dict | None:
    """Return the stored mapping dict for ``tenant_id``, or ``None``.

    ``None`` means "no mapping saved yet". A row whose ``mapping_json`` is
    missing or unparseable is treated as no usable mapping (logged, returns
    ``None``) rather than raising — callers degrade to proposing a fresh one.
    Never raises on an empty ``tenant_id`` (returns ``None``).
    """
    if not tenant_id:
        return None
    async with get_connection() as db:
        cur = await db.execute(
            "SELECT mapping_json FROM public.tenant_schema_maps "
            "WHERE tenant_id = ?",
            (tenant_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
    if not rows:
        return None
    raw = dict(rows[0]).get("mapping_json")
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _log.warning(
            "tenant_schema_maps row for tenant=%r has unparseable mapping_json",
            tenant_id,
        )
        return None
    return loaded if isinstance(loaded, dict) else None


__all__ = ["save_tenant_mapping", "load_tenant_mapping"]
