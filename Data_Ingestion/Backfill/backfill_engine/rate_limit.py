"""Async token-bucket rate limiter for free-tier REST quota management."""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.capacity = burst if burst is not None else max(1, rate_per_minute)
        self.tokens = float(self.capacity)
        self.refill_per_second = rate_per_minute / 60.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
                if self.tokens >= n:
                    self.tokens -= n
                    return
                deficit = n - self.tokens
                wait = deficit / self.refill_per_second if self.refill_per_second > 0 else 1.0
                await asyncio.sleep(wait)


class QuotaExceeded(RuntimeError):
    """Raised when a hard free-tier quota (rows/day, bytes/week) would be exceeded."""


class RollingQuota:
    """Tracks cumulative usage (rows or bytes) within a sliding window."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    async def consume(self, amount: int) -> None:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds
            self._events = [(t, a) for t, a in self._events if t >= cutoff]
            used = sum(a for _, a in self._events)
            if used + amount > self.limit:
                raise QuotaExceeded(
                    f"would exceed quota of {self.limit} within {self.window_seconds}s "
                    f"(used={used}, requested={amount})"
                )
            self._events.append((now, amount))

    def used(self) -> int:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return sum(a for t, a in self._events if t >= cutoff)
