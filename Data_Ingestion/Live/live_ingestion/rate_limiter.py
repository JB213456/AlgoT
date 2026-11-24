"""Async token-bucket rate limiter for Alpaca's free-tier REST API limits."""
from __future__ import annotations

import asyncio
import time


class AsyncTokenBucket:
    def __init__(self, rate_per_minute: int) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._capacity = float(rate_per_minute)
        self._tokens = float(rate_per_minute)
        self._refill_per_second = rate_per_minute / 60.0
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
            self._updated_at = now

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self._refill_per_second)

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens
