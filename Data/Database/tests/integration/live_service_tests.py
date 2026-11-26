#!/usr/bin/env python3
"""Integration tests run INSIDE the cluster against the deployed market-data-db
ClusterIP Service.

Executed as a Kubernetes Job (kubernetes/database/integration-tests.yaml), so
every assertion exercises the real network path: pod -> NetworkPolicy ->
ClusterIP Service -> uvicorn -> the parquet store on the OCI block volume.
Nothing here touches the data files directly.

Environment:
  DB_URL                  http://market-data-db.algot-data.svc.cluster.local:8000
  MARKET_DATA_DB_API_KEY  from the market-data-db-client Secret
  RUN_ID                  unique per run; isolates this run's test data
  BASELINE_TRADES/QUOTES/BARS
                          physical row counts captured before the run, asserted
                          to be intact afterwards (0 or unset skips the check)

Isolation: every record written carries provider="itest-<RUN_ID>". `provider`
is part of the primary key of every dataset and of the on-disk partition path,
so this run's rows cannot collide with, dedup against, or compact into real
market data -- and compaction can be scoped to exactly these rows.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DB_URL = os.environ["DB_URL"].rstrip("/")
API_KEY = os.environ["MARKET_DATA_DB_API_KEY"]
RUN_ID = os.environ.get("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

PROVIDER = f"itest-{RUN_ID}"
SOURCE = "backfill"
SYM_MAIN = "ZZ-ITEST-MAIN"
N_MAIN = 120
CONCURRENT_WRITERS = 6
CONCURRENT_READERS = 6
N_PER_WRITER = 50

# Bars are written on a fixed 1-minute grid anchored well in the past, so the
# synthetic rows can never be mistaken for a live quote and the gap check has a
# deterministic expectation.
ANCHOR = datetime(2020, 1, 6, 14, 30, tzinfo=timezone.utc)

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


def call(method, path, body=None, key=API_KEY, timeout=180):
    """Returns (status_code, parsed_body). HTTP errors are returned, not raised,
    so that 401/404/409 can be asserted as outcomes."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DB_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key is not None:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"detail": raw}
        return exc.code, payload


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def make_bars(symbol: str, n: int, anchor: datetime = ANCHOR, base: float = 100.0):
    """Deterministic 1-minute bars. Every schema column is supplied, including
    the nullable ones, because the writer requires all of them to be present."""
    out = []
    for i in range(n):
        out.append({
            "symbol": symbol,
            "asset_class": "equity",
            "timeframe": "1Min",
            "timestamp": iso(anchor + timedelta(minutes=i)),
            "open": base + i,
            "high": base + i + 1.0,
            "low": base + i - 1.0,
            "close": base + i + 0.5,
            "volume": 1000.0 + i,
            "trade_count": 10 + i,
            "vwap": base + i + 0.25,
        })
    return out


def write_bars(records):
    return call("POST", "/v1/datasets/bars/write",
                {"provider": PROVIDER, "source": SOURCE, "records": records})


def read_ours(dedup="canonical", symbols=None, limit=None):
    """Read back only this run's rows. Filtering by provider makes every count
    below immune to live ingestion writing to the same dataset concurrently."""
    body = {"providers": [PROVIDER], "dedup": dedup}
    if symbols is not None:
        body["symbols"] = symbols
    if limit is not None:
        body["limit"] = limit
    status, payload = call("POST", "/v1/datasets/bars/read", body)
    if status != 200:
        return status, []
    return status, payload.get("rows", [])


# --------------------------------------------------------------------------- probes --

print(f"market-data-db live integration tests -- run {RUN_ID}")
print(f"target   : {DB_URL}")
print(f"provider : {PROVIDER}  (isolates this run's data)")

section("Service reachability and health")
status, payload = call("GET", "/health", key=None)
check("/health reachable over ClusterIP DNS without auth", status == 200, f"status {status}")
check("/health reports status=ok", payload.get("status") == "ok", json.dumps(payload))
check("/health reports the volume mount as the db root", payload.get("root") == "/data",
      f"root={payload.get('root')}")

