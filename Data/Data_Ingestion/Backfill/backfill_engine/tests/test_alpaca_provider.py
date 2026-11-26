import pytest
import respx
from httpx import Response

from backfill_engine.providers.alpaca import AlpacaProvider
from backfill_engine.providers.base import DatasetUnavailable


@pytest.mark.asyncio
async def test_fetch_bars_single_page():
    provider = AlpacaProvider("key", "secret", rate_limit_per_minute=1000)
    try:
        with respx.mock(base_url="https://data.alpaca.markets") as mock:
            mock.get("/v2/stocks/bars").mock(return_value=Response(200, json={
                "bars": {"AAPL": [
                    {"t": "2024-01-02T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "n": 10, "vw": 1.2},
                ]},
                "next_page_token": None,
            }))
            pages = []
            async for page in provider.fetch_bars("AAPL", "1Day", "2024-01-01", "2024-01-03"):
                pages.append(page)
            assert len(pages) == 1
            assert pages[0][0].close == 1.5
            assert pages[0][0].provider == "alpaca"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_fetch_bars_paginates():
    provider = AlpacaProvider("key", "secret", rate_limit_per_minute=1000)
    try:
        with respx.mock(base_url="https://data.alpaca.markets") as mock:
            route = mock.get("/v2/stocks/bars")
            route.side_effect = [
                Response(200, json={"bars": {"AAPL": [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
                                     "next_page_token": "abc"}),
                Response(200, json={"bars": {"AAPL": [{"t": "2024-01-02T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
                                     "next_page_token": None}),
            ]
            pages = []
            async for page in provider.fetch_bars("AAPL", "1Day", "2024-01-01", "2024-01-03"):
                pages.append(page)
            assert len(pages) == 2
            assert route.call_count == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_forbidden_raises_dataset_unavailable():
    provider = AlpacaProvider("key", "secret", rate_limit_per_minute=1000)
    try:
        with respx.mock(base_url="https://data.alpaca.markets") as mock:
            mock.get("/v2/stocks/bars").mock(return_value=Response(403, text="forbidden"))
            with pytest.raises(DatasetUnavailable):
                async for _ in provider.fetch_bars("AAPL", "1Day", "2024-01-01", "2024-01-03"):
                    pass
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_crypto_symbol_uses_crypto_endpoint():
    provider = AlpacaProvider("key", "secret", rate_limit_per_minute=1000)
    try:
        with respx.mock(base_url="https://data.alpaca.markets") as mock:
            mock.get("/v1beta3/crypto/us/bars").mock(return_value=Response(200, json={
                "bars": {"BTC/USD": [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
                "next_page_token": None,
            }))
            pages = []
            async for page in provider.fetch_bars("BTC/USD", "1Day", "2024-01-01", "2024-01-03"):
                pages.append(page)
            assert len(pages) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_400_degrades_dataset_instead_of_aborting_run():
    """A 400 must surface as DatasetUnavailable so one dataset cannot kill the run."""
    provider = AlpacaProvider(api_key="k", secret_key="s", max_retries=0, retry_base_delay=0.01)
    try:
        with respx.mock(base_url="https://data.alpaca.markets") as mock:
            mock.get("/v1beta1/options/bars").mock(
                return_value=Response(400, text='{"message":"invalid symbol"}'))
            with pytest.raises(DatasetUnavailable) as exc:
                async for _ in provider.fetch_option_bars("AAPL  261127C00110000", "1Day",
                                                          "2026-11-10", "2026-11-14"):
                    pass
            assert "400" in str(exc.value)
            assert "invalid symbol" in str(exc.value)
    finally:
        await provider.close()
