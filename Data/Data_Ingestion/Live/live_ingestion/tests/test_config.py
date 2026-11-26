import pytest

from live_ingestion.config import ConfigError, Settings


def _kwargs(**overrides):
    base = dict(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        data_feed="iex",
        symbols=("AAPL",),
        subscriptions=frozenset({"trades"}),
    )
    base.update(overrides)
    return base


def test_valid_settings():
    settings = Settings(**_kwargs())
    assert settings.rest_polling_enabled is True


def test_missing_api_key_raises():
    with pytest.raises(ConfigError):
        Settings(**_kwargs(alpaca_api_key=""))


def test_no_symbols_raises():
    with pytest.raises(ConfigError):
        Settings(**_kwargs(symbols=()))


def test_non_iex_feed_rejected():
    with pytest.raises(ConfigError):
        Settings(**_kwargs(data_feed="sip"))


def test_unknown_subscription_rejected():
    with pytest.raises(ConfigError):
        Settings(**_kwargs(subscriptions=frozenset({"bogus"})))


def test_zero_poll_interval_disables_rest():
    settings = Settings(**_kwargs(rest_poll_interval_seconds=0))
    assert settings.rest_polling_enabled is False


def test_from_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_DATA_FEED", "iex")
    monkeypatch.setenv("ALPACA_SYMBOLS", "AAPL,MSFT")
    monkeypatch.setenv("ALPACA_SUBSCRIPTIONS", "trades,quotes")
    monkeypatch.delenv("ALPACA_WS_DATA_TIMEOUT", raising=False)

    settings = Settings.from_env(env_file="/nonexistent/.env")
    assert settings.symbols == ("AAPL", "MSFT")
    assert settings.subscriptions == frozenset({"trades", "quotes"})
