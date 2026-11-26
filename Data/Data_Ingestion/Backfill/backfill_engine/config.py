"""Configuration for the historical market data backfill engine.

Sourced from environment variables (optionally loaded from a .env file), same
convention as the sibling live_ingestion engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "backfill_data" / "market_data.sqlite3")


class ConfigError(ValueError):
    """Raised when the engine is misconfigured."""


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    lse_api_key: str = ""

    db_path: str = DEFAULT_DB_PATH

    alpaca_rate_limit_per_minute: int = 200
    lse_rate_limit_per_minute: int = 60
    lse_max_rows_per_day: int = 12_500_000
    lse_max_bytes_per_week: int = 12 * 1024 * 1024 * 1024  # 12 GiB, conservative vs documented 12.5 GiB
    lse_max_rows_per_export: int = 2_500_000
    lse_max_concurrent_downloads: int = 1

    alpaca_max_concurrent_requests: int = 4
    max_retries: int = 5
    retry_base_delay_seconds: float = 1.0

    log_level: str = "INFO"

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def lse_configured(self) -> bool:
        return bool(self.lse_api_key)

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)
        return cls(
            alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            lse_api_key=os.getenv("LSE_API_KEY", "").strip(),
            db_path=(os.getenv("BACKFILL_DB_PATH") or DEFAULT_DB_PATH).strip(),
            alpaca_rate_limit_per_minute=int(os.getenv("ALPACA_REST_RATE_LIMIT_PER_MINUTE", "200")),
            lse_rate_limit_per_minute=int(os.getenv("LSE_RATE_LIMIT_PER_MINUTE", "60")),
            lse_max_rows_per_day=int(os.getenv("LSE_MAX_ROWS_PER_DAY", "12500000")),
            lse_max_bytes_per_week=int(os.getenv("LSE_MAX_BYTES_PER_WEEK", str(12 * 1024 * 1024 * 1024))),
            lse_max_rows_per_export=int(os.getenv("LSE_MAX_ROWS_PER_EXPORT", "2500000")),
            lse_max_concurrent_downloads=int(os.getenv("LSE_MAX_CONCURRENT_DOWNLOADS", "1")),
            alpaca_max_concurrent_requests=int(os.getenv("ALPACA_MAX_CONCURRENT_REQUESTS", "4")),
            max_retries=int(os.getenv("BACKFILL_MAX_RETRIES", "5")),
            retry_base_delay_seconds=float(os.getenv("BACKFILL_RETRY_BASE_DELAY_SECONDS", "1.0")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
