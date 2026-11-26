import json
import pytest
import respx
from httpx import HTTPStatusError, Response

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
async def test_ingest_many_propagates_database_failure():
    client = MarketDBClient("http://market-data-db:8000", "secret", max_retries=0, retry_base_delay=0.01)
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.get("/v1/storage/status").mock(return_value=Response(200, json={
                "backfill_allowed": True, "free_bytes": 20_000_000_000,
                "minimum_free_bytes": 8_000_000_000,
            }))
            mock.post("/v1/datasets/bars/write").mock(return_value=Response(500, text="boom"))
            bar = Bar(provider="alpaca", feed="iex", symbol="AAPL", resolution="1Day",
                      timestamp="2024-01-01T00:00:00Z", open=1, high=2, low=0.5, close=1.5, volume=100)
            with pytest.raises(HTTPStatusError):
                await client.ingest_many([bar, bar])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_many_stops_before_write_when_storage_low():
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000", assert_all_called=False) as mock:
            mock.get("/v1/storage/status").mock(return_value=Response(200, json={
                "backfill_allowed": False, "free_bytes": 4_000_000_000,
                "minimum_free_bytes": 8_000_000_000,
            }))
            write = mock.post("/v1/datasets/bars/write").mock(return_value=Response(200, json={"rows_written": 1}))
            bar = Bar(provider="alpaca", feed="iex", symbol="AAPL", resolution="1Day",
                      timestamp="2024-01-01T00:00:00Z", open=1, high=2, low=0.5, close=1.5, volume=100)
            with pytest.raises(RuntimeError, match="backfill suspended"):
                await client.ingest_many([bar])
            assert not write.called
    finally:
        await client.close()


def _bar(symbol="AAPL", ts="2024-01-01T00:00:00Z"):
    return Bar(provider="alpaca", feed="iex", symbol=symbol, resolution="1Day",
               timestamp=ts, open=1, high=2, low=0.5, close=1.5, volume=100)


def _storage_ok(mock):
    mock.get("/v1/storage/status").mock(return_value=Response(200, json={
        "backfill_allowed": True, "free_bytes": 20_000_000_000,
        "minimum_free_bytes": 8_000_000_000,
    }))


@pytest.mark.asyncio
async def test_ingest_many_batches_into_one_request():
    """475 bars must not become 475 POSTs."""
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            _storage_ok(mock)
            route = mock.post("/v1/datasets/bars/write").mock(
                side_effect=lambda req: Response(200, json={
                    "rows_written": len(json.loads(req.content)["records"])}))
            bars = [_bar(ts=f"2024-01-01T00:{i // 60:02d}:{i % 60:02d}Z") for i in range(475)]
            rows = await client.ingest_many(bars)
            assert rows == 475, "every row must still be reported written"
            assert len(route.calls) == 1, f"expected 1 batched request, got {len(route.calls)}"
            assert len(json.loads(route.calls.last.request.content)["records"]) == 475
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_many_respects_batch_size_limit():
    client = MarketDBClient("http://market-data-db:8000", "secret", max_batch_rows=100)
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            _storage_ok(mock)
            route = mock.post("/v1/datasets/bars/write").mock(
                side_effect=lambda req: Response(200, json={
                    "rows_written": len(json.loads(req.content)["records"])}))
            rows = await client.ingest_many([_bar() for _ in range(250)])
            assert rows == 250
            # 100 + 100 + 50
            assert len(route.calls) == 3
            sizes = [len(json.loads(c.request.content)["records"]) for c in route.calls]
            assert sizes == [100, 100, 50]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_many_keeps_datasets_and_providers_separate():
    """A mixed stream must not be merged into one call."""
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            _storage_ok(mock)
            bars = mock.post("/v1/datasets/bars/write").mock(
                side_effect=lambda req: Response(200, json={
                    "rows_written": len(json.loads(req.content)["records"])}))
            actions = mock.post("/v1/datasets/corporate_actions/write").mock(
                side_effect=lambda req: Response(200, json={
                    "rows_written": len(json.loads(req.content)["records"])}))
            stream = [
                _bar("AAPL"), _bar("MSFT"),
                CorporateAction(provider="alpaca", feed="iex", symbol="AAPL",
                                action_type="split", ex_date="2024-01-02", ratio=2.0),
                _bar("TSLA"),
            ]
            rows = await client.ingest_many(stream)
            assert rows == 4
            # One batched call per (dataset, provider), not per record.
            assert len(bars.calls) == 1
            assert len(actions.calls) == 1
            assert len(json.loads(bars.calls.last.request.content)["records"]) == 3
            assert len(json.loads(actions.calls.last.request.content)["records"]) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_record_still_writes_single_record():
    """Batching must not change the single-record path."""
    client = MarketDBClient("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            route = mock.post("/v1/datasets/bars/write").mock(return_value=Response(200, json={"rows_written": 1}))
            assert await client.ingest_record(_bar()) == 1
            assert len(route.calls) == 1
            assert len(json.loads(route.calls.last.request.content)["records"]) == 1
    finally:
        await client.close()
