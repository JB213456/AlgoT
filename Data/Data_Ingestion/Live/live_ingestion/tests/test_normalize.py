import pytest

from live_ingestion.normalize import ValidationError, normalize_bar, normalize_quote, normalize_trade
from .conftest import make_raw_bar, make_raw_quote, make_raw_trade


def test_normalize_trade_ok():
    trade = normalize_trade(make_raw_trade())
    assert trade.symbol == "AAPL"
    assert trade.price == 100.0
    assert trade.timestamp.tzinfo is not None


def test_normalize_trade_rejects_nonpositive_price():
    with pytest.raises(ValidationError):
        normalize_trade(make_raw_trade(price=0))


def test_normalize_trade_rejects_negative_size():
    with pytest.raises(ValidationError):
        normalize_trade(make_raw_trade(size=-5))


def test_normalize_quote_ok():
    quote = normalize_quote(make_raw_quote())
    assert quote.bid_price == 99.0
    assert quote.ask_price == 100.0


def test_normalize_quote_rejects_negative_price():
    with pytest.raises(ValidationError):
        normalize_quote(make_raw_quote(bid=-1))


def test_normalize_bar_ok():
    bar = normalize_bar(make_raw_bar())
    assert bar.open == 100.0
    assert bar.volume == 1000


def test_normalize_bar_rejects_inconsistent_ohlc():
    with pytest.raises(ValidationError):
        normalize_bar(make_raw_bar(h=50.0))  # high below open/close


def test_normalize_bar_rejects_negative_volume():
    with pytest.raises(ValidationError):
        normalize_bar(make_raw_bar(v=-1))


def test_naive_timestamp_assumed_utc():
    import datetime as dt

    naive = dt.datetime(2024, 1, 1, 12, 0, 0)
    trade = normalize_trade(make_raw_trade(ts=naive))
    assert trade.timestamp.tzinfo is not None
    assert trade.timestamp.utcoffset() == dt.timedelta(0)
