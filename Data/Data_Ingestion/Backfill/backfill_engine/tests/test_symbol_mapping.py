from backfill_engine.symbol_mapping import classify, to_alpaca_symbol, to_lse_symbol


def test_classify_equity():
    k = classify("aapl")
    assert not k.is_crypto
    assert k.base == "AAPL"


def test_classify_crypto():
    k = classify("BTC/USD")
    assert k.is_crypto
    assert k.base == "BTC"
    assert k.quote == "USD"


def test_alpaca_symbol_equity():
    assert to_alpaca_symbol("aapl") == "AAPL"


def test_alpaca_symbol_crypto():
    assert to_alpaca_symbol("BTC/USD") == "BTC/USD"


def test_lse_symbol_equity():
    assert to_lse_symbol("aapl") == "AAPL"


def test_lse_symbol_crypto_usd_maps_to_usdt():
    assert to_lse_symbol("BTC/USD") == "BTCUSDT"


def test_lse_symbol_crypto_non_usd_quote_untouched():
    assert to_lse_symbol("ETH/BTC") == "ETHBTC"
