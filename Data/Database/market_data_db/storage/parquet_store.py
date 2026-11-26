from __future__ import annotations

import glob as globmod
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import DBConfig, connect_duckdb
from ..timestamps import parse_utc_series
from ..paths import archive_root, partition_dir
from ..schema import Dataset, DatasetSpec, get_spec
from .lock import partition_lock


@dataclass
class WriteResult:
    rows_written: int
    partitions: list[Path]
    files: list[Path]


def _partition_groups(df: pd.DataFrame, spec: DatasetSpec) -> Iterable[tuple[str | None, date, pd.DataFrame]]:
    ts = parse_utc_series(df[spec.time_col])
    df = df.copy()
    df["_date"] = ts.dt.date
    key_cols = ([spec.symbol_col] if spec.symbol_col else []) + ["_date"]
    for key, gdf in df.groupby(key_cols, sort=False):
        if spec.symbol_col:
            symbol_val, day = key
        else:
            symbol_val, day = None, key
        yield symbol_val, day, gdf.drop(columns=["_date"])


def write_batch(
    config: DBConfig,
    dataset: Dataset,
    provider: str,
    source: str,
    table: pa.Table,
) -> WriteResult:
    """Append a batch as one or more new, immutable Parquet files into the landing
    zone, one file per (symbol, date) partition. Append-only: no locking, safe for
    many concurrent writers (live + backfill + multiple symbols) at once.
    """
    spec = get_spec(dataset)
    if table.num_rows == 0:
        return WriteResult(0, [], [])

    df = table.to_pandas()
    partitions: list[Path] = []
    files: list[Path] = []
    rows = 0
    for symbol_val, day, gdf in _partition_groups(df, spec):
        part_dir = partition_dir(
            config.landing_root, spec.name, provider, source, symbol_val or "_all", day
        )
        part_dir.mkdir(parents=True, exist_ok=True)
        out_table = pa.Table.from_pandas(gdf, schema=spec.schema, preserve_index=False)
        fname = f"{uuid.uuid4().hex}.parquet"
        tmp_path = part_dir / f".{fname}.tmp"
        final_path = part_dir / fname
        pq.write_table(
            out_table,
            tmp_path,
            compression=config.compression,
            row_group_size=config.row_group_size,
        )
        os.replace(tmp_path, final_path)
        partitions.append(part_dir)
        files.append(final_path)
        rows += len(gdf)
    return WriteResult(rows, partitions, files)


def _existing_globs(zone_root: Path, dataset: Dataset) -> list[str]:
    pattern = str(zone_root / f"dataset={dataset.value}" / "**" / "*.parquet")
    if globmod.glob(pattern, recursive=True):
        return [pattern]
    return []


def _quote_globs(globs: Sequence[str]) -> str:
    return "[" + ", ".join(f"'{g}'" for g in globs) + "]"


def build_scan_sql(config: DBConfig, dataset: Dataset) -> str | None:
    globs = _existing_globs(config.canonical_root, dataset) + _existing_globs(config.landing_root, dataset)
    if not globs:
        return None
    return f"read_parquet({_quote_globs(globs)}, hive_partitioning=true, union_by_name=true)"


