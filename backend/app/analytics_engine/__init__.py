"""analytics_engine — public surface (re-export shim).

Phase 2 of the refactor (TD-001) converted the original ~6,000-line
``analytics_engine.py`` into this package. The original module body now
lives in ``_core.py`` so future PRs can extract focused submodules
(``intent_router.py``, ``loop.py``, ``coordinator.py``, …) without
breaking the ~30 external callsites that still do
``from app.analytics_engine import X``.

How this file works
-------------------
``from ._core import *`` makes every public name (no leading underscore)
available as ``app.analytics_engine.<name>``. Private helpers that other
modules actually import by name are listed explicitly below — Python's
``*`` import skips underscore names unless ``__all__`` is defined, and
maintaining ``__all__`` on a 6,000-line file is its own footgun.

If you need a new private symbol from outside this package: add it to the
explicit re-export list below. If you are removing one: grep the repo
first.
"""
from __future__ import annotations

from ._core import *  # noqa: F401, F403

# --- Explicit re-export of private symbols other modules import by name -----
# Keep this list short and intentional. Each entry is grep-confirmed to be
# imported from `app.analytics_engine` somewhere in the repo.
from ._core import (
    _has_any_uploaded_data,       # backend/tests/test_persistence_lifecycle.py
    _narrate_kpi,                 # backend/app/orchestrator_v2/front_door.py
    # Phase 1 helpers — exposed so future code / tests can introspect them
    # without having to know the internal `_core` path.
    _schema_summary_text,
    _dynamic_routing_hints,
    _columns_index_for_dynamic,
    _build_dynamic_chart,
    _normalize_y_label,
    _capability_tool_schemas,
    _tool_result_message,
    _RAW_SQL_BARE_TABLE,
    _SQLITE_NO_COLUMN,
    _RAW_SQL_QUOTED_IDENT,
    _RAW_SQL_BACKTICK_IDENT,
    _RAW_SQL_DANGEROUS,
    _RAW_SQL_SELECT,
    _RAW_SQL_MAX_LIMIT,
)
