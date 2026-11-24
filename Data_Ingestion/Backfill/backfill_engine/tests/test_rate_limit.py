import asyncio

import pytest

from backfill_engine.rate_limit import QuotaExceeded, RollingQuota, TokenBucket


@pytest.mark.asyncio
async def test_token_bucket_allows_burst_up_to_capacity():
    bucket = TokenBucket(rate_per_minute=60, burst=5)
    for _ in range(5):
        await asyncio.wait_for(bucket.acquire(), timeout=0.1)


@pytest.mark.asyncio
async def test_token_bucket_blocks_past_capacity():
    bucket = TokenBucket(rate_per_minute=60, burst=1)
    await bucket.acquire()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bucket.acquire(), timeout=0.05)


@pytest.mark.asyncio
async def test_rolling_quota_allows_within_limit():
    quota = RollingQuota(limit=100, window_seconds=60)
    await quota.consume(50)
    await quota.consume(50)
    assert quota.used() == 100


@pytest.mark.asyncio
async def test_rolling_quota_rejects_over_limit():
    quota = RollingQuota(limit=100, window_seconds=60)
    await quota.consume(90)
    with pytest.raises(QuotaExceeded):
        await quota.consume(20)
