import pytest
import respx
from httpx import Response

from backfill_engine.providers.base import DatasetUnavailable
from backfill_engine.providers.lse import LSEProvider

CSV_BODY = "time,open,high,low,close,volume,trades\n2024-01-01,1,2,0.5,1.5,100,5\n"


@pytest.mark.asyncio
async def test_fetch_bars_parses_csv_export():
    provider = LSEProvider("key", rate_limit_per_minute=1000, max_rows_per_day=10**9,
                            max_bytes_per_week=10**12)
    try:
        with respx.mock(base_url="https://download.londonstrategicedge.com") as mock:
            mock.get("/v3/export").mock(return_value=Response(
                200, text=CSV_BODY, headers={"x-export-rows": "1"},
            ))
            pages = []
            async for page in provider.fetch_bars("AAPL", "1d", "2024-01-01", "2024-01-02"):
                pages.append(page)
            assert len(pages) == 1
            bar = pages[0][0]
            assert bar.close == 1.5
            assert bar.provider == "lse"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_data_unavailable_raises_dataset_unavailable():
    provider = LSEProvider("key", rate_limit_per_minute=1000, max_rows_per_day=10**9,
                            max_bytes_per_week=10**12)
    try:
        with respx.mock(base_url="https://download.londonstrategicedge.com") as mock:
            mock.get("/v3/export").mock(return_value=Response(400, json={"code": "data-unavailable"}))
            with pytest.raises(DatasetUnavailable):
                await provider.fetch_trades("AAPL", "2024-01-01", "2024-01-02").__anext__()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_single_connection_semaphore_serializes_exports():
    provider = LSEProvider("key", rate_limit_per_minute=1000, max_rows_per_day=10**9,
                            max_bytes_per_week=10**12, max_concurrent_downloads=1)
    try:
        assert provider._download_semaphore._value == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_fetch_option_chain_parses_rows_shape():
    provider = LSEProvider("key", rate_limit_per_minute=1000, max_rows_per_day=10**9,
                            max_bytes_per_week=10**12)
    try:
        with respx.mock(base_url="https://download.londonstrategicedge.com") as mock:
            mock.get("/v3/options/chain").mock(return_value=Response(200, json={
                "contracts": 1, "greeks_as_of": "2024-01-01 00:00:00", "limit": 1000, "offset": 0, "returned": 1,
                "rows": [{
                    "contract_type": "call", "delta": 0.5, "expiry": "2024-02-01", "gamma": 0.01,
                    "greeks_at": "2024-01-01 00:00:00", "iv": 0.3, "last_price": 5.0, "strike": 150.0,
                    "ticker": "AAPL  240201C00150000", "theta": -0.1, "underlying": "AAPL", "vega": 0.05,
                    "volume_today": 10,
                }],
            }))
            pages = []
            async for page in provider.fetch_option_chain("AAPL"):
                pages.append(page)
            assert len(pages) == 1
            contract = pages[0][0]
            assert contract.contract_symbol == "AAPL  240201C00150000"
            assert contract.option_type == "call"
            assert contract.implied_volatility == 0.3
            assert contract.last == 5.0
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_crypto_symbol_mapped_to_market_param():
    provider = LSEProvider("key", rate_limit_per_minute=1000, max_rows_per_day=10**9,
                            max_bytes_per_week=10**12)
    try:
        with respx.mock(base_url="https://download.londonstrategicedge.com") as mock:
            route = mock.get("/v3/export").mock(return_value=Response(
                200, text=CSV_BODY, headers={"x-export-rows": "1"},
            ))
            # The engine resolves the market from the canonical symbol and
            # passes it explicitly, since the provider-mapped symbol
            # ("BTCUSDT") no longer carries the crypto marker.
            async for _ in provider.fetch_bars("BTCUSDT", "1d", "2024-01-01", "2024-01-02", feed="crypto"):
                pass
            assert route.calls.last.request.url.params["market"] == "crypto"
    finally:
        await provider.close()
