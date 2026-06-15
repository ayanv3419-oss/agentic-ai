"""Regression: data-version sidecar path + bump resilience.

Guards the production bug where ``POST /upload`` returned HTTP 500
(``PermissionError: [Errno 13] Permission denied: '/data'``) even though the
ingest succeeded and the rows landed. Root cause: ``bump_data_version()`` wrote
the ``.version`` sidecar next to ``FINANCIAL_DB_PATH`` — which Render points at
``/data``, a path with no writable disk on the free plan — so the post-ingest
version bump crashed the request.

The fix is twofold:
  1. ``_data_version_path()`` routes the sidecar to ``/tmp`` when ``DATABASE_URL``
     is set (mirroring ``uploads_dir()`` / ``_cache_path()``).
  2. ``bump_data_version()`` tolerates an unwritable path (logs + returns)
     instead of propagating, so a successful ingest can never become a 500.

LOCAL ONLY — forces SQLite like the rest of the suite (never dials Supabase).

Run:  cd backend && python -m pytest tests/test_data_version_path.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Force SQLite before importing app modules (no DATABASE_URL -> is_postgres False).
os.environ.pop("DATABASE_URL", None)

from app import infrastructure  # noqa: E402


def test_local_path_sits_beside_financial_db(monkeypatch):
    """SQLite / local: unchanged — sidecar lives next to the DB file."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    p = infrastructure._data_version_path()
    assert p == Path(infrastructure.settings.financial_db_path).with_suffix(".version")


def test_postgres_path_routes_to_tmp_not_data(monkeypatch):
    """The core fix: with DATABASE_URL set, the version file is NOT derived
    from FINANCIAL_DB_PATH (=/data on Render) — it goes to /tmp."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
    monkeypatch.setattr(
        infrastructure.settings, "financial_db_path",
        "/data/financial_records.db", raising=False,
    )
    p = infrastructure._data_version_path()
    norm = str(p).replace("\\", "/")
    assert norm.endswith("/tmp/agentic_data.version"), norm
    # The /data parent must play no part in the resolved path.
    assert "/data/" not in norm


def test_bump_round_trips(monkeypatch, tmp_path):
    """bump increments + persists; get reads it back."""
    target = tmp_path / "agentic_data.version"
    monkeypatch.setattr(infrastructure, "_data_version_path", lambda: target)
    assert infrastructure.get_data_version() == 0
    v1 = infrastructure.bump_data_version()
    v2 = infrastructure.bump_data_version()
    assert (v1, v2) == (1, 2)
    assert target.read_text(encoding="utf-8").strip() == "2"
    assert infrastructure.get_data_version() == 2


def test_bump_never_500s_on_unwritable_path(monkeypatch):
    """Reproduce the prod failure shape: the write path raises
    PermissionError('/data'). bump must degrade (log + return an int), NEVER
    propagate — otherwise a successful ingest becomes a user-facing HTTP 500."""
    monkeypatch.setattr(
        infrastructure, "_data_version_path",
        lambda: Path("/data/financial_records.version"),
    )

    def _boom(*_a, **_k):
        raise PermissionError(13, "Permission denied", "/data")

    monkeypatch.setattr(Path, "mkdir", _boom)
    v = infrastructure.bump_data_version()  # must not raise
    assert isinstance(v, int)
