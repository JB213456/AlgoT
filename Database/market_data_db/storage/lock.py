from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock


@contextmanager
def partition_lock(partition_dir: Path, timeout: float = 60.0):
    """Exclusive lock for a single partition directory, used only around compaction
    (the merge-rewrite). Plain ingestion writes are append-only and need no lock.
    """
    partition_dir.mkdir(parents=True, exist_ok=True)
    lock_path = partition_dir / ".compaction.lock"
    lock = FileLock(str(lock_path), timeout=timeout)
    with lock:
        yield