def query(
    config: DBConfig,
    dataset: Dataset,
    *,
    symbols: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    dedup: str | None = "exact",
    columns: Sequence[str] | None = None,
    extra_where: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Read rows for a dataset out of the Parquet lake (landing + canonical zones).

    dedup:
      None       -> return every physical row (raw access, for integrity checks).
      "exact"    -> collapse exact re-ingestion duplicates (same primary key),
                    keeping the row with the newest ingested_at.
      "canonical"-> like "exact" but also collapses across provider/source for the
                    same (symbol, time) using the dataset's source_priority order
                    (live beats backfill), so overlapping backfill never shadows a
                    live observation.
    as_of: point-in-time read — only rows ingested at or before this timestamp are
      considered, and dedup picks the latest such ingestion per key.
    """
    spec = get_spec(dataset)
    scan = build_scan_sql(config, dataset)
    if scan is None:
        return pd.DataFrame()

    con = connect_duckdb(config)
    try:
        select_cols = ", ".join(columns) if columns else "*"
        clauses: list[str] = []
        params: list = []

        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            clauses.append(f"{spec.symbol_col} IN ({placeholders})")
            params.extend(symbols)
        if providers:
            placeholders = ", ".join("?" for _ in providers)
            clauses.append(f"provider IN ({placeholders})")
            params.extend(providers)
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        if start is not None:
            clauses.append(f"{spec.time_col} >= ?")
            params.append(start)
        if end is not None:
            clauses.append(f"{spec.time_col} < ?")
            params.append(end)
        if as_of is not None:
            clauses.append("ingested_at <= ?")
            params.append(as_of)
        if extra_where:
            clauses.append(extra_where)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        base_sql = f"SELECT {select_cols} FROM {scan} {where_sql}"

        if dedup is None:
            sql = base_sql
        else:
            if dedup == "exact":
                part_cols = list(spec.primary_key)
                order_sql = "ingested_at DESC"
            elif dedup == "canonical":
                part_cols = [c for c in spec.primary_key if c != "provider"]
                priority_case = "CASE source " + " ".join(
                    f"WHEN '{s}' THEN {i}" for i, s in enumerate(spec.source_priority)
                ) + f" ELSE {len(spec.source_priority)} END"
                order_sql = f"{priority_case} ASC, ingested_at DESC"
            else:
                raise ValueError(f"unknown dedup mode: {dedup}")

            inner_cols = select_cols if columns is None else ", ".join(
                set(columns) | set(part_cols) | {"ingested_at", "source", "provider"}
            )
            sql = (
                f"SELECT {select_cols} FROM ("
                f"SELECT {inner_cols}, ROW_NUMBER() OVER ("
                f"PARTITION BY {', '.join(part_cols)} ORDER BY {order_sql}"
                f") AS _rn FROM {scan} {where_sql}"
                f") WHERE _rn = 1"
            )

        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        return con.execute(sql, params).fetchdf()
    finally:
        con.close()


def compact_partition(
    config: DBConfig,
    dataset: Dataset,
    provider: str,
    source: str,
    symbol: str,
    day: date,
) -> int:
    """Merge every landing file in one partition (plus the existing canonical file,
    if any) into a single deduplicated canonical Parquet file, then archive the
    merged landing files. Safe under concurrent ingestion: only the files present at
    the moment of enumeration are merged/archived; anything written afterward is
    left for the next compaction run.
    """
    spec = get_spec(dataset)
    landing_dir = partition_dir(config.landing_root, dataset, provider, source, symbol, day)
    canonical_dir = partition_dir(config.canonical_root, dataset, provider, source, symbol, day)

    with partition_lock(canonical_dir):
        landing_files = sorted(landing_dir.glob("*.parquet")) if landing_dir.exists() else []
        canonical_file = canonical_dir / "data.parquet"
        existing = [canonical_file] if canonical_file.exists() else []
        all_files = existing + landing_files
        if not all_files:
            return 0

        tables = [pq.read_table(f) for f in all_files]
        merged = pa.concat_tables(tables, promote_options="default")
        df = merged.to_pandas()
        df = df.sort_values("ingested_at").drop_duplicates(subset=list(spec.primary_key), keep="last")
        out_table = pa.Table.from_pandas(df, schema=spec.schema, preserve_index=False)

        canonical_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = canonical_dir / ".data.parquet.tmp"
        pq.write_table(out_table, tmp_path, compression=config.compression, row_group_size=config.row_group_size)
        os.replace(tmp_path, canonical_file)

        if landing_files:
            arch_dir = archive_root(config) / landing_dir.relative_to(config.landing_root)
            arch_dir.mkdir(parents=True, exist_ok=True)
            for f in landing_files:
                shutil.move(str(f), str(arch_dir / f.name))

        return len(df)


def iter_partitions(config: DBConfig, dataset: Dataset) -> Iterable[tuple[str, str, str, date]]:
    """Enumerate distinct (provider, source, symbol, date) partitions present in the
    landing zone for a dataset, for driving compaction sweeps."""
    root = config.landing_root / f"dataset={dataset.value}"
    if not root.exists():
        return
    for provider_dir in root.glob("provider=*"):
        provider = provider_dir.name.split("=", 1)[1]
        for source_dir in provider_dir.glob("source=*"):
            source = source_dir.name.split("=", 1)[1]
            for symbol_dir in source_dir.glob("symbol=*"):
                symbol = symbol_dir.name.split("=", 1)[1]
                for date_dir in symbol_dir.glob("date=*"):
                    day = date.fromisoformat(date_dir.name.split("=", 1)[1])
                    if any(date_dir.glob("*.parquet")):
                        yield provider, source, symbol, day
