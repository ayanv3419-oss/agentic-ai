"""Regression suite for the three advanced subsystems shipped together:

  1. Vector retrieval — char-ngram TF-IDF embeddings + in-memory cosine
     store + hybrid (deterministic-first, semantic-fallback) retrieval.
  2. DuckDB analytical acceleration — monthly_sales_distribution +
     top_entities, parity-checked against the SQLite source of truth.
  3. Sentry monitoring — span context manager works in no-op mode + the
     instrument_tool wrapper records breadcrumbs without a DSN.

Runs as a plain script:

    cd "Agentic Ai/Agentic Ai"
    python backend/tests/test_advanced_subsystems.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Per-process temp DB so we never touch a developer's real financial DB.
_TMP_DB = Path(tempfile.gettempdir()) / "agentic_ai_advanced_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()

os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("AUTH_TOKEN_SECRET", "test-secret-1234567890123456")
os.environ["FINANCIAL_DB_PATH"] = str(_TMP_DB)

import aiosqlite  # noqa: E402

from app.infrastructure import (  # noqa: E402
    SCHEMA_COLUMNS,
    init_database,
    quoted,
    settings,
)


# -------- 1. Vector retrieval --------------------------------------------

def case_embedder_typo_similarity_high() -> tuple[bool, str]:
    """A character-ngram embedder must produce vectors that distinguish
    typo-pairs from random words. We assert (a) typo-pair cosine is well
    above noise floor (>0.25), and (b) typo-pair cosine is meaningfully
    higher than a totally-different-word cosine. The retrieval code uses
    a tighter 0.55 floor for actual matching decisions."""
    from app.vector import CharNgramTfidfEmbedder
    e = CharNgramTfidfEmbedder()
    typo_sim = float(e.embed("Sneakers") @ e.embed("snkrs"))
    diff_sim = float(e.embed("Sneakers") @ e.embed("xylophone"))
    if typo_sim < 0.25:
        return False, f"typo cosine={typo_sim:.3f} below noise floor"
    if typo_sim - diff_sim < 0.15:
        return False, f"typo {typo_sim:.3f} not separated from diff {diff_sim:.3f}"
    return True, f"typo={typo_sim:.3f} diff={diff_sim:.3f}"


def case_vector_store_upsert_and_search() -> tuple[bool, str]:
    """upsert + search returns the right canonical. The store dedupes on
    (kind, id) BY DESIGN — duplicate inserts for the same logical entity
    upsert (replace) the existing vector, which is what we want for
    re-indexing. To carry multiple texts per canonical, callers use unique
    id-suffixes (the strategy `register_vocabulary` uses)."""
    from app.vector import (
        InMemoryVectorStore, VectorRecord, set_vector_store,
    )
    store = InMemoryVectorStore()
    set_vector_store(store)
    store.upsert([
        VectorRecord(id="Sneakers",          text="Sneakers", kind="product"),
        VectorRecord(id="Sneakers#alias=0",  text="sneaker",  kind="product"),
        VectorRecord(id="Boots",             text="Boots",    kind="product"),
        VectorRecord(id="Sandals",           text="Sandals",  kind="product"),
    ])
    if store.size() != 4:
        return False, f"store.size()={store.size()} (expected 4)"
    hits = store.search("snkrs", kind="product", limit=2, min_score=0.1)
    if not hits:
        return False, "no semantic hits for 'snkrs'"
    top = hits[0][0]
    if not top.id.startswith("Sneakers"):
        return False, f"top hit id={top.id!r} (expected one of Sneakers / Sneakers#alias=*)"
    return True, f"top hit id={top.id} score={hits[0][1]:.3f}"


def case_hybrid_retrieve_deterministic_first() -> tuple[bool, str]:
    """Stage-1 (exact) wins over stage-3 (semantic) — the user's deterministic
    intent for an exact-match query never gets overridden by fuzziness."""
    from app.vector import (
        InMemoryVectorStore, hybrid_retrieve, register_vocabulary,
        reset_vocabulary, set_vector_store,
    )
    set_vector_store(InMemoryVectorStore())
    reset_vocabulary()
    register_vocabulary("entity", {
        "Sneakers": ["sneaker", "kicks"],
        "Boots":    ["boot"],
    })
    out = hybrid_retrieve("sneakers", kind="entity")
    if not out:
        return False, "no matches for exact-known query"
    if out[0].source != "exact":
        return False, f"first source={out[0].source} (expected 'exact')"
    if out[0].canonical != "sneakers":
        return False, f"canonical={out[0].canonical!r}"
    return True, f"exact match score=1.0 source=exact"


def case_hybrid_retrieve_semantic_fallback() -> tuple[bool, str]:
    """When stage 1 + 2 produce nothing, semantic fallback fires."""
    from app.vector import (
        InMemoryVectorStore, hybrid_retrieve, register_vocabulary,
        reset_vocabulary, set_vector_store,
    )
    set_vector_store(InMemoryVectorStore())
    reset_vocabulary()
    register_vocabulary("entity", {
        "Sneakers": ["sneaker"],
        "Sandals":  ["sandal"],
    })
    out = hybrid_retrieve("snkrs", kind="entity")
    if not out:
        return False, "no semantic fallback for typo'd query"
    if out[0].source != "semantic":
        return False, f"source={out[0].source} (expected 'semantic')"
    if out[0].canonical not in ("sneakers",):
        return False, f"canonical={out[0].canonical!r} (expected 'sneakers')"
    return True, f"semantic fallback canonical=sneakers score={out[0].score:.3f}"


# -------- 2. DuckDB acceleration ----------------------------------------

async def _seed_for_duckdb() -> None:
    """Wipe + seed the test DB with rows spanning multiple months + products."""
    await init_database()
    async with aiosqlite.connect(str(_TMP_DB)) as db:
        await db.execute(f"DELETE FROM {quoted('sales')}")
        cols = ['batch_id', 'source', 'file_name', *SCHEMA_COLUMNS]
        ph = ",".join("?" for _ in cols)
        qcols = ",".join(quoted(c) for c in cols)
        sql = f"INSERT INTO {quoted('sales')} ({qcols}) VALUES ({ph})"

        def row(d, party, product, amt):
            base = ['b', 'u', 't', d, None, None, party, product, None, None, None,
                    amt, None, None, None, None, None, None, None, None, None, None, None]
            assert len(base) == len(cols), f"{len(base)} vs {len(cols)}"
            return base
        rows = [
            row("2025-01-05", "Acme",  "Sneaker A", 1500.0),
            row("2025-01-15", "Acme",  "Sneaker A",  500.0),
            row("2025-01-28", "Beta",  "Boot B",    1000.0),
            row("2025-02-10", "Beta",  "Boot B",    2500.0),
            row("2025-02-22", "Acme",  "Sandal C",   400.0),
            row("2025-03-05", "Acme",  "Sneaker A", 3000.0),
        ]
        for r in rows:
            await db.execute(sql, r)
        await db.commit()


async def case_duckdb_monthly_pie_matches_sqlite() -> tuple[bool, str]:
    """DuckDB-pied months must equal what SQLite produces. Same shape,
    same totals, same chronological order."""
    from app.analytics_acceleration import (
        get_duckdb_engine, monthly_sales_distribution,
    )
    from app.analytics_engine import DashboardAgent

    await _seed_for_duckdb()
    # Force rebuild — settings.financial_db_path may have changed across
    # cases.
    get_duckdb_engine().reset()

    fast = await monthly_sales_distribution(get_duckdb_engine(), table="sales")
    # Compare via SQLite-fallback path inside DashboardAgent. Force
    # DUCKDB_ENABLED=False for this leg.
    saved = settings.duckdb_enabled
    try:
        settings.duckdb_enabled = False
        slow = await DashboardAgent().run(month=None)
    finally:
        settings.duckdb_enabled = saved
    sqlite_pie = slow.get("monthly_sales_pie") or []
    if fast != sqlite_pie:
        return False, f"DuckDB={fast}\n  SQLite={sqlite_pie}"
    return True, f"DuckDB pie == SQLite pie ({len(fast)} months)"


async def case_duckdb_top_entities_ranks_correctly() -> tuple[bool, str]:
    """`top_entities` must produce the same ranking as the SqlPlanner
    ranking path — products sorted DESC by SUM(Total Amount)."""
    from app.analytics_acceleration import get_duckdb_engine, top_entities
    await _seed_for_duckdb()
    get_duckdb_engine().reset()
    rows = await top_entities(
        get_duckdb_engine(), table="sales",
        group_col="Product Name",
        start_date="2025-01-01",
        end_date="2025-12-31",
        direction="desc",
        limit=10,
    )
    names = [r["name"] for r in rows]
    # Expected from seed:
    #   Sneaker A = 1500 + 500 + 3000 = 5000
    #   Boot B    = 1000 + 2500       = 3500
    #   Sandal C  = 400
    expected_top3 = ["Sneaker A", "Boot B", "Sandal C"]
    if names[:3] != expected_top3:
        return False, f"got {names} (expected first 3 = {expected_top3})"
    return True, f"top_entities ranking correct: {names[:3]}"


async def case_duckdb_bottom_direction_inverts() -> tuple[bool, str]:
    """`direction='asc'` must flip the ORDER BY so the worst seller leads."""
    from app.analytics_acceleration import get_duckdb_engine, top_entities
    await _seed_for_duckdb()
    get_duckdb_engine().reset()
    rows = await top_entities(
        get_duckdb_engine(), table="sales",
        group_col="Product Name",
        direction="asc",
        limit=10,
    )
    names = [r["name"] for r in rows]
    if not names or names[0] != "Sandal C":
        return False, f"got {names} (expected first = 'Sandal C')"
    return True, f"asc direction inverts: first = {names[0]}"


# -------- 3. Sentry monitoring (no-op mode) ------------------------------

def case_span_no_op_works_without_dsn() -> tuple[bool, str]:
    """`span()` must work + return without erroring when no DSN is configured.
    Captures wall-clock elapsed_ms even in no-op mode."""
    import time as _time
    from app.monitoring import span
    started = _time.perf_counter()
    with span("test.op", description="basic", foo="bar") as s:
        # Dummy work
        for _ in range(1000):
            pass
        if s is None:
            return False, "span() yielded None"
    elapsed = _time.perf_counter() - started
    if elapsed > 2.0:
        return False, f"span took {elapsed:.2f}s (broken?)"
    return True, f"no-op span ok ({elapsed*1000:.1f}ms)"


def case_span_propagates_exceptions() -> tuple[bool, str]:
    """An exception inside a span must propagate out — the span tags the
    error type but doesn't swallow it."""
    from app.monitoring import span

    class Boom(Exception): ...
    try:
        with span("test.failing"):
            raise Boom("simulated")
    except Boom:
        return True, "exception propagated correctly"
    except Exception as e:
        return False, f"unexpected exception type: {type(e).__name__}"
    return False, "exception was swallowed"