section("Authentication")
status, _ = call("GET", "/v1/datasets/bars/row-count", key=None)
check("request with no X-API-Key is rejected 401", status == 401, f"status {status}")

status, _ = call("GET", "/v1/datasets/bars/row-count", key="not-the-real-key")
check("request with a wrong X-API-Key is rejected 401", status == 401, f"status {status}")

status, _ = call("POST", "/v1/datasets/bars/write",
                 {"provider": PROVIDER, "source": SOURCE, "records": []}, key=None)
check("unauthenticated write is rejected 401", status == 401, f"status {status}")

status, payload = call("GET", "/v1/datasets/bars/row-count")
check("request with the Secret's key is accepted 200", status == 200, f"status {status}")
baseline_bars_physical = None
status, payload = call("GET", "/v1/datasets/bars/row-count?dedup=none")
if status == 200:
    baseline_bars_physical = payload.get("row_count")

section("Storage status and capacity")
status, payload = call("GET", "/v1/storage/status")
if check("/v1/storage/status returns 200", status == 200, f"status {status}"):
    total_gib = payload.get("total_bytes", 0) / 1024 ** 3
    free_gib = payload.get("free_bytes", 0) / 1024 ** 3
    check("reported volume size is the 80 GB block volume", 70 <= total_gib <= 85,
          f"{total_gib:.1f} GiB total")
    check("free space is reported and non-zero", payload.get("free_bytes", 0) > 0,
          f"{free_gib:.1f} GiB free")
    check("backfill is permitted at current utilisation",
          payload.get("backfill_allowed") is True,
          f"backfill_allowed={payload.get('backfill_allowed')}")

section("Write / read round trip and data integrity")
status, payload = write_bars(make_bars(SYM_MAIN, N_MAIN))
check(f"write of {N_MAIN} bars accepted", status == 200 and payload.get("rows_written") == N_MAIN,
      f"status {status} rows_written={payload.get('rows_written')}")

status, rows = read_ours()
check(f"read back returns exactly {N_MAIN} rows", len(rows) == N_MAIN, f"{len(rows)} rows")

# Value-level integrity: every field must survive the parquet round trip
# unchanged, not merely the row count.
expected = {b["timestamp"].replace("Z", ""): b for b in make_bars(SYM_MAIN, N_MAIN)}
mismatches = []
for row in rows:
    ts = str(row.get("timestamp", ""))
    key = next((k for k in expected if ts.startswith(k[:16])), None)
    exp = expected.get(key) if key else None
    if exp is None:
        mismatches.append(f"unexpected timestamp {ts}")
        continue
    for field in ("open", "high", "low", "close", "volume", "vwap"):
        if abs(float(row[field]) - float(exp[field])) > 1e-9:
            mismatches.append(f"{ts} {field}: {row[field]} != {exp[field]}")
    if row.get("symbol") != SYM_MAIN or row.get("timeframe") != "1Min":
        mismatches.append(f"{ts} identity columns wrong")
check("every column round-trips bit-exact through parquet", not mismatches,
      "; ".join(mismatches[:3]))
check("provider/source stamped on every row",
      all(r.get("provider") == PROVIDER and r.get("source") == SOURCE for r in rows),
      "provider or source mismatch")

status, payload = call("GET", f"/v1/datasets/bars/latest/{SYM_MAIN}")
latest_ts = (payload.get("row") or {}).get("timestamp", "")
check("latest endpoint returns the newest bar", status == 200 and
      str(latest_ts).startswith(iso(ANCHOR + timedelta(minutes=N_MAIN - 1))[:16]),
      f"latest timestamp {latest_ts}")

section("Deduplication")
status, payload = call("GET", "/v1/datasets/bars/integrity/duplicates")
dups_before = payload.get("duplicate_rows", 0) if status == 200 else 0

