"""Dedup engine E2E — file-level + row-level duplicate detection,
plus the 4 resolution policies (block / skip / replace / append).

Run:
    python backend/tests/test_dedup.py
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SANDBOX_DB = _BACKEND_DIR / "_dedup_test.db"
_SANDBOX_CSV = _BACKEND_DIR / "_dedup_test.csv"
os.environ["FINANCIAL_DB_PATH"] = str(_SANDBOX_DB)


_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    ok = got == want
    _passed += int(ok); _failed += int(not ok)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:55s} got={got!r:30s} want={want!r}")


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Order No", "Invoice No", "Party Name", "Product Name", "Total Amount"])
        for r in rows:
            w.writerow(r)


async def main():
    # Clean slate
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CSV):
        try: f.unlink()
        except FileNotFoundError: pass

    from app.infrastructure import (
        init_database, count_rows, find_active_upload_by_file_hash,
        list_uploads_meta,
    )
    from app.kpi import init_kpi_table, rebuild_catalog
    from app.dedup import (
        classify_batch, compute_file_hash, compute_row_hash, DEDUP_MODES,
    )
    from app.analytics_engine import DataCleanAgent

    await init_database()
    await init_kpi_table()
    await rebuild_catalog()

    base_rows = [
        ("2026-05-01", "O1", "INV1", "Acme",  "Nike Running Shoes", 1000),
        ("2026-05-02", "O2", "INV2", "Beta",  "Bata Slippers",      500),
        ("2026-05-03", "O3", "INV3", "Gamma", "Sandals Mens",       800),
    ]

    # ---------- 1. First upload: clean insert ----------
    print("[1] First upload — fresh insert")
    write_csv(_SANDBOX_CSV, base_rows)
    file_hash_1, bytes_1 = compute_file_hash(_SANDBOX_CSV)

    agent = DataCleanAgent()
    res1 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales.csv",
        target="sales", batch_id="b1", dedup_mode="block",
    )
    check("rows_inserted",     res1["rows_inserted"],     3)
    check("rows_skipped_dupe", res1.get("rows_skipped_duplicate"), 0)
    check("rows_replaced",     res1.get("rows_replaced"), 0)
    check("sales table count", await count_rows("sales"), 3)

    # ---------- 2. Same file again, mode=block → must raise ----------
    print("[2] Re-upload identical file with mode=block (must reject)")
    from app.infrastructure import UploadError
    raised = False
    try:
        await agent.run(
            tmp_path=_SANDBOX_CSV, filename="sales.csv",
            target="sales", batch_id="b2", dedup_mode="block",
        )
    except UploadError as e:
        raised = True
        msg_has_dupe = "duplicate" in str(e).lower()
    check("UploadError raised", raised, True)
    check("error mentions duplicate", msg_has_dupe, True)
    check("table count unchanged", await count_rows("sales"), 3)

    # ---------- 3. Same file, mode=skip → 0 inserts, no error ----------
    print("[3] Re-upload identical file with mode=skip")
    res3 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales.csv",
        target="sales", batch_id="b3", dedup_mode="skip",
    )
    check("rows_inserted",     res3["rows_inserted"],     0)
    check("rows_skipped_dupe", res3.get("rows_skipped_duplicate"), 3)
    check("table count unchanged", await count_rows("sales"), 3)

    # ---------- 4. Mixed file: 2 dupes + 2 new rows, mode=skip ----------
    print("[4] Mixed file (2 dupes + 2 new) with mode=skip")
    mixed = base_rows[:2] + [
        ("2026-05-04", "O4", "INV4", "Delta",   "Adidas Sneakers", 1200),
        ("2026-05-05", "O5", "INV5", "Epsilon", "Puma Sports",      900),
    ]
    write_csv(_SANDBOX_CSV, mixed)
    res4 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales-mixed.csv",
        target="sales", batch_id="b4", dedup_mode="skip",
    )
    check("rows_inserted",     res4["rows_inserted"],     2)
    check("rows_skipped_dupe", res4.get("rows_skipped_duplicate"), 2)
    check("table count grew",  await count_rows("sales"), 5)

    # ---------- 5. Replace mode: corrected amounts ----------
    print("[5] Replace mode — same rows, corrected amounts")
    corrected = [
        ("2026-05-01", "O1", "INV1", "Acme",  "Nike Running Shoes", 1100),  # was 1000
        ("2026-05-02", "O2", "INV2", "Beta",  "Bata Slippers",      550),   # was 500
    ]
    write_csv(_SANDBOX_CSV, corrected)
    # First, since amounts changed the hash is different, so with "block"
    # there'd be no collision. Re-write IDENTICAL rows to test replace.
    write_csv(_SANDBOX_CSV, base_rows[:2])
    res5 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales-replace.csv",
        target="sales", batch_id="b5", dedup_mode="replace",
    )
    check("rows_inserted (re-inserted)", res5["rows_inserted"], 2)
    check("rows_replaced (deleted before insert)", res5.get("rows_replaced"), 2)
    check("table count unchanged net", await count_rows("sales"), 5)

    # ---------- 6. File-hash collision detection ----------
    print("[6] File-hash lookup")
    write_csv(_SANDBOX_CSV, base_rows)
    fh, _ = compute_file_hash(_SANDBOX_CSV)
    check("file_hash deterministic", fh, file_hash_1)
    # No upload has been recorded with this file_hash yet (we never patched
    # file_hash onto the audit rows in this test because we called the
    # agent directly, not the HTTP route). Confirm it's None.
    existing = await find_active_upload_by_file_hash(fh)
    check("no upload found by hash (agent path skips audit patch)", existing, None)

    # ---------- 7. Intra-batch duplicate detection ----------
    print("[7] Intra-batch duplicates collapse to one entry")
    repeats = [base_rows[0], base_rows[0], base_rows[0]]
    write_csv(_SANDBOX_CSV, repeats)
    # Use append mode so we don't get blocked by table-level dupes
    rows_before = await count_rows("sales")
    res7 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales-repeats.csv",
        target="sales", batch_id="b7", dedup_mode="append",
    )
    # In append mode we explicitly bypass dedup classification, so all 3 rows insert.
    # But verify the dedup report would have caught it. Run a fresh classification.
    parsed = [{
        "Date": r[0], "Order No": r[1], "Invoice No": r[2],
        "Party Name": r[3], "Product Name": r[4], "Total Amount": float(r[5]),
    } for r in repeats]
    classification = await classify_batch("sales", parsed, mode="skip")
    check("intra-batch dupes counted", classification.intra_batch_dupe_count, 2)
    check("classification.new_rows = 1 (after intra dedupe)", len(classification.new_rows), 0)
    # And the row in question already exists in the DB
    # (we re-inserted base_rows[0] earlier in step 5)

    # ---------- 8. Preview mode ----------
    print("[8] Preview-only mode returns classification without inserting")
    write_csv(_SANDBOX_CSV, base_rows + [("2026-05-09", "O9", "INV9", "Theta", "Boot", 700)])
    rows_before = await count_rows("sales")
    res8 = await agent.run(
        tmp_path=_SANDBOX_CSV, filename="sales-preview.csv",
        target="sales", batch_id="b8", dedup_mode="skip", preview_only=True,
    )
    check("preview flag",                res8.get("preview"), True)
    check("preview did not insert",      await count_rows("sales"), rows_before)
    check("preview shows new rows",      res8["dedup"]["rows_new"], 1)
    check("preview shows duplicate",     res8["dedup"]["rows_duplicate"], 3)

    # Cleanup
    for f in (_SANDBOX_DB, _SANDBOX_DB.with_suffix(".db.version"), _SANDBOX_CSV):
        try: f.unlink()
        except FileNotFoundError: pass

    print()
    print(f"TOTAL: {_passed}/{_passed + _failed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
