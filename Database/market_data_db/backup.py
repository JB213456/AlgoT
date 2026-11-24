from __future__ import annotations

import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .config import DBConfig


def create_backup(config: DBConfig, label: str | None = None) -> Path:
    """Snapshot the whole data lake (canonical + landing + archive) and the
    operational metadata DB into one tar.zst archive under config.backup_root.
    Uses a consistent point-in-time copy by archiving directly (no live DB lock
    needed for the Parquet files, since they are immutable once written); the
    SQLite metadata file is copied via its own backup API for a consistent copy.
    """
    config.ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"backup-{ts}" + (f"-{label}" if label else "")
    archive_path = config.backup_root / f"{name}.tar.gz"

    snapshot_path: Path | None = None
    if config.metadata_url.startswith("sqlite"):
        import sqlite3

        db_file = config.metadata_url.replace("sqlite:///", "", 1)
        snapshot_path = config.root / "metadata" / f".backup_snapshot_{ts}.sqlite"
        src = sqlite3.connect(db_file)
        try:
            dst = sqlite3.connect(str(snapshot_path))
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()

    with tarfile.open(archive_path, mode="w:gz") as tar:
        for sub in ("canonical", "landing", "archive"):
            p = config.root / sub
            if p.exists():
                tar.add(p, arcname=sub)
        if snapshot_path is not None:
            tar.add(snapshot_path, arcname="metadata/metadata.sqlite")
            snapshot_path.unlink()

    return archive_path


def restore_backup(config: DBConfig, archive_path: Path, *, overwrite: bool = False) -> None:
    """Restore a backup created by create_backup() into config.root. Refuses to
    clobber an existing non-empty data lake unless overwrite=True."""
    config.ensure_dirs()
    if not overwrite:
        existing = list(config.canonical_root.rglob("*.parquet")) + list(config.landing_root.rglob("*.parquet"))
        if existing:
            raise RuntimeError(
                f"{config.root} already contains data; pass overwrite=True to restore anyway"
            )

    for sub in ("canonical", "landing", "archive"):
        p = config.root / sub
        if p.exists():
            shutil.rmtree(p)

    with tarfile.open(archive_path, mode="r:gz") as tar:
        tar.extractall(config.root, filter="data")

    restored_meta = config.root / "metadata" / "metadata.sqlite"
    if restored_meta.exists() and config.metadata_url.startswith("sqlite"):
        target = Path(config.metadata_url.replace("sqlite:///", "", 1))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(restored_meta), str(target))