def case_instrument_tool_records_breadcrumbs() -> tuple[bool, str]:
    """`instrument_tool` runs without erroring + drops breadcrumbs in no-op
    mode (we can't assert delivery, but we CAN assert no exceptions)."""
    from app.monitoring import instrument_tool, record_breadcrumb
    try:
        record_breadcrumb("test", "smoke")
        with instrument_tool("FakeTool", turn_id="t-1"):
            pass
    except Exception as e:
        return False, f"raised {type(e).__name__}: {e}"
    return True, "no-op instrument_tool runs clean"


def case_sentry_init_idempotent() -> tuple[bool, str]:
    """init_sentry can be called more than once without exploding."""
    from app.monitoring import init_sentry
    try:
        init_sentry()
        init_sentry()
    except Exception as e:
        return False, f"raised {type(e).__name__}"
    return True, "double-init clean"


# -------- Runner --------------------------------------------------------

SYNC_CASES = [
    ("embedder produces high cosine for typo pairs",
     case_embedder_typo_similarity_high),
    ("vector store: upsert + search returns top canonical",
     case_vector_store_upsert_and_search),
    ("hybrid retrieve: deterministic exact match wins",
     case_hybrid_retrieve_deterministic_first),
    ("hybrid retrieve: semantic fallback fires on typos",
     case_hybrid_retrieve_semantic_fallback),
    ("monitoring: span() works in no-op mode",
     case_span_no_op_works_without_dsn),
    ("monitoring: span() propagates exceptions",
     case_span_propagates_exceptions),
    ("monitoring: instrument_tool + record_breadcrumb run clean",
     case_instrument_tool_records_breadcrumbs),
    ("monitoring: init_sentry is idempotent",
     case_sentry_init_idempotent),
]

ASYNC_CASES = [
    ("DuckDB monthly_sales_distribution == SQLite path",
     case_duckdb_monthly_pie_matches_sqlite),
    ("DuckDB top_entities ranks DESC correctly",
     case_duckdb_top_entities_ranks_correctly),
    ("DuckDB top_entities direction='asc' inverts ranking",
     case_duckdb_bottom_direction_inverts),
]


def main() -> int:
    print("=== Advanced subsystems (vector / DuckDB / Sentry) ===")
    passed = failed = 0
    for label, fn in SYNC_CASES:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:62} :: {detail}")
        passed += int(ok); failed += int(not ok)

    for label, fn in ASYNC_CASES:
        try:
            ok, detail = asyncio.run(fn())
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        marker = "OK " if ok else "BAD"
        print(f"  [{marker}] {label:62} :: {detail}")
        passed += int(ok); failed += int(not ok)

    total = passed + failed
    print(f"\nTOTAL: {passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        return_code = main()
    finally:
        # Clean up temp DB.
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(_TMP_DB) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
    sys.exit(return_code)
