"""Middleware-order regression tests.

The bug this guards against (found live 2026-08-03): the auth middleware
was registered AFTER CORSMiddleware, which in Starlette means it ran
OUTSIDE it — its early-return 401s carried no Access-Control-Allow-Origin
header, so browsers masked every expired-token 401 as a network error
("TypeError: Failed to fetch") and the frontend blamed the backend
instead of saying "session expired".

Also covers the two companion fixes from the same incident:
  * unhandled-500s render in ServerErrorMiddleware OUTSIDE all user
    middleware, so the Exception handler attaches CORS headers itself;
  * /health now proves the database ANSWERS (a paused Supabase project
    used to report status "ok").

Run:
    python -m pytest tests/test_middleware_cors.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_cors_test.db"
os.environ.setdefault("FINANCIAL_DB_PATH", str(_SANDBOX_DB))

from fastapi.testclient import TestClient  # noqa: E402

from app import db_engine  # noqa: E402
from app.core_system import _allow_all, _raw_origins, app, settings  # noqa: E402
from app.infrastructure import init_database  # noqa: E402

# An origin CORSMiddleware will actually allow in THIS environment —
# prod-style env files pin ALLOWED_ORIGINS to the deployed frontend.
ORIGIN = (
    _raw_origins[0]
    if (_raw_origins and not _allow_all)
    else "http://localhost:5173"
)


def _purge_db_files() -> None:
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"),
              Path(str(_SANDBOX_DB) + "-wal"), Path(str(_SANDBOX_DB) + "-shm")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """Hard-force the SQLite engine for every test and assert it stuck.

    Without this, ``db_engine.database_url()`` falls back to the cached
    pydantic Settings (which loaded DATABASE_URL from backend/.env) and the
    /health test would dial live Supabase — passing or failing with the
    availability of the production database.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "database_url", "", raising=False)
    monkeypatch.setattr(settings, "financial_db_path", str(_SANDBOX_DB),
                        raising=False)

    # Tripwire: prove we are NOT on Postgres before doing any DB work.
    assert db_engine.is_postgres() is False, (
        "test must run on SQLite — is_postgres() is True; DATABASE_URL leaked"
    )

    # Belt-and-suspenders: any accidental Postgres path errors loudly
    # instead of dialing Supabase.
    def _no_postgres(*_a, **_k):
        raise AssertionError("Postgres path reached in a SQLite-only test")

    monkeypatch.setattr(db_engine, "pg_connection", _no_postgres)
    monkeypatch.setattr(db_engine, "get_pool", _no_postgres)

    _purge_db_files()
    asyncio.run(init_database())
    yield
    _purge_db_files()


def _client(**kw) -> TestClient:
    # Deliberately NOT used as a context manager: the lifespan (and its
    # Postgres-required startup guard) must not run — these tests exercise
    # only the middleware stack, the exception handlers, and /health.
    return TestClient(app, **kw)


def test_auth_401_carries_cors_headers(monkeypatch):
    """An expired/missing token must yield a 401 the BROWSER can read.

    That requires Access-Control-Allow-Origin on the short-circuit
    response, i.e. CORSMiddleware must be OUTSIDE the auth middleware.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = _client().get("/dashboard", headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert "access-control-allow-origin" in r.headers, (
        "401 lost its CORS header — auth middleware is outside CORS again"
    )


def test_auth_401_keeps_request_id_and_security_headers(monkeypatch):
    """Instrumentation must sit OUTSIDE auth: 401s keep request ids +
    security headers so users can quote an id when reporting problems."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = _client().get("/dashboard", headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert r.headers.get("x-request-id")
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_preflight_passes_without_token(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = _client().options(
        "/dashboard",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


def test_unhandled_500_carries_cors_headers(monkeypatch):
    """Unhandled exceptions render in ServerErrorMiddleware, OUTSIDE every
    user middleware including CORS — the handler must attach the headers
    itself or browsers mask the 500 as a network error."""
    monkeypatch.setattr(settings, "auth_enabled", False)
    if not any(getattr(r, "path", None) == "/_test_boom" for r in app.routes):
        async def _boom() -> None:
            raise RuntimeError("boom")
        app.add_api_route("/_test_boom", _boom, methods=["GET"])
    r = _client(raise_server_exceptions=False).get(
        "/_test_boom", headers={"Origin": ORIGIN}
    )
    assert r.status_code == 500
    assert "access-control-allow-origin" in r.headers, (
        "500 lost its CORS header — unhandled_exception_handler must mirror it"
    )


def test_health_is_public_and_reports_db_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    # Sandboxed SQLite file — the round-trip must succeed and the overall
    # status must reflect it.
    assert body["database"]["reachable"] is True
    assert body["status"] == "ok"
