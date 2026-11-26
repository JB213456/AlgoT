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
