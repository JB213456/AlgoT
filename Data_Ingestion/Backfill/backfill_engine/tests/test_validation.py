from backfill_engine.models import Bar
from backfill_engine.validation import filter_valid_bars, is_valid_bar


def _bar(**overrides):
    base = dict(provider="alpaca", feed="iex", symbol="AAPL", resolution="1Day",
                timestamp="2024-01-01T00:00:00Z", open=10, high=12, low=9, close=11, volume=1000)
    base.update(overrides)
    return Bar(**base)


def test_valid_bar_passes():
    assert is_valid_bar(_bar())


def test_negative_price_rejected():
    assert not is_valid_bar(_bar(open=-1))


def test_negative_volume_rejected():
    assert not is_valid_bar(_bar(volume=-1))


def test_inconsistent_ohlc_rejected():
    assert not is_valid_bar(_bar(high=5))  # high below open/close


def test_filter_valid_bars_drops_only_bad_ones():
    bars = [_bar(), _bar(open=-1), _bar(timestamp="2024-01-02T00:00:00Z")]
    result = filter_valid_bars(bars)
    assert len(result) == 2
