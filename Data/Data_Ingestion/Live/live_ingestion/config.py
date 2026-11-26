"""Configuration for the live market data ingestion engine.

Everything is sourced from environment variables (optionally loaded from a
.env file) so the engine can run standalone without code changes between
deployments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

from dotenv import load_dotenv

VALID_SUBSCRIPTIONS = {
    "trades",
    "quotes",
    "bars",
    "updated_bars",
    "daily_bars",
    "statuses",
}

VALID_FEEDS = {"iex", "sip", "delayed_sip", "otc", "boats", "overnight"}


class ConfigError(ValueError):
    """Raised when the engine is misconfigured."""


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    data_feed: str = "iex"
    symbols: tuple[str, ...] = ()
    subscriptions: FrozenSet[str] = field(default_factory=frozenset)
    ws_data_timeout: Optional[float] = None
    rest_poll_interval_seconds: float = 10.0
    rest_rate_limit_per_minute: int = 200
    health_log_interval_seconds: float = 30.0
    queue_max_size: int = 10_000
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        if not self.symbols:
            raise ConfigError("At least one symbol must be configured (ALPACA_SYMBOLS)")
        if self.data_feed not in VALID_FEEDS:
            raise ConfigError(f"Unsupported data feed '{self.data_feed}', expected one of {VALID_FEEDS}")
        if self.data_feed != "iex":
            raise ConfigError(
                "This engine targets Alpaca's free tier, which only entitles the "
                "'iex' feed. Set ALPACA_DATA_FEED=iex, or if you hold a paid "
                "subscription remove this guard deliberately."
            )
        unknown = self.subscriptions - VALID_SUBSCRIPTIONS
        if unknown:
            raise ConfigError(f"Unknown subscription type(s): {unknown}")
        if not self.subscriptions:
            raise ConfigError("At least one subscription type must be configured (ALPACA_SUBSCRIPTIONS)")
        if self.rest_rate_limit_per_minute <= 0:
            raise ConfigError("ALPACA_REST_RATE_LIMIT_PER_MINUTE must be positive")
        if self.rest_poll_interval_seconds < 0:
            raise ConfigError("ALPACA_REST_POLL_INTERVAL_SECONDS must be >= 0")
        if self.queue_max_size <= 0:
            raise ConfigError("ALPACA_QUEUE_MAX_SIZE must be positive")

    @property
    def rest_polling_enabled(self) -> bool:
        return self.rest_poll_interval_seconds > 0

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)

        def _float_or_none(name: str) -> Optional[float]:
            raw = os.getenv(name, "").strip()
            return float(raw) if raw else None

        return cls(
            alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            data_feed=os.getenv("ALPACA_DATA_FEED", "iex").strip().lower(),
            symbols=tuple(_split_csv(os.getenv("ALPACA_SYMBOLS", ""))),
            subscriptions=frozenset(
                s.lower() for s in _split_csv(os.getenv("ALPACA_SUBSCRIPTIONS", "trades,quotes,bars"))
            ),
            ws_data_timeout=_float_or_none("ALPACA_WS_DATA_TIMEOUT"),
            rest_poll_interval_seconds=float(os.getenv("ALPACA_REST_POLL_INTERVAL_SECONDS", "10")),
            rest_rate_limit_per_minute=int(os.getenv("ALPACA_REST_RATE_LIMIT_PER_MINUTE", "200")),
            health_log_interval_seconds=float(os.getenv("ALPACA_HEALTH_LOG_INTERVAL_SECONDS", "30")),
            queue_max_size=int(os.getenv("ALPACA_QUEUE_MAX_SIZE", "10000")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
