from backfill_engine.engine import _chunk_range, _missing_ranges, _normalize_resolution


def test_normalize_resolution_per_provider():
    assert _normalize_resolution("1Day", "lse") == "1d"
    assert _normalize_resolution("1day", "alpaca") == "1Day"
    assert _normalize_resolution("1Hour", "lse") == "1h"


def test_normalize_resolution_passthrough_for_unknown_token():
    assert _normalize_resolution("3Min", "lse") == "3Min"


def test_missing_ranges_no_existing_coverage():
    assert _missing_ranges(None, "2020-01-01", "2020-02-01") == [("2020-01-01", "2020-02-01")]


def test_missing_ranges_fully_covered():
    existing = ("2020-01-01", "2020-02-01")
    assert _missing_ranges(existing, "2020-01-05", "2020-01-20") == []


def test_missing_ranges_extends_before_and_after():
    existing = ("2020-01-10", "2020-01-20")
    gaps = _missing_ranges(existing, "2020-01-01", "2020-01-31")
    assert gaps[0] == ("2020-01-01", "2020-01-09")
    assert gaps[1][0] >= "2020-01-20"
    assert gaps[1][1] == "2020-01-31"


def test_missing_ranges_extends_after_only():
    existing = ("2020-01-01", "2020-01-20")
    gaps = _missing_ranges(existing, "2020-01-01", "2020-01-31")
    assert len(gaps) == 1
    assert gaps[0][1] == "2020-01-31"


def test_chunk_range_splits_into_requested_day_windows():
    chunks = _chunk_range("2020-01-01", "2020-04-01", chunk_days=30)
    assert chunks[0][0] == "2020-01-01"
    assert chunks[-1][1] == "2020-04-01"
    for s, e in chunks:
        assert s < e


def test_chunk_range_empty_when_start_ge_end():
    assert _chunk_range("2020-01-01", "2020-01-01", chunk_days=30) == []