status, payload = write_bars(make_bars(SYM_MAIN, N_MAIN))
check("re-writing an identical batch is accepted (idempotent write)",
      status == 200 and payload.get("rows_written") == N_MAIN,
      f"rows_written={payload.get('rows_written')}")

status, rows = read_ours(dedup="canonical")
check("canonical read still returns one row per key after the duplicate write",
      len(rows) == N_MAIN, f"{len(rows)} rows (expected {N_MAIN})")

status, rows_exact = read_ours(dedup="exact")
check("exact-dedup read also collapses the duplicates", len(rows_exact) == N_MAIN,
      f"{len(rows_exact)} rows")

status, rows_phys = read_ours(dedup=None)
check("the duplicates are physically present before compaction",
      len(rows_phys) == 2 * N_MAIN, f"{len(rows_phys)} physical rows (expected {2 * N_MAIN})")

status, payload = call("GET", "/v1/datasets/bars/integrity/duplicates")
dups_after = payload.get("duplicate_rows", 0) if status == 200 else 0
check("the integrity report detects the duplicate keys", dups_after > dups_before,
      f"duplicate_rows {dups_before} -> {dups_after}")

section("Compaction")
status, payload = call("POST",
                       f"/v1/datasets/bars/compact?provider={PROVIDER}&source={SOURCE}")
check("compaction scoped to this run's provider succeeds",
      status == 200 and payload.get("rows_compacted", 0) > 0,
      f"status {status} rows_compacted={payload.get('rows_compacted')}")

status, rows_phys = read_ours(dedup=None)
check("compaction physically removed the duplicate rows", len(rows_phys) == N_MAIN,
      f"{len(rows_phys)} physical rows (expected {N_MAIN})")

status, rows = read_ours()
check("no data was lost by compaction", len(rows) == N_MAIN, f"{len(rows)} rows")

section("Point-in-time queries")
status, payload = call("POST", "/v1/datasets/bars/point-in-time",
                       {"providers": [PROVIDER], "as_of": iso(datetime.now(timezone.utc))})
check("as_of=now returns this run's rows", status == 200 and
      len(payload.get("rows", [])) == N_MAIN,
      f"status {status} rows={len(payload.get('rows', []))}")

status, payload = call("POST", "/v1/datasets/bars/point-in-time",
                       {"providers": [PROVIDER],
                        "as_of": iso(datetime.now(timezone.utc) - timedelta(days=1))})
check("as_of before ingestion excludes them (no time travel)",
      status == 200 and len(payload.get("rows", [])) == 0,
      f"rows={len(payload.get('rows', []))}")

section("Continuity and coverage")
status, payload = call("GET",
                       f"/v1/datasets/bars/integrity/gaps?symbol={SYM_MAIN}&max_gap_seconds=120")
check("contiguous 1-minute bars report no gaps",
      status == 200 and len(payload.get("gaps", [])) == 0,
      f"status {status} gaps={len(payload.get('gaps', []))}")

status, payload = call("GET", f"/v1/datasets/bars/coverage?symbol={SYM_MAIN}")
check("coverage metadata recorded for the written symbol",
      status == 200 and len(payload.get("rows", [])) > 0,
      f"status {status} rows={len(payload.get('rows', []))}")

section("Concurrent reads and writes")
errors: list[str] = []
read_counts: list[int] = []
lock = threading.Lock()


def writer(idx: int) -> None:
    sym = f"ZZ-ITEST-C{idx}"
    try:
        status, payload = write_bars(make_bars(sym, N_PER_WRITER, base=200.0 + idx))
        if status != 200 or payload.get("rows_written") != N_PER_WRITER:
            with lock:
                errors.append(f"writer {idx}: status {status} rows={payload.get('rows_written')}")
    except Exception as exc:  # noqa: BLE001
        with lock:
            errors.append(f"writer {idx}: {exc!r}")


