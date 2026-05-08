"""Agentic AI — entry shim.

Run from the project root:
    python backend/main.py
or:
    uvicorn backend.app.core_system:app --port 8000 --reload

Architecture (3 modules under app/):
    infrastructure.py     — settings, schema, SQLite + JSON cache + memory
    analytics_engine.py   — TurnState + 14 tools + registry + Groq + SSE +
                            cost guard, coordinator + 7 sub-agents
    core_system.py        — FastAPI app, every HTTP route, auth, middleware,
                            exception handlers, startup hook
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `import app.*` when launched as `python backend/main.py` from the
# project root: put backend/ on sys.path so `app` resolves to backend/app/.
_BACKEND_DIR = str(Path(__file__).resolve().parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import uvicorn

from app.core_system import app  # noqa: F401  — uvicorn imports the symbol below
from app.infrastructure import settings


if __name__ == "__main__":
    uvicorn.run(
        "app.core_system:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
