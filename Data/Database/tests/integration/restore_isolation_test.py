#!/usr/bin/env python3
"""Backup-restore verification that never writes to the live data lake.

Runs as a Kubernetes Job in the database image (which carries the
market_data_db library). Two phases:

  A. Isolated synthetic data. A throwaway lake is built under /scratch/iso,
     backed up, destroyed, and restored. Proves the backup/restore code path
     end to end against data this test owns entirely.

  B. The real archive, restored to an isolated target. The live volume is
     mounted READ-ONLY at /live; the archive produced by the live service is
     copied to /scratch/real and restored there. This proves the production
     backup is genuinely restorable -- including the rows the live integration
     test had just written through the Service -- while the live lake is
     physically unwritable to this pod.

Nothing in this job can modify the production data: the only read-write paths
are emptyDir scratch space.

Environment:
  ARCHIVE                  archive filename under /live/backups (phase B)
  TEST_PROVIDER            provider of the live test rows to look for
  EXPECTED_PROVIDER_ROWS   how many of those rows to expect
  BASELINE_TRADES/QUOTES   pre-existing physical row counts to find in the restore
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset

SCRATCH = Path(os.environ.get("SCRATCH_ROOT", "/scratch"))
LIVE = Path(os.environ.get("LIVE_ROOT", "/live"))
ARCHIVE = os.environ.get("ARCHIVE", "")
TEST_PROVIDER = os.environ.get("TEST_PROVIDER", "")
EXPECTED_PROVIDER_ROWS = int(os.environ.get("EXPECTED_PROVIDER_ROWS", "0") or 0)

ISO_PROVIDER = "restore-isolation-test"
ISO_SYMBOL = "ZZ-RESTORE"
ISO_ROWS = 200
ANCHOR = datetime(2019, 3, 4, 14, 30, tzinfo=timezone.utc)

_passed: list[str] = []
_failed: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        _passed.append(name)
    else:
        print(f"  FAIL  {name}  {detail}", flush=True)
        _failed.append((name, detail))
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def make_bars(symbol: str, n: int, base: float = 50.0):
    return [{
        "symbol": symbol,
        "asset_class": "equity",
        "timeframe": "1Min",
        "timestamp": ANCHOR + timedelta(minutes=i),
        "open": base + i,
        "high": base + i + 1.0,
        "low": base + i - 1.0,
        "close": base + i + 0.5,
        "volume": 500.0 + i,
        "trade_count": 5 + i,
        "vwap": base + i + 0.25,
    } for i in range(n)]


print("market-data-db backup / restore isolation test")
print(f"scratch: {SCRATCH}   live (read-only): {LIVE}")

# ------------------------------------------------------- phase A: synthetic lake --
section("Phase A -- backup and restore an isolated synthetic lake")

iso_root = SCRATCH / "iso"
shutil.rmtree(iso_root, ignore_errors=True)
config = DBConfig(root=iso_root)
config.ensure_dirs()

db = MarketDataDB(config)
written = db.write(Dataset.BARS, make_bars(ISO_SYMBOL, ISO_ROWS),
                   provider=ISO_PROVIDER, source="backfill")
check(f"wrote {ISO_ROWS} synthetic rows to the throwaway lake", written == ISO_ROWS,
      f"{written} rows written")

before = db.read(Dataset.BARS, providers=[ISO_PROVIDER], dedup="canonical")
check("the synthetic rows read back before backup", len(before) == ISO_ROWS,
      f"{len(before)} rows")
checksum_before = float(before["close"].sum())

archive_path = db.backup(label="isolation")
check("backup archive written", archive_path.exists() and archive_path.stat().st_size > 0,
      f"{archive_path.name} ({archive_path.stat().st_size} bytes)")
db.close()

# Simulate total loss of the data lake: remove every parquet zone and the
# operational metadata database.
for sub in ("canonical", "landing", "archive"):
    shutil.rmtree(iso_root / sub, ignore_errors=True)
meta_file = iso_root / "metadata" / "metadata.sqlite"
meta_file.unlink(missing_ok=True)
check("data lake destroyed before restore",
      not (iso_root / "landing").exists() and not meta_file.exists(),
      "landing zone and metadata removed")

db = MarketDataDB(DBConfig(root=iso_root))
db.restore(archive_path, overwrite=False)
db.close()

db = MarketDataDB(DBConfig(root=iso_root))
after = db.read(Dataset.BARS, providers=[ISO_PROVIDER], dedup="canonical")
check("every row returned after restore", len(after) == ISO_ROWS, f"{len(after)} rows")
check("restored values are identical (close-price checksum)",
      abs(float(after["close"].sum()) - checksum_before) < 1e-9,
      f"{float(after['close'].sum()):.2f} vs {checksum_before:.2f}")

cols_match = list(before.columns) == list(after.columns)
check("restored schema matches the original", cols_match,
      f"{len(after.columns)} columns")

coverage = db.coverage(Dataset.BARS)
check("operational metadata (coverage) restored, not just the parquet files",
      not coverage.empty, f"{len(coverage)} coverage rows")

# A restore must refuse to clobber a populated lake unless told to.
refused = False
try:
    db.restore(archive_path, overwrite=False)
except RuntimeError:
    refused = True
check("a second restore over the now-populated lake is refused", refused,
      "RuntimeError raised as expected")

db.restore(archive_path, overwrite=True)
db.close()
db = MarketDataDB(DBConfig(root=iso_root))
forced = db.read(Dataset.BARS, providers=[ISO_PROVIDER], dedup="canonical")
check("an explicit overwrite=True restore succeeds", len(forced) == ISO_ROWS,
      f"{len(forced)} rows")
db.close()
shutil.rmtree(iso_root, ignore_errors=True)

# --------------------------------------------- phase B: the real archive, isolated --
section("Phase B -- restore the production archive into an isolated root")

if not ARCHIVE:
    print("  SKIP  no ARCHIVE supplied; phase B not run", flush=True)
elif not (LIVE / "backups" / ARCHIVE).exists():
    check(f"production archive {ARCHIVE} present on the volume", False,
          f"not found under {LIVE}/backups")
else:
    src = LIVE / "backups" / ARCHIVE
    size = src.stat().st_size
    check("production archive found on the live volume (read-only mount)", size > 0,
          f"{ARCHIVE} ({size / 1024 ** 2:.1f} MiB)")

    # Confirm the live mount really is read-only, so this job provably cannot
    # damage production data.
    read_only = False
    try:
        (LIVE / ".restore-test-write-probe").touch()
    except OSError:
        read_only = True
    check("the live volume is mounted read-only in this job", read_only,
          "write probe rejected by the kernel")

    real_root = SCRATCH / "real"
    shutil.rmtree(real_root, ignore_errors=True)
    real_config = DBConfig(root=real_root)
    real_config.ensure_dirs()
    shutil.copy2(src, real_config.backup_root / ARCHIVE)

    db = MarketDataDB(real_config)
    db.restore(real_config.backup_root / ARCHIVE, overwrite=False)
    db.close()

    db = MarketDataDB(DBConfig(root=real_root))
    for dataset in ("trades", "quotes", "bars"):
        baseline = os.environ.get(f"BASELINE_{dataset.upper()}")
        if not baseline or baseline == "0":
            print(f"  SKIP  {dataset}: no baseline supplied", flush=True)
            continue
        count = db.row_count(Dataset(dataset), dedup=None)
        check(f"{dataset}: pre-existing rows present in the restored copy",
              count >= int(baseline), f"{count} rows (baseline {baseline})")

    if TEST_PROVIDER and EXPECTED_PROVIDER_ROWS:
        rows = db.read(Dataset.BARS, providers=[TEST_PROVIDER], dedup="canonical")
        check("rows written through the live Service survived backup and restore",
              len(rows) == EXPECTED_PROVIDER_ROWS,
              f"{len(rows)} rows (expected {EXPECTED_PROVIDER_ROWS})")

    coverage = db.coverage()
    check("coverage metadata restored from the production archive",
          not coverage.empty, f"{len(coverage)} coverage rows")
    db.close()
    shutil.rmtree(real_root, ignore_errors=True)

print(f"\n=== {len(_passed)} passed, {len(_failed)} failed ===")
if _failed:
    for name, detail in _failed:
        print(f"  FAILED: {name}  {detail}")
    sys.exit(1)
