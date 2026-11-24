import pytest
import respx
from httpx import Response

from backfill_engine.market_db_client import MarketDBClient, build_market_db_client
from backfill_engine.models import Bar, CorporateAction, FundamentalRecord, OptionContract


def test_build_returns_none_without_url(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_DB_URL", raising=False)
    assert build_market_db_client() is None


def test_build_returns_none_without_api_key(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_DB_URL", "http://market-data-db:8000")
    monkeypatch.delenv("MARKET_DATA_DB_API_KEY", raising=False)
    assert build_market_db_client() is None


def test_build_returns_client_when_configured(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_DB_URL", "http://market-data-db:8000")
    monkeypatch.setenv("MARKET_DATA_DB_API_KEY", "secret")
    client = build_market_db_client()
    assert isinstance(client, MarketDBClient)


@pytest.mark.asyncio
async def test_ingest_bar_posts_to_bars_endpoint():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            route = mock.post("/v1/datasets/bars/write").mock(return_value=Response(200, json={"rows_written": 1}))
            bar = Bar(provider="alpaca", feed="iex", symbol="AAPL", resolution="1Day",
                      timestamp="2024-01-01T00:00:00Z", open=1, high=2, low=0.5, close=1.5, volume=100)
            rows = await client.ingest_record(bar)
            assert rows == 1
            body = route.calls.last.request.content
            assert b"AAPL" in body
            assert route.calls.last.request.headers["X-API-Key"] == "secret"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_option_contract_writes_quote_and_greeks():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.post("/v1/datasets/options_quotes/write").mock(return_value=Response(200, json={"rows_written": 1}))
            mock.post("/v1/datasets/options_greeks/write").mock(return_value=Response(200, json={"rows_written": 1}))
            contract = OptionContract(provider="lse", feed="indicative", underlying="AAPL",
                                        contract_symbol="AAPL  240201C00150000", expiry="2024-02-01",
                                        strike=150.0, option_type="call", as_of="2024-01-01T00:00:00Z",
                                        delta=0.5)
            rows = await client.ingest_record(contract)
            assert rows == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_option_contract_without_greeks_skips_greeks_write():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            route = mock.post("/v1/datasets/options_quotes/write").mock(
                return_value=Response(200, json={"rows_written": 1}))
            contract = OptionContract(provider="lse", feed="indicative", underlying="AAPL",
                                        contract_symbol="AAPL  240201C00150000", expiry="2024-02-01",
                                        strike=150.0, option_type="call", as_of="2024-01-01T00:00:00Z")
            rows = await client.ingest_record(contract)
            assert rows == 1
            assert route.call_count == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_fundamental_profile_filters_numeric_fields():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            route = mock.post("/v1/datasets/fundamentals/write").mock(
                return_value=Response(200, json={"rows_written": 1}))
            import json
            rec = FundamentalRecord(provider="lse", feed="reference", symbol="AAPL", as_of="2024-01-01T00:00:00Z",
                                      statement_type="profile", period="latest",
                                      fields_json=json.dumps({"market_cap": 100.0, "name": "Apple"}))
            rows = await client.ingest_record(rec)
            assert rows == 1
            body = route.calls.last.request.content
            assert b"market_cap" in body
            assert b"\"name\"" not in body  # non-numeric fields are dropped, matching the DB adapter
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_corporate_action():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.post("/v1/datasets/corporate_actions/write").mock(
                return_value=Response(200, json={"rows_written": 1}))
            action = CorporateAction(provider="lse", feed="us_stocks", symbol="AAPL", action_type="split",
                                       ex_date="2024-06-10", ratio=4.0)
            rows = await client.ingest_record(action)
            assert rows == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_many_continues_past_individual_failures():
    client = MarketDBClient("http://market-data-db:8000", "secret", max_retries=0, retry_base_delay=0.01)
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.post("/v1/datasets/bars/write").mock(return_value=Response(500, text="boom"))
            bar = Bar(provider="alpaca", feed="iex", symbol="AAPL", resolution="1Day",
                      timestamp="2024-01-01T00:00:00Z", open=1, high=2, low=0.5, close=1.5, volume=100)
            total = await client.ingest_many([bar, bar])
            assert total == 0  # both failed, but no exception propagated
    finally:
        await client.close()
