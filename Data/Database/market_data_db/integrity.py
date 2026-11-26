from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import DBConfig, connect_duckdb
from .schema import Dataset, get_spec
from .storage.parquet_store import build_scan_sql


@dataclass
class DuplicateReport:
    dataset: Dataset
    duplicate_keys: int
    duplicate_rows: int
    sample: pd.DataFrame


@dataclass
class GapReport:
    symbol: str
    timeframe: str | None
    gaps: pd.DataFrame  # columns: prev_ts, next_ts, gap_seconds


def find_duplicates(config: DBConfig, dataset: Dataset, *, sample_limit: int = 20) -> DuplicateReport:
    """Detect rows that share a primary key within the raw (un-deduped) lake.

    This is expected to find rows across the landing zone that `query(dedup="exact")`
    would already collapse at read time — a non-zero count here just confirms the
    dedup path has real work to do, not a correctness problem. A canonical+landing
    partition with leftover duplicates *after* compaction would indicate a bug.
    """
    spec = get_spec(dataset)
    scan = build_scan_sql(config, dataset)
    if scan is None:
        return DuplicateReport(dataset, 0, 0, pd.DataFrame())

    pk = ", ".join(spec.primary_key)
    con = connect_duckdb(config)
    try:
        dup_keys_sql = f"""
            SELECT {pk}, COUNT(*) AS n
            FROM {scan}
            GROUP BY {pk}
            HAVING COUNT(*) > 1
        """
        dup_df = con.execute(dup_keys_sql).fetchdf()
        duplicate_rows = int(dup_df["n"].sum()) if not dup_df.empty else 0
        return DuplicateReport(dataset, len(dup_df), duplicate_rows, dup_df.head(sample_limit))
    finally:
        con.close()


def find_gaps(
    config: DBConfig,
    dataset: Dataset,
    symbol: str,
    *,
    providers: list[str] | None = None,
    max_gap_seconds: float,
) -> GapReport:
    """Flag consecutive-observation gaps larger than max_gap_seconds for one symbol
    (e.g. missing bars in an OHLCV series)."""
    spec = get_spec(dataset)
    scan = build_scan_sql(config, dataset)
    if scan is None:
        return GapReport(symbol, None, pd.DataFrame())

    con = connect_duckdb(config)
    try:
        clauses = [f"{spec.symbol_col} = ?"]
        params: list = [symbol]
        if providers:
            placeholders = ", ".join("?" for _ in providers)
            clauses.append(f"provider IN ({placeholders})")
            params.extend(providers)
        where_sql = " AND ".join(clauses)
        sql = f"""
            WITH ordered AS (
                SELECT DISTINCT {spec.time_col} AS ts
                FROM {scan}
                WHERE {where_sql}
                ORDER BY ts
            )
            SELECT
                ts AS prev_ts,
                LEAD(ts) OVER (ORDER BY ts) AS next_ts,
                date_diff('second', ts, LEAD(ts) OVER (ORDER BY ts)) AS gap_seconds
            FROM ordered
        """
        df = con.execute(sql, params).fetchdf()
        df = df.dropna(subset=["next_ts"])
        gaps = df[df["gap_seconds"] > max_gap_seconds]
        return GapReport(symbol, None, gaps.reset_index(drop=True))
    finally:
        con.close()


def row_count(config: DBConfig, dataset: Dataset, *, dedup: str | None = "exact") -> int:
    """Count rows without materializing them (safe for lakes with many millions of rows)."""
    spec = get_spec(dataset)
    scan = build_scan_sql(config, dataset)
    if scan is None:
        return 0
    con = connect_duckdb(config)
    try:
        if dedup is None:
            return int(con.execute(f"SELECT COUNT(*) FROM {scan}").fetchone()[0])
        if dedup == "exact":
            part_cols = list(spec.primary_key)
        elif dedup == "canonical":
            part_cols = [c for c in spec.primary_key if c != "provider"]
        else:
            raise ValueError(f"unknown dedup mode: {dedup}")
        sql = f"SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(part_cols)} FROM {scan})"
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()
