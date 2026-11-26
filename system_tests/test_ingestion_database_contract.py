"""Exercise the tracked ingestion wire format against an isolated database."""
from datetime import datetime, timezone
from types import SimpleNamespace

from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset
from ingestion_runtime.live_db_client import _trade_to_record


def test_live_trade_contract_and_idempotent_read(tmp_path):
    payload = SimpleNamespace(
        symbol="AAPL", timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        price=190.5, size=100, exchange="V", trade_id=12345,
        conditions=("@",), tape="C",
    )
    record = _trade_to_record(payload)
    db = MarketDataDB(DBConfig(root=tmp_path / "lake"))
    try:
        db.write(Dataset.TRADES, [record], provider="alpaca", source="live")
        db.write(Dataset.TRADES, [record], provider="alpaca", source="live")
        rows = db.read(Dataset.TRADES, symbols=["AAPL"], dedup="exact")
        assert len(rows) == 1
        assert rows.iloc[0]["price"] == 190.5
    finally:
        db.close()


def test_live_ingestion_wire_payload_through_database_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import app

    monkeypatch.setenv('MARKET_DATA_DB_API_KEYS', 'isolated-test-key')
    monkeypatch.setenv('MARKET_DATA_DB_ROOT', str(tmp_path / 'api-lake'))
    payload = SimpleNamespace(
        symbol='MSFT', timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        price=380.5, size=10, exchange='V', trade_id=67890,
        conditions=(), tape='C',
    )
    record = _trade_to_record(payload)
    with TestClient(app) as client:
        headers = {'X-API-Key': 'isolated-test-key'}
        for _ in range(2):
            response = client.post('/v1/datasets/trades/write', headers=headers, json={
                'provider': 'alpaca', 'source': 'live', 'records': [record],
            })
            assert response.status_code == 200, response.text
        response = client.post('/v1/datasets/trades/read', headers=headers, json={
            'symbols': ['MSFT'], 'dedup': 'exact',
        })
        assert response.status_code == 200, response.text
        rows = response.json()['rows']
        assert len(rows) == 1
        assert rows[0]['price'] == 380.5
