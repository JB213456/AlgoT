"""Bounded-memory deduplication of market data records.

Websocket feeds can redeliver messages after a reconnect, and REST polling
can legitimately re-observe the same trade/quote/bar the websocket already
delivered. This keeps a bounded recent-history set per bucket (e.g. per
symbol) so duplicates are dropped without unbounded memory growth.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, Hashable, Set


class Deduplicator:
    def __init__(self, maxlen: int = 5000) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._maxlen = maxlen
        self._order: Dict[str, Deque[Hashable]] = defaultdict(lambda: deque(maxlen=maxlen))
        self._seen: Dict[str, Set[Hashable]] = defaultdict(set)

    def is_duplicate(self, bucket: str, key: Hashable) -> bool:
        """Returns True if key was already seen for this bucket, else records it."""
        order = self._order[bucket]
        seen = self._seen[bucket]
        if key in seen:
            return True
        if len(order) == order.maxlen:
            evicted = order.popleft()
            seen.discard(evicted)
        order.append(key)
        seen.add(key)
        return False

    def size(self, bucket: str) -> int:
        return len(self._seen.get(bucket, ()))
