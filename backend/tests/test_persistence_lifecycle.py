"""Persistent-dataset lifecycle E2E.

Covers the full lifecycle the spec describes:
  upload  → AI uses data
  restart → AI still uses data
  archive → AI stops using data, file preserved on disk
  unarchive → AI uses data again
  disconnect → rows + file gone, audit row remains

Run:
    python backend/tests/test_persistence_lifecycle.py
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_persist_lifecycle_test.db"
_SANDBOX_CACHE = _BACKEND_DIR / "_persist_lifecycle_cache.json"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)
os.environ["RESPONSE_STORE_PATH"] = str(_SANDBOX_CACHE)

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:55s} got={got!r:30s} want={want!r}")


def assert_true(label, condition):
    global _passed, _failed
    ok = bool(condition)
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")


async def main():
    # Clean slate
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import (
        init_database, insert_rows, record_upload_meta,
        count_rows, list_uploads_meta, get_upload_meta,
        archive_upload, unarchive_upload, disconnect_upload,
        bump_data_version, uploads_dir,
    )
    from app.kpi import init_kpi_table, rebuild_catalog, calculate_by_name
    from app.time_engine import invalidate_cache as invalidate_time_cache
    from app.analytics_engine import _has_any_uploaded_data

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()

    # ---------- 1. Upload + persist ----------
    print("[1] Upload — rows + audit row + source file all persist")
    udir = uploads_dir()
    src = udir / "b1.csv"
    src.write_text("Date,Total Amount\n2026-05-19,1000\n", encoding="utf-8")
    insert_rows("sales", [
        {"Date": "2026-05-19", "Total Amount": 1000.0, "Party Name": "X",
         "Order No": "O1", "Product Name": "Nike Shoes"},
        {"Date": "2026-05-18", "Total Amount": 500.0,  "Party Name": "Y",
         "Order No": "O2", "Product Name": "Bata Slippers"},
    ], batch_id="b1")
    await record_upload_meta(
        batch_id="b1", filename="b1.csv", target="sales",
        rows_inserted=2, rows_failed=0, source="upload", status="active",
        file_path=str(src),
    )
    bump_data_version(); invalidate_time_cache()
    check("sales rows after upload",        await count_rows("sales"),  2)
    check("uploads rows after upload",      len(await list_uploads_meta()), 1)
    check("source file exists",             src.exists(),               True)
    check("AI sees data (has_any_uploaded)", await _has_any_uploaded_data(), True)

    # ---------- 2. Cold-start restart simulation ----------
    print()
    print("[2] Simulated restart — modules reloaded, same DB on disk")
    for mod_name in list(sys.modules):
        if mod_name.startswith("app."):
            del sys.modules[mod_name]
    from app.infrastructure import (  # noqa: E402
        init_database, count_rows, list_uploads_meta,
    )
    from app.analytics_engine import _has_any_uploaded_data as _has_data_2  # noqa: E402
    from app.time_engine import invalidate_cache as inv_2  # noqa: E402

    await init_database()
    inv_2()
    check("sales rows after restart",  await count_rows("sales"), 2)
    check("uploads rows after restart", len(await list_uploads_meta()), 1)
    check("AI sees data after restart", await _has_data_2(), True)

    # ---------- 3. Archive — AI stops using the data ----------
    print()
    print("[3] Archive — rows leave live table, file stays on disk")
    from app.infrastructure import (  # noqa: E402
        archive_upload as _arch,
        unarchive_upload as _unarch,
        disconnect_upload as _disc,
        get_upload_meta as _meta,
        bump_data_version as _bump,
    )
    from app.time_engine import invalidate_cache as inv_3  # noqa: E402

    arch_result = await _arch("b1")
    _bump(); inv_3()
    check("rows moved to archive",   arch_result["rows_moved"], 2)
    check("status now archived",     arch_result["status"], "archived")
    check("sales live count = 0",    await count_rows("sales"), 0)
    check("sales_archive count = 2", await count_rows("sales_archive"), 2)
    check("source file preserved",   src.exists(), True)
    meta = await _meta("b1")
    check("upload meta status",      meta["status"], "archived")
    from app.analytics_engine import _has_any_uploaded_data as _has_data_3  # noqa: E402
    check("AI no longer sees data",  await _has_data_3(), False)

    # Verify KPI engine returns zero for live-only metrics
    r = await calculate_by_name("total_revenue")
    check("total_revenue after archive", r.value, 0)

    # ---------- 4. Unarchive — restore live data ----------
    print()
    print("[4] Unarchive — rows return to live, AI uses them again")
    unarch_result = await _unarch("b1")
    _bump(); inv_3()
    check("rows moved back",         unarch_result["rows_moved"], 2)
    check("status now active",       unarch_result["status"], "active")
    check("sales live count = 2",    await count_rows("sales"), 2)
    check("sales_archive count = 0", await count_rows("sales_archive"), 0)
    from app.analytics_engine import _has_any_uploaded_data as _has_data_4  # noqa: E402
    check("AI sees data again",      await _has_data_4(), True)

    r = await calculate_by_name("total_revenue")
    check("total_revenue after unarchive", r.value, 1500.0)

    # ---------- 5. Archive again, then disconnect ----------
    print()
    print("[5] Archive then disconnect — removal cleans archive table too")
    await _arch("b1")
    check("archived first", (await _meta("b1"))["status"], "archived")
    disc = await _disc("b1")
    check("rows_removed",                 disc["rows_removed"], 2)
    check("file_removed",                 disc["file_removed"], True)
    check("status removed",               disc["status"], "removed")
    check("sales live = 0 after disconnect",    await count_rows("sales"), 0)
    check("sales_archive = 0 after disconnect", await count_rows("sales_archive"), 0)
    check("source file gone after disconnect",  src.exists(), False)
    check("upload audit row kept",        (await _meta("b1"))["status"], "removed")

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CACHE):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
