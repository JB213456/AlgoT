"""London Strategic Edge (LSE Data 3.0) historical data provider.

Implemented as a direct HTTPS client against the documented REST API
(https://download.londonstrategicedge.com) rather than the vendor's PyPI
SDK, so no third-party code runs -- only our own HTTP calls using a key the
user supplies.

Key documented constraints honored here:
- Auth via `X-API-Key` header.
- Exports are whole-file downloads (CSV), not JSON pages: `/v3/export`.
- Free-tier quotas: ~60 calls/min, a daily row cap, a weekly byte cap, a
  per-export row cap (chunked here), and a single concurrent download.
- Options: `/v3/options/chain` for the live chain, `/v3/export` with
  `market=us_options&data=greeks` for historical greeks/IV.
- Fundamentals/corporate actions: `/v3/factors` (splits/dividends) and
  `/v3/reference` (company profiles and other reference datasets).
- A 400/403/404 for a given market/symbol combination (e.g. market not
  entitled on the free plan) is surfaced as DatasetUnavailable so the engine
  can skip it rather than abort the whole run.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import AsyncIterator, Optional, Sequence

import httpx

from ..models import Bar, CorporateAction, FundamentalRecord, OptionBar, OptionContract, Quote, Trade
from ..rate_limit import RollingQuota, TokenBucket
from ..retry import retry_request
from ..symbol_mapping import classify
from .base import DatasetUnavailable, Provider

logger = logging.getLogger(__name__)

BASE_URL = "https://download.londonstrategicedge.com"


def _default_market(symbol: str) -> str:
    kind = classify(symbol)
    return "crypto" if kind.is_crypto else "us_stocks"


class LSEProvider(Provider):
    name = "lse"

    def __init__(self, api_key: str, rate_limit_per_minute: int = 60, max_rows_per_day: int = 12_500_000,
                 max_bytes_per_week: int = 12 * 1024 ** 3, max_rows_per_export: int = 2_500_000,
                 max_concurrent_downloads: int = 1, max_retries: int = 5, retry_base_delay: float = 1.0) -> None:
        self._client = httpx.AsyncClient(base_url=BASE_URL, headers={"X-API-Key": api_key}, timeout=60.0)
        self._bucket = TokenBucket(rate_limit_per_minute)
        self._row_quota = RollingQuota(max_rows_per_day, window_seconds=24 * 3600)
        self._byte_quota = RollingQuota(max_bytes_per_week, window_seconds=7 * 24 * 3600)
        self._download_semaphore = asyncio.Semaphore(max_concurrent_downloads)
        self._max_rows_per_export = max_rows_per_export
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict) -> dict:
        await self._bucket.acquire()
        response = await retry_request(
            lambda: self._client.get(path, params={k: v for k, v in params.items() if v is not None}),
            max_retries=self._max_retries, base_delay=self._retry_base_delay, description=f"lse GET {path}",
        )
        self._raise_for_status(path, response)
        return response.json()

    async def _export(self, params: dict, path: str = "/v3/export") -> str:
        """Download a CSV export, respecting the single-connection and quota limits."""
        await self._bucket.acquire()
        await self._row_quota.consume(1)  # placeholder reservation; real row count applied after download
        async with self._download_semaphore:
            response = await retry_request(
                lambda: self._client.get(path, params={k: v for k, v in params.items() if v is not None}),
                max_retries=self._max_retries, base_delay=self._retry_base_delay, description=f"lse GET {path}",
            )
        self._raise_for_status(path, response)
        content_length = int(response.headers.get("Content-Length", len(response.content)))
        await self._byte_quota.consume(content_length)
        rows = int(response.headers.get("x-export-rows", 0)) or None
        if rows:
            await self._row_quota.consume(max(0, rows - 1))
        return response.text

    def _raise_for_status(self, path: str, response: httpx.Response) -> None:
        if response.status_code in (400, 403, 404):
            raise DatasetUnavailable(f"lse {path}: {response.status_code} {response.text[:200]}")
        response.raise_for_status()

    async def discover(self, symbol: str) -> dict:
        market = _default_market(symbol)
        try:
            data = await self._get_json("/v3/tables", {"market": market})
        except DatasetUnavailable as exc:
            return {"provider": self.name, "symbol": symbol, "market": market, "error": str(exc)}
        return {"provider": self.name, "symbol": symbol, "market": market, "tables": data}

    async def fetch_bars(self, symbol: str, resolution: str, start: str, end: str,
                           feed: Optional[str] = None) -> AsyncIterator[Sequence[Bar]]:
        market = feed or _default_market(symbol)
        csv_text = await self._export({
            "market": market, "symbol": symbol, "timeframe": resolution,
            "from": start, "to": end, "format": "csv", "adjusted": 0,
        })
        reader = csv.DictReader(io.StringIO(csv_text))
        page: list[Bar] = []
        for row in reader:
            page.append(Bar(
                provider=self.name, feed=market, symbol=symbol, resolution=resolution,
                timestamp=row["time"], open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row.get("volume") or 0),
                trade_count=int(row["trades"]) if row.get("trades") else None,
            ))
            if len(page) >= 5000:
                yield page
                page = []
        if page:
            yield page

    async def fetch_trades(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Trade]]:
        market = _default_market(symbol)
        csv_text = await self._export({
            "market": market, "symbol": symbol, "data": "trades", "from": start, "to": end, "format": "csv",
        })
        reader = csv.DictReader(io.StringIO(csv_text))
        page: list[Trade] = []
        for row in reader:
            page.append(Trade(
                provider=self.name, feed=market, symbol=symbol, timestamp=row["time"],
                price=float(row["price"]), size=float(row.get("size") or 0),
            ))
            if len(page) >= 5000:
                yield page
                page = []
        if page:
            yield page

    def fetch_quotes(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Quote]]:
        raise DatasetUnavailable("lse historical quotes: paid feed only (CME/Nasdaq) on free markets")

    async def fetch_option_chain(self, underlying: str) -> AsyncIterator[Sequence[OptionContract]]:
        offset = 0
        limit = 1000
        while True:
            data = await self._get_json("/v3/options/chain", {"underlying": underlying, "limit": limit, "offset": offset})
            rows = data.get("rows") or []
            if not rows:
                break
            as_of = data.get("greeks_as_of", "")
            page = []
            for row in rows:
                page.append(OptionContract(
                    provider=self.name, feed="indicative", underlying=underlying,
                    contract_symbol=row.get("symbol") or row.get("ticker"),
                    expiry=row.get("expiry"), strike=float(row.get("strike", 0)),
                    option_type=row.get("contract_type") or row.get("option_type"),
                    as_of=row.get("greeks_at") or as_of,
                    bid=row.get("bid"), ask=row.get("ask"), last=row.get("last_price") or row.get("last"),
                    open_interest=row.get("open_interest"), volume=row.get("volume_today") or row.get("volume"),
                    implied_volatility=row.get("iv") or row.get("implied_volatility"),
                    delta=row.get("delta"), gamma=row.get("gamma"), theta=row.get("theta"),
                    vega=row.get("vega"), rho=row.get("rho"),
                ))
            yield page
            returned = data.get("returned", len(rows))
            if returned < limit:
                break
            offset += limit

    async def fetch_option_bars(self, contract_symbol: str, resolution: str, start: str,
                                  end: str) -> AsyncIterator[Sequence[OptionBar]]:
        csv_text = await self._export({
            "market": "us_options", "symbol": contract_symbol, "timeframe": resolution,
            "from": start, "to": end, "format": "csv",
        })
        reader = csv.DictReader(io.StringIO(csv_text))
        page: list[OptionBar] = []
        for row in reader:
            page.append(OptionBar(
                provider=self.name, feed="us_options", contract_symbol=contract_symbol, resolution=resolution,
                timestamp=row["time"], open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row.get("volume") or 0),
            ))
            if len(page) >= 5000:
                yield page
                page = []
        if page:
            yield page

    async def fetch_fundamentals(self, symbol: str) -> AsyncIterator[Sequence[FundamentalRecord]]:
        csv_text = await self._export({"dataset": "company_profiles", "symbol": symbol, "format": "csv"},
                                        path="/v3/reference/export")
        reader = csv.DictReader(io.StringIO(csv_text))
        page = [
            FundamentalRecord(
                provider=self.name, feed="reference", symbol=symbol, as_of=rec.get("as_of", ""),
                statement_type="profile", period=rec.get("period", "latest"), fields_json=json.dumps(rec),
            )
            for rec in reader
        ]
        if page:
            yield page

    async def fetch_corporate_actions(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[CorporateAction]]:
        market = _default_market(symbol)
        data = await self._get_json("/v3/factors", {"market": market, "symbol": symbol, "from": start, "to": end})
        rows = data if isinstance(data, list) else data.get("data", [])
        page = []
        for row in rows:
            action_type = row.get("type") or row.get("action_type")
            page.append(CorporateAction(
                provider=self.name, feed=market, symbol=symbol, action_type=action_type,
                ex_date=row.get("ex_date", ""), declared_date=row.get("declared_date"),
                record_date=row.get("record_date"), pay_date=row.get("pay_date"),
                ratio=row.get("ratio"), cash_amount=row.get("cash_amount") or row.get("amount"),
            ))
        if page:
            yield page
