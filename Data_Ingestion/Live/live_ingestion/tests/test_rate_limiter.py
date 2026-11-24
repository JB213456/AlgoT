import asyncio

import pytest

from live_ingestion.rate_limiter import AsyncTokenBucket


@pytest.mark.asyncio
async def test_acquire_consumes_token():
    bucket = AsyncTokenBucket(rate_per_minute=60)
    before = bucket.available
    await bucket.acquire()
    assert bucket.available == pytest.approx(before - 1, abs=0.5)


@pytest.mark.asyncio
async def test_acquire_blocks_when_exhausted():
    bucket = AsyncTokenBucket(rate_per_minute=60)  # 1 token/sec refill
    for _ in range(60):
        await bucket.acquire()
    assert bucket.available < 1

    loop = asyncio.get_running_loop()
    start = loop.time()
    await bucket.acquire()
    elapsed = loop.time() - start
    assert elapsed > 0.3  # had to wait for refill
