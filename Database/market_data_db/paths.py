from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import DBConfig
from .schema import Dataset

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize(value: str) -> str:
    """Make a value safe to use as a single path component (e.g. 'BTC/USD' -> 'BTC_USD')."""
    return _UNSAFE.sub("_", str(value))


def partition_dir(
    zone_root: Path,
    dataset: Dataset,
    provider: str,
    source: str,
    symbol: str,
    day: date,
) -> Path:
    return (
        zone_root
        / f"dataset={dataset.value}"
        / f"provider={sanitize(provider)}"
        / f"source={sanitize(source)}"
        / f"symbol={sanitize(symbol)}"
        / f"date={day.isoformat()}"
    )


def dataset_glob(zone_root: Path, dataset: Dataset) -> str:
    return str(zone_root / f"dataset={dataset.value}" / "**" / "*.parquet")


def archive_root(config: DBConfig) -> Path:
    return config.root / "archive"