def reader(idx: int) -> None:
    try:
        for _ in range(4):
            status, rows = read_ours(symbols=[SYM_MAIN])
            if status != 200:
                with lock:
                    errors.append(f"reader {idx}: status {status}")
                return
            with lock:
                read_counts.append(len(rows))
    except Exception as exc:  # noqa: BLE001
        with lock:
            errors.append(f"reader {idx}: {exc!r}")


threads = [threading.Thread(target=writer, args=(i,)) for i in range(CONCURRENT_WRITERS)]
threads += [threading.Thread(target=reader, args=(i,)) for i in range(CONCURRENT_READERS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check(f"{CONCURRENT_WRITERS} concurrent writers and {CONCURRENT_READERS} readers all succeeded",
      not errors, "; ".join(errors[:3]))
check("every concurrent read saw a complete, consistent snapshot",
      bool(read_counts) and set(read_counts) == {N_MAIN},
      f"observed row counts {sorted(set(read_counts))} (expected all {N_MAIN})")

expected_total = N_MAIN + CONCURRENT_WRITERS * N_PER_WRITER
status, rows = read_ours()
check("no write was lost under concurrency", len(rows) == expected_total,
      f"{len(rows)} rows (expected {expected_total})")

for i in range(CONCURRENT_WRITERS):
    sym = f"ZZ-ITEST-C{i}"
    status, rows = read_ours(symbols=[sym])
    if len(rows) != N_PER_WRITER:
        check(f"writer {i}'s rows are all present", False,
              f"{len(rows)} rows (expected {N_PER_WRITER})")
        break
else:
    check("each concurrent writer's rows are individually complete", True,
          f"{CONCURRENT_WRITERS} symbols x {N_PER_WRITER} rows")

section("Backup creation")
status, payload = call("POST", f"/v1/backups?label=itest-{RUN_ID}", timeout=900)
archive = payload.get("archive", "")
check("backup archive created on the volume", status == 200 and archive.endswith(".tar.gz"),
      f"status {status} archive={archive}")

status, payload = call("GET", "/v1/backups")
listed = {b["name"]: b["bytes"] for b in payload.get("backups", [])} if status == 200 else {}
check("the new archive is listed with a non-zero size",
      archive in listed and listed[archive] > 0,
      f"{listed.get(archive, 'absent')} bytes")

section("Restore safety guards")
if archive:
    status, payload = call("POST", "/v1/backups/restore",
                           {"archive": archive, "overwrite": False})
    check("restore over a populated lake is refused without overwrite (409)",
          status == 409, f"status {status} {payload.get('detail', '')}")

status, payload = call("POST", "/v1/backups/restore",
                       {"archive": "../../../etc/passwd", "overwrite": True})
check("restore rejects a path-traversal archive name (404)", status == 404,
      f"status {status}")

status, payload = call("POST", "/v1/backups/restore",
                       {"archive": "no-such-backup.tar.gz", "overwrite": True})
check("restore rejects an unknown archive (404)", status == 404, f"status {status}")

section("Pre-existing records preserved")
for dataset in ("trades", "quotes", "bars"):
    baseline = os.environ.get(f"BASELINE_{dataset.upper()}")
    if not baseline or baseline == "0":
        print(f"  SKIP  {dataset}: no baseline supplied", flush=True)
        continue
    status, payload = call("GET", f"/v1/datasets/{dataset}/row-count?dedup=none")
    actual = payload.get("row_count", -1) if status == 200 else -1
    floor = int(baseline)
    # bars also carries this run's synthetic rows, hence >= rather than ==.
    check(f"{dataset}: all {floor} pre-existing rows still present", actual >= floor,
          f"{actual} physical rows now (baseline {floor})")

# --------------------------------------------------------------------------- summary --
print(f"\n=== {len(_passed)} passed, {len(_failed)} failed ===")
if archive:
    print(f"ARCHIVE={archive}")
print(f"TEST_PROVIDER={PROVIDER}")
if _failed:
    for name, detail in _failed:
        print(f"  FAILED: {name}  {detail}")
    sys.exit(1)
