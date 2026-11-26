from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DBConfig:
    """Configuration for the market data database.

    root: filesystem root for the Parquet data lake (landing + canonical zones).
    metadata_url: SQLAlchemy URL for operational metadata (ingestion runs, coverage,
        schema migrations, dedup bookkeeping). Defaults to a SQLite file under root.
        Pass a postgresql:// URL to use Postgres instead.
    """

    root: Path = field(default_factory=lambda: Path(os.environ.get("MARKET_DATA_DB_ROOT", "./data")))
    metadata_url: str | None = None
    compression: str = "zstd"
    row_group_size: int = 128_000
    # DuckDB query memory budget and thread count, applied to every connection.
    #
    # DuckDB sizes its own budget from the *host's* RAM, which inside a
    # container is the node's RAM, not the cgroup limit -- so it will cheerfully
    # allocate past the container limit and get the process OOM-killed instead
    # of spilling to disk. Observed on the live deployment: a handful of
    # concurrent dedup reads over a 2M-row dataset killed the database five
    # times (exit 137, OOMKilled) against a 1536Mi limit.
    #
    # A dedup read is a ROW_NUMBER() window over the whole match set, so the
    # LIMIT cannot prune it and the peak is driven by dataset size and
    # concurrency rather than result size. With an explicit budget DuckDB
    # spills to temp_directory and the query gets slower instead of the pod
    # dying -- which is the right trade for a single-replica store.
    duckdb_memory_limit: str = field(
        default_factory=lambda: os.environ.get("MARKET_DATA_DB_DUCKDB_MEMORY_LIMIT", "512MB")
    )
    duckdb_threads: int = field(
        default_factory=lambda: int(os.environ.get("MARKET_DATA_DB_DUCKDB_THREADS", "2"))
    )

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        if self.metadata_url is None:
            meta_path = self.root / "metadata" / "metadata.sqlite"
            self.metadata_url = f"sqlite:///{meta_path}"

    @property
    def landing_root(self) -> Path:
        return self.root / "landing"

    @property
    def canonical_root(self) -> Path:
        return self.root / "canonical"

    @property
    def backup_root(self) -> Path:
        return self.root / "backups"

    def ensure_dirs(self) -> None:
        for p in (self.root, self.landing_root, self.canonical_root, self.backup_root, self.root / "metadata"):
            p.mkdir(parents=True, exist_ok=True)


def connect_duckdb(config: "DBConfig"):
    """A DuckDB connection with this deployment's memory and thread budget.

    Every query path must go through here rather than calling duckdb.connect()
    directly: a bare connection inherits DuckDB's host-RAM-derived default
    budget, which ignores the container's cgroup limit (see DBConfig).
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit='{config.duckdb_memory_limit}'")
        con.execute(f"SET threads={int(config.duckdb_threads)}")
        # Spill target for anything that does not fit the budget.
        tmp = config.root / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{tmp}'")
    except Exception:
        con.close()
        raise
    return con
