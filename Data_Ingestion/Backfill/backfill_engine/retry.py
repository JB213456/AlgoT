"""Retry helper with exponential backoff, honoring Retry-After when present."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def retry_request(
    fn: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 5,
    base_delay: float = 1.0,
    description: str = "request",
) -> httpx.Response:
    attempt = 0
    while True:
        try:
            response = await fn()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning("%s: network error (%s), retry %d/%d in %.1fs", description, exc, attempt, max_retries, delay)
            await asyncio.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS:
            return response

        attempt += 1
        if attempt > max_retries:
            return response

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = base_delay * (2 ** (attempt - 1))
        else:
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)

        logger.warning(
            "%s: status %d, retry %d/%d in %.1fs", description, response.status_code, attempt, max_retries, delay
        )
        await asyncio.sleep(delay)
