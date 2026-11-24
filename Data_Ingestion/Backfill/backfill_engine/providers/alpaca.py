"""Alpaca free-tier (IEX feed) historical data provider.

Covers stocks, crypto, and options bars via the documented Market Data API
(https://data.alpaca.markets). Free ("Basic") plan constraints applied here:

- Stocks must use feed=iex (sip/otc/boats require a paid subscription).
- Options bars are available (historical since Feb 2024); live option chain
  snapshots and trades/quotes beyond the latest 15 minutes are not available
  on Basic, so this provider does not implement option chain discovery --
  that's covered by the LSE provider instead.
- No fundamentals / corporate-actions endpoints are documented for Alpaca's
  market data API, so those methods raise DatasetUnavailable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional, Sequence

import httpx

from ..rate_limit import TokenBucket
from ..retry import retry_request
from ..models import Bar, CorporateAction, FundamentalRecord, OptionBar, Quote, Trade
from ..symbol_mapping import classify
from .base import DatasetUnavailable, Provider

logger = logging.getLogger(__name__)

BASE_URL = "https://data.alpaca.markets"


class AlpacaProvider(Provider):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, rate_limit_per_minute: int = 200,
                 max_concurrent: int = 4, max_retries: int = 5, retry_base_delay: float = 1.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
            timeout=30.0,
        )
        self._bucket = TokenBucket(rate_limit_per_minute)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        await self._bucket.acquire()
        async with self._semaphore:
            response = await retry_request(
                lambda: self._client.get(path, params={k: v for k, v in params.items() if v is not None}),
                max_retries=self._max_retries,
                base_delay=self._retry_base_delay,
                description=f"alpaca GET {path}",
            )
        if response.status_code == 403:
            raise DatasetUnavailable(f"alpaca {path}: 403 forbidden on free tier ({response.text[:200]})")
        if response.status_code == 404:
            raise DatasetUnavailable(f"alpaca {path}: 404 not found")
        response.raise_for_status()
        return response.json()

    async def discover(self, symbol: str) -> dict:
        kind = classify(symbol)
        return {
            "provider": self.name,
            "symbol": symbol,
            "datasets": ["bars"] + ([] if kind.is_crypto else ["option_bars"]),
            "note": "fundamentals/corporate_actions/option_chain not available on Alpaca free tier",
        }

    def _bars_path_and_params(self, symbol: str, resolution: str, start: str, end: str,
                                feed: Optional[str]) -> tuple[str, dict, str]:
        kind = classify(symbol)
        if kind.is_crypto:
            path = "/v1beta3/crypto/us/bars"
            params = {"symbols": symbol, "timeframe": resolution, "start": start, "end": end, "limit": 10000}
            return path, params, "crypto_us"
        path = "/v2/stocks/bars"
        params = {
            "symbols": symbol,
            "timeframe": resolution,
            "start": start,
            "end": end,
            "limit": 10000,
            "adjustment": "raw",
            "feed": feed or "iex",
        }
        return path, params, feed or "iex"

    async def fetch_bars(self, symbol: str, resolution: str, start: str, end: str,
                           feed: Optional[str] = None) -> AsyncIterator[Sequence[Bar]]:
        path, params, feed_label = self._bars_path_and_params(symbol, resolution, start, end, feed)
        page_token = None
        while True:
            params["page_token"] = page_token
            data = await self._get(path, params)
            bars_by_symbol = data.get("bars", {})
            raw_bars = bars_by_symbol.get(symbol, []) if isinstance(bars_by_symbol, dict) else (bars_by_symbol or [])
            page: list[Bar] = []
            for row in raw_bars:
                page.append(Bar(
                    provider=self.name,
                    feed=feed_label,
                    symbol=symbol,
                    resolution=resolution,
                    timestamp=row["t"],
                    open=row["o"], high=row["h"], low=row["l"], close=row["c"], volume=row.get("v", 0),
                    trade_count=row.get("n"),
                    vwap=row.get("vw"),
                    adjustment=params.get("adjustment", "raw"),
                ))
            if page:
                yield page
            page_token = data.get("next_page_token")
            if not page_token:
                break

    async def fetch_option_bars(self, contract_symbol: str, resolution: str, start: str,
                                  end: str) -> AsyncIterator[Sequence[OptionBar]]:
        path = "/v1beta1/options/bars"
        params = {"symbols": contract_symbol, "timeframe": resolution, "start": start, "end": end, "limit": 10000,
                   "page_token": None}
        page_token = None
        while True:
            params["page_token"] = page_token
            data = await self._get(path, params)
            bars_by_symbol = data.get("bars", {})
            raw_bars = bars_by_symbol.get(contract_symbol, []) if isinstance(bars_by_symbol, dict) else []
            page = [
                OptionBar(
                    provider=self.name, feed="indicative", contract_symbol=contract_symbol, resolution=resolution,
                    timestamp=row["t"], open=row["o"], high=row["h"], low=row["l"], close=row["c"],
                    volume=row.get("v", 0),
                )
                for row in raw_bars
            ]
            if page:
                yield page
            page_token = data.get("next_page_token")
            if not page_token:
                break

    def fetch_trades(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Trade]]:
        raise DatasetUnavailable("alpaca historical trades endpoint not implemented in this engine")

    def fetch_quotes(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Quote]]:
        raise DatasetUnavailable("alpaca historical quotes endpoint not implemented in this engine")

    def fetch_fundamentals(self, symbol: str) -> AsyncIterator[Sequence[FundamentalRecord]]:
        raise DatasetUnavailable("alpaca does not expose fundamentals on the free market data API")

    def fetch_corporate_actions(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[CorporateAction]]:
        raise DatasetUnavailable("alpaca does not expose corporate actions on the free market data API")
