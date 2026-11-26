from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pyarrow as pa
from sqlalchemy import func

from . import integrity as integrity_mod
from .backup import create_backup, restore_backup
from .config import DBConfig
from .metadata.engine import make_engine, make_session_factory
from .metadata.models import Coverage, IngestionRun
from .schema import Dataset, DatasetSpec, get_spec
from .storage import parquet_store
from .timestamps import now_utc

RecordsLike = Sequence[Mapping[str, Any]] | pd.DataFrame | pa.Table


def _naive_utc(ts: Any) -> datetime:
    """SQLite has no native tz-aware datetime type: SQLAlchemy round-trips it as
    naive. Store everything as naive-UTC in the metadata DB and re-attach UTC
    tzinfo on the way out, so comparisons stay consistent either way."""
    dt = pd.Timestamp(ts)
    if dt.tzinfo is not None:
        dt = dt.tz_convert("UTC").tz_localize(None)
    return dt.to_pydatetime()


class MarketDataDB:
    """Unified read/write interface for the market data lake.

    One instance owns a Parquet data lake (landing + canonical zones, under
    config.root) and an operational metadata database (SQLite by default,
    Postgres via config.metadata_url) that tracks ingestion runs and coverage.
    """

    def __init__(self, config: DBConfig | None = None):
        self.config = config or DBConfig()
        self.config.ensure_dirs()
        self._engine = make_engine(self.config)
        self._Session = make_session_factory(self._engine)

    # ---------------------------------------------------------------- writes --

    def write(
        self,
        dataset: Dataset | str,
        records: RecordsLike,
        *,
        provider: str,
        source: str,
    ) -> int:
        """Write a batch of records for one dataset. `records` may be a list of
        dicts, a pandas DataFrame, or a pyarrow Table whose columns are a subset of
        the dataset's schema fields (provider/source/ingested_at are filled in
        automatically). Idempotent: re-writing the exact same records is safe —
        reads deduplicate by primary key, and compact() physically removes the
        resulting duplicates.
        """
        ds = Dataset(dataset)
        spec = get_spec(ds)
        started = now_utc()
        try:
            table = self._prepare_table(spec, records, provider, source)
            result = parquet_store.write_batch(self.config, ds, provider, source, table)
            self._record_run(ds, provider, source, result.rows_written, "ok", None, started)
            if result.rows_written:
                self._update_coverage(ds, provider, source, table, spec)
            return result.rows_written
        except Exception as exc:  # noqa: BLE001 - record failure, then re-raise
            self._record_run(ds, provider, source, 0, "error", str(exc), started)
            raise

    def _prepare_table(
        self, spec: DatasetSpec, records: RecordsLike, provider: str, source: str
    ) -> pa.Table:
        if isinstance(records, pa.Table):
            df = records.to_pandas()
        elif isinstance(records, pd.DataFrame):
            df = records.copy()
        else:
            df = pd.DataFrame(list(records))

        if df.empty:
            return pa.Table.from_pylist([], schema=spec.schema)

        df["provider"] = provider
        df["source"] = source
        if "ingested_at" not in df.columns:
            df["ingested_at"] = now_utc()
        if "raw" not in df.columns:
            df["raw"] = None

        for field in spec.schema:
            if field.name not in df.columns:
                raise ValueError(f"missing required column '{field.name}' for dataset {spec.name.value}")
            if pa.types.is_timestamp(field.type):
                df[field.name] = pd.to_datetime(df[field.name], utc=True)
            elif pa.types.is_date32(field.type):
                df[field.name] = pd.to_datetime(df[field.name]).dt.date

        df = df[[f.name for f in spec.schema]]
        return pa.Table.from_pandas(df, schema=spec.schema, preserve_index=False)

    def _record_run(
        self,
        dataset: Dataset,
        provider: str,
        source: str,
        rows: int,
        status: str,
        error: str | None,
        started: datetime,
    ) -> None:
        with self._Session() as session:
            session.add(
                IngestionRun(
                    dataset=dataset.value,
                    provider=provider,
                    source=source,
                    symbol=None,
                    rows_written=rows,
                    status=status,
                    error=error,
                    started_at=_naive_utc(started),
                    completed_at=_naive_utc(now_utc()),
                )
            )
            session.commit()

    def _update_coverage(
        self, dataset: Dataset, provider: str, source: str, table: pa.Table, spec: DatasetSpec
    ) -> None:
        if not spec.symbol_col:
            return
        df = table.to_pandas()
        grouped = df.groupby(spec.symbol_col)[spec.time_col].agg(["min", "max", "count"])
        now = now_utc()
        with self._Session() as session:
            for symbol, row in grouped.iterrows():
                existing = (
                    session.query(Coverage)
                    .filter_by(dataset=dataset.value, provider=provider, source=source, symbol=symbol)
                    .one_or_none()
                )
                min_ts, max_ts, count = _naive_utc(row["min"]), _naive_utc(row["max"]), int(row["count"])
                if existing is None:
                    session.add(
                        Coverage(
                            dataset=dataset.value,
                            provider=provider,
                            source=source,
                            symbol=symbol,
                            min_ts=min_ts,
                            max_ts=max_ts,
                            row_count=count,
                            last_updated=_naive_utc(now),
                        )
                    )
                else:
                    existing.min_ts = min(existing.min_ts, min_ts)
                    existing.max_ts = max(existing.max_ts, max_ts)
                    existing.row_count += count
                    existing.last_updated = _naive_utc(now)
            session.commit()

    # ----------------------------------------------------------------- reads --

    def read(
        self,
        dataset: Dataset | str,
        *,
        symbols: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
        dedup: str | None = "canonical",
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        return parquet_store.query(
            self.config,
            Dataset(dataset),
            symbols=symbols,
            providers=providers,
            sources=sources,
            start=start,
            end=end,
            as_of=as_of,
            dedup=dedup,
            columns=columns,
            limit=limit,
        )

    def get_latest(
        self, dataset: Dataset | str, symbol: str, *, providers: Sequence[str] | None = None
    ) -> pd.Series | None:
        """The most recent row for a symbol.

        Reads a bounded window at the end of the symbol's coverage rather than
        the symbol's entire history. The naive form -- read everything for the
        symbol, sort, take the last row -- costs time proportional to how much
        history the symbol has, which is the wrong shape for what is usually
        the most latency-sensitive query in the system: measured on the live
        store this took 2.2s for a quotes symbol with ~680k rows, against 55ms
        for a bars symbol with ~1.9k.

        The coverage table (SQL metadata, already indexed) gives max_ts for the
        symbol without touching Parquet. A `start` bound derived from it lets
        DuckDB prune by row-group statistics so only the tail is scanned. The
        window widens if the first attempt finds nothing -- coverage can be
        slightly ahead of the files after a failed write -- and falls back to
        the full scan, so the result is identical either way.
        """
        ds = Dataset(dataset)
        spec = get_spec(ds)

        max_ts = self._coverage_max_ts(ds, symbol, providers)
        if max_ts is not None:
            for days in (1, 7, 90):
                df = self.read(
                    ds,
                    symbols=[symbol],
                    providers=providers,
                    start=max_ts - timedelta(days=days),
                    dedup="canonical",
                )
                if not df.empty:
                    return df.sort_values(spec.time_col).iloc[-1]

        df = self.read(ds, symbols=[symbol], providers=providers, dedup="canonical")
        if df.empty:
            return None
        return df.sort_values(spec.time_col).iloc[-1]

    def _coverage_max_ts(
        self, dataset: Dataset, symbol: str, providers: Sequence[str] | None
    ) -> datetime | None:
        """Newest covered timestamp for a symbol, from metadata only."""
        with self._Session() as session:
            q = session.query(func.max(Coverage.max_ts)).filter(
                Coverage.dataset == dataset.value, Coverage.symbol == symbol
            )
            if providers:
                q = q.filter(Coverage.provider.in_(list(providers)))
            value = q.scalar()
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def point_in_time(
        self,
        dataset: Dataset | str,
        as_of: datetime,
        *,
        symbols: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        dedup: str = "canonical",
    ) -> pd.DataFrame:
        return self.read(dataset, symbols=symbols, providers=providers, as_of=as_of, dedup=dedup)

    # ------------------------------------------------------------- lifecycle --

    def compact(
        self,
        dataset: Dataset | str | None = None,
        *,
        provider: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
        day: date | None = None,
    ) -> int:
        """Merge landing-zone files into deduplicated canonical files. With no
        filters, sweeps every partition of every dataset found in the landing zone.
        """
        datasets = [Dataset(dataset)] if dataset is not None else list(Dataset)
        total = 0
        for ds in datasets:
            if provider and source and symbol and day:
                total += parquet_store.compact_partition(self.config, ds, provider, source, symbol, day)
                continue
            for p, s, sym, d in parquet_store.iter_partitions(self.config, ds):
                if provider and p != provider:
                    continue
                if source and s != source:
                    continue
                if symbol and sym != symbol:
                    continue
                if day and d != day:
                    continue
                total += parquet_store.compact_partition(self.config, ds, p, s, sym, d)
        return total

    def coverage(self, dataset: Dataset | str | None = None, symbol: str | None = None) -> pd.DataFrame:
        with self._Session() as session:
            q = session.query(Coverage)
            if dataset is not None:
                q = q.filter_by(dataset=Dataset(dataset).value)
            if symbol is not None:
                q = q.filter_by(symbol=symbol)
            rows = q.all()
            return pd.DataFrame(
                [
                    {
                        "dataset": r.dataset,
                        "provider": r.provider,
                        "source": r.source,
                        "symbol": r.symbol,
                        "min_ts": r.min_ts.replace(tzinfo=timezone.utc),
                        "max_ts": r.max_ts.replace(tzinfo=timezone.utc),
                        "row_count": r.row_count,
                        "last_updated": r.last_updated,
                    }
                    for r in rows
                ]
            )

    def check_integrity(self, dataset: Dataset | str, **kwargs) -> integrity_mod.DuplicateReport:
        return integrity_mod.find_duplicates(self.config, Dataset(dataset), **kwargs)

    def find_gaps(self, dataset: Dataset | str, symbol: str, *, max_gap_seconds: float, **kwargs):
        return integrity_mod.find_gaps(self.config, Dataset(dataset), symbol, max_gap_seconds=max_gap_seconds, **kwargs)

    def row_count(self, dataset: Dataset | str, *, dedup: str | None = "exact") -> int:
        return integrity_mod.row_count(self.config, Dataset(dataset), dedup=dedup)

    def backup(self, label: str | None = None) -> Path:
        return create_backup(self.config, label=label)

    def restore(self, archive_path: Path, *, overwrite: bool = False) -> None:
        restore_backup(self.config, archive_path, overwrite=overwrite)

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> "MarketDataDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
