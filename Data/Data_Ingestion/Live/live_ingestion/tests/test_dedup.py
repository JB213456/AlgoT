from live_ingestion.dedup import Deduplicator


def test_first_seen_not_duplicate():
    d = Deduplicator(maxlen=10)
    assert d.is_duplicate("bucket", "k1") is False


def test_repeat_is_duplicate():
    d = Deduplicator(maxlen=10)
    d.is_duplicate("bucket", "k1")
    assert d.is_duplicate("bucket", "k1") is True


def test_different_buckets_independent():
    d = Deduplicator(maxlen=10)
    d.is_duplicate("a", "k1")
    assert d.is_duplicate("b", "k1") is False


def test_eviction_allows_resurfacing_key():
    d = Deduplicator(maxlen=2)
    assert d.is_duplicate("bucket", "k1") is False
    assert d.is_duplicate("bucket", "k2") is False
    assert d.is_duplicate("bucket", "k3") is False  # evicts k1
    assert d.is_duplicate("bucket", "k1") is False  # k1 resurfaces as "new"
