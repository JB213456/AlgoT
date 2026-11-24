"""Backfill orchestrator: discovery, gap detection, chunked concurrent
download, normalization/validation/dedup, and coverage tracking.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db
from .config import Settings
from .market_db_client import build_market_db_client
from .models import Bar, Dataset
from .providers.alpaca import AlpacaProvider
from .providers.base import DatasetUnavailable, Provider
from .providers.lse import LSEProvider
from .symbol_mapping import classify, to_alpaca_symbol, to_lse_symbol
from .validation import filter_valid_bars

logger = logging.getLogger(__name__)

EARLIEST_DEFAULT_START = "2000-01-01"

ALPACA_STOCK_RESOLUTIONS = ["1Min", "5Min", "15Min", "1Hour", "1Day"]
ALPACA_CRYPTO_RESOLUTIONS = ["1Min", "15Min", "1Hour", "1Day"]
LSE_RESOLUTIONS = ["1m", "5m", "15m", "1h", "1d"]

# A user-supplied --resolution is one canonical token; providers use different
# syntax for the same period (Alpaca "1Day" vs LSE "1d"), so normalize it per
# provider rather than passing it through unchanged to both.
_RESOLUTION_ALIASES: dict[str, dict[str, str]] = {
    "1min": {"alpaca": "1Min", "lse": "1m"},
    "5min": {"alpaca": "5Min", "lse": "5m"},
    "15min": {"alpaca": "15Min", "lse": "15m"},
    "1hour": {"alpaca": "1Hour", "lse": "1h"},
    "1day": {"alpaca": "1Day", "lse": "1d"},
}


def _normalize_resolution(resolution: str, provider_name: str) -> str:
    key = resolution.strip().lower()
    aliases = _RESOLUTION_ALIASES.get(key)
    if aliases and provider_name in aliases:
        return aliases[provider_name]
    return resolution  # assume the caller already gave provider-native syntax

ALL_DATASETS = [d.value for d in Dataset]


@dataclass
class RunStats:
    provider: str
    dataset: str
    symbol: str
    resolution: str
    rows: int = 0
    error: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(s: str) -> datetime:
    s = s.split(".")[0] if "." in s else s  # strip fractional seconds (e.g. LSE greeks_at)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {s}")


def _missing_ranges(existing: Optional[tuple], start: str, end: str) -> list[tuple[str, str]]:
    """Given existing coverage [min_ts, max_ts] and a requested [start, end],
    return the sub-range(s) not yet covered. Historical data is treated as
    immutable, so only the leading/trailing gaps need to be fetched."""
    if existing is None:
        return [(start, end)]
    min_ts, max_ts = existing
    req_start, req_end = _parse_date(start), _parse_date(end)
    cov_start, cov_end = _parse_date(min_ts), _parse_date(max_ts)
    ranges: list[tuple[str, str]] = []
    if req_start < cov_start:
        gap_end = min(cov_start - timedelta(seconds=1), req_end)
        ranges.append((start, gap_end.strftime("%Y-%m-%d")))
    if req_end > cov_end:
        gap_start = max(cov_end + timedelta(seconds=1), req_start)
        ranges.append((gap_start.strftime("%Y-%m-%d"), end))
    return ranges


def _chunk_range(start: str, end: str, chunk_days: int) -> list[tuple[str, str]]:
    s, e = _parse_date(start), _parse_date(end)
    if s >= e:
        return []
    chunks = []
    cur = s
    while cur < e:
        nxt = min(cur + timedelta(days=chunk_days), e)
        chunks.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return chunks


class BackfillEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.conn = db.connect(settings.db_path)
        self.providers: dict[str, Provider] = {}
        if settings.alpaca_configured:
            self.providers["alpaca"] = AlpacaProvider(
                settings.alpaca_api_key, settings.alpaca_secret_key,
                rate_limit_per_minute=settings.alpaca_rate_limit_per_minute,
                max_concurrent=settings.alpaca_max_concurrent_requests,
                max_retries=settings.max_retries, retry_base_delay=settings.retry_base_delay_seconds,
            )
        if settings.lse_configured:
            self.providers["lse"] = LSEProvider(
                settings.lse_api_key,
                rate_limit_per_minute=settings.lse_rate_limit_per_minute,
                max_rows_per_day=settings.lse_max_rows_per_day,
                max_bytes_per_week=settings.lse_max_bytes_per_week,
                max_rows_per_export=settings.lse_max_rows_per_export,
                max_concurrent_downloads=settings.lse_max_concurrent_downloads,
                max_retries=settings.max_retries, retry_base_delay=settings.retry_base_delay_seconds,
            )
        self.market_db_client = build_market_db_client()

    async def close(self) -> None:
        for p in self.providers.values():
            await p.close()
        if self.market_db_client:
            await self.market_db_client.close()
        self.conn.close()

    def _provider_symbol(self, provider_name: str, symbol: str) -> str:
        return to_alpaca_symbol(symbol) if provider_name == "alpaca" else to_lse_symbol(symbol)

    def _resolutions_for(self, provider_name: str, symbol: str) -> list[str]:
        if provider_name == "alpaca":
            return ALPACA_CRYPTO_RESOLUTIONS if classify(symbol).is_crypto else ALPACA_STOCK_RESOLUTIONS
        return LSE_RESOLUTIONS

    async def backfill_bars(self, provider_name: str, symbol: str, start: str, end: str,
                              resolutions: Optional[list[str]] = None, chunk_days: int = 90) -> list[RunStats]:
        provider = self.providers[provider_name]
        provider_symbol = self._provider_symbol(provider_name, symbol)
        resolutions = resolutions or self._resolutions_for(provider_name, symbol)
        resolutions = [_normalize_resolution(r, provider_name) for r in resolutions]
        results = []
        for resolution in resolutions:
            stat = RunStats(provider=provider_name, dataset="bars", symbol=symbol, resolution=resolution)
            try:
                existing = db.get_coverage(self.conn, provider_name, provider_name, "bars", symbol, resolution)
                gaps = _missing_ranges(existing, start, end)
                if not gaps:
                    logger.info("%s/%s %s %s: already fully covered", provider_name, symbol, resolution, "bars")
                    results.append(stat)
                    continue
                chunks: list[tuple[str, str]] = []
                for g_start, g_end in gaps:
                    chunks.extend(_chunk_range(g_start, g_end, chunk_days))

                concurrency = self.settings.alpaca_max_concurrent_requests if provider_name == "alpaca" else 1
                sem = asyncio.Semaphore(concurrency)

                # LSE needs the market/feed derived from the *canonical* symbol
                # (e.g. "BTC/USD") since the provider-mapped symbol ("BTCUSDT")
                # no longer carries the crypto marker LSE's own classifier needs.
                lse_feed = "crypto" if classify(symbol).is_crypto else "us_stocks"
                feed_override = lse_feed if provider_name == "lse" else None

                async def run_chunk(c_start: str, c_end: str) -> tuple[int, Optional[str], Optional[str]]:
                    rows = 0
                    cmin = cmax = None
                    async with sem:
                        async for page in provider.fetch_bars(provider_symbol, resolution, c_start, c_end,
                                                                feed=feed_override):
                            valid = filter_valid_bars([
                                Bar(**{**b.__dict__, "symbol": symbol}) for b in page
                            ])
                            if not valid:
                                continue
                            db.upsert_bars(self.conn, valid)
                            if self.market_db_client:
                                await self.market_db_client.ingest_many(valid)
                            rows += len(valid)
                            ts_values = [b.timestamp for b in valid]
                            cmin = min(ts_values) if cmin is None else min(cmin, min(ts_values))
                            cmax = max(ts_values) if cmax is None else max(cmax, max(ts_values))
                    return rows, cmin, cmax

                chunk_results = await asyncio.gather(*(run_chunk(s, e) for s, e in chunks), return_exceptions=True)
                total_rows = 0
                min_ts = max_ts = None
                for res in chunk_results:
                    if isinstance(res, DatasetUnavailable):
                        logger.warning("%s %s %s bars: %s", provider_name, symbol, resolution, res)
                        continue
                    if isinstance(res, Exception):
                        raise res
                    rows, cmin, cmax = res
                    total_rows += rows
                    if cmin:
                        min_ts = cmin if min_ts is None else min(min_ts, cmin)
                    if cmax:
                        max_ts = cmax if max_ts is None else max(max_ts, cmax)
                if total_rows:
                    db.update_coverage(self.conn, provider_name, provider_name, "bars", symbol, resolution,
                                        min_ts, max_ts, total_rows, _now_iso())
                stat.rows = total_rows
                logger.info("%s/%s %s bars: +%d rows", provider_name, symbol, resolution, total_rows)
            except DatasetUnavailable as exc:
                stat.error = str(exc)
                logger.warning("%s/%s %s bars unavailable: %s", provider_name, symbol, resolution, exc)
            except Exception as exc:  # noqa: BLE001 - surfaced to CLI summary, run continues for other datasets
                stat.error = str(exc)
                logger.error("%s/%s %s bars failed: %s", provider_name, symbol, resolution, exc)
            results.append(stat)
        return results

    async def backfill_option_bars(self, provider_name: str, symbol: str, start: str, end: str,
                                     resolutions: Optional[list[str]] = None,
                                     max_contracts: int = 20) -> list[RunStats]:
        """Backfill historical option price bars.

        Option chains run into the thousands of contracts per underlying, and
        the free tiers' per-export row caps and daily quotas make backfilling
        full history for all of them impractical (and mostly pointless, since
        far OTM/expired contracts rarely traded). Discovery uses whichever
        configured provider exposes a chain (currently LSE); bars are then
        pulled for the `max_contracts` nearest-to-the-money, nearest-expiry
        contracts -- call again with a larger `max_contracts` if you need
        deeper coverage, it's quota-bounded either way.
        """
        chain_provider_name = next((p for p in ("lse", "alpaca") if p in self.providers
                                     and p in self.providers and hasattr(self.providers[p], "fetch_option_chain")), None)
        provider = self.providers[provider_name]
        resolutions = resolutions or (["1d"] if provider_name == "lse" else ["1Day"])
        resolutions = [_normalize_resolution(r, provider_name) for r in resolutions]
        results: list[RunStats] = []
        try:
            contracts = []
            async for page in self.providers.get(chain_provider_name or provider_name).fetch_option_chain(symbol):
                contracts.extend(page)
        except (DatasetUnavailable, NotImplementedError) as exc:
            stat = RunStats(provider=provider_name, dataset="option_bars", symbol=symbol, resolution="n/a",
                             error=f"could not discover contracts: {exc}")
            return [stat]

        # No reliable underlying spot price is modeled on OptionContract, so
        # this is a near-term/low-strike proxy for "most likely to matter",
        # not a true ATM selection -- good enough to bound the download.
        contracts = [c for c in contracts if c.implied_volatility is not None or c.last is not None]
        contracts.sort(key=lambda c: (c.expiry or "9999", c.strike or 0))
        chosen = contracts[:max_contracts]

        for contract in chosen:
            for resolution in resolutions:
                stat = RunStats(provider=provider_name, dataset="option_bars", symbol=contract.contract_symbol,
                                 resolution=resolution)
                existing = db.get_coverage(self.conn, provider_name, provider_name, "option_bars",
                                            contract.contract_symbol, resolution)
                gaps = _missing_ranges(existing, start, end)
                if not gaps:
                    results.append(stat)
                    continue
                try:
                    total = 0
                    cmin = cmax = None
                    for g_start, g_end in gaps:
                        async for page in provider.fetch_option_bars(contract.contract_symbol, resolution,
                                                                       g_start, g_end):
                            db.upsert_option_bars(self.conn, page)
                            if self.market_db_client:
                                await self.market_db_client.ingest_many(page)
                            total += len(page)
                            ts_values = [b.timestamp for b in page]
                            cmin = min(ts_values) if cmin is None else min(cmin, min(ts_values))
                            cmax = max(ts_values) if cmax is None else max(cmax, max(ts_values))
                    if total:
                        db.update_coverage(self.conn, provider_name, provider_name, "option_bars",
                                            contract.contract_symbol, resolution, cmin, cmax, total, _now_iso())
                    stat.rows = total
                except DatasetUnavailable as exc:
                    stat.error = str(exc)
                results.append(stat)
        return results

    async def backfill_option_chain(self, provider_name: str, symbol: str) -> RunStats:
        provider = self.providers[provider_name]
        provider_symbol = self._provider_symbol(provider_name, symbol)
        stat = RunStats(provider=provider_name, dataset="option_chain", symbol=symbol, resolution="n/a")
        try:
            total = 0
            async for page in provider.fetch_option_chain(provider_symbol):
                db.upsert_option_chain(self.conn, page)
                if self.market_db_client:
                    await self.market_db_client.ingest_many(page)
                total += len(page)
            stat.rows = total
        except DatasetUnavailable as exc:
            stat.error = str(exc)
            logger.warning("%s/%s option_chain unavailable: %s", provider_name, symbol, exc)
        except NotImplementedError:
            stat.error = "not supported by this provider"
        return stat

    async def backfill_fundamentals(self, provider_name: str, symbol: str) -> RunStats:
        provider = self.providers[provider_name]
        provider_symbol = self._provider_symbol(provider_name, symbol)
        stat = RunStats(provider=provider_name, dataset="fundamentals", symbol=symbol, resolution="n/a")
        try:
            total = 0
            async for page in provider.fetch_fundamentals(provider_symbol):
                db.upsert_fundamentals(self.conn, page)
                if self.market_db_client:
                    await self.market_db_client.ingest_many(page)
                total += len(page)
            stat.rows = total
        except DatasetUnavailable as exc:
            stat.error = str(exc)
            logger.warning("%s/%s fundamentals unavailable: %s", provider_name, symbol, exc)
        return stat

    async def backfill_corporate_actions(self, provider_name: str, symbol: str, start: str, end: str) -> RunStats:
        provider = self.providers[provider_name]
        provider_symbol = self._provider_symbol(provider_name, symbol)
        stat = RunStats(provider=provider_name, dataset="corporate_actions", symbol=symbol, resolution="n/a")
        try:
            total = 0
            async for page in provider.fetch_corporate_actions(provider_symbol, start, end):
                db.upsert_corporate_actions(self.conn, page)
                if self.market_db_client:
                    await self.market_db_client.ingest_many(page)
                total += len(page)
            stat.rows = total
        except DatasetUnavailable as exc:
            stat.error = str(exc)
            logger.warning("%s/%s corporate_actions unavailable: %s", provider_name, symbol, exc)
        return stat

    async def run(self, symbol: str, providers: list[str], datasets: list[str], start: str, end: str,
                   resolutions: Optional[list[str]] = None) -> list[RunStats]:
        all_stats: list[RunStats] = []
        for provider_name in providers:
            if provider_name not in self.providers:
                logger.warning("provider %s not configured (missing credentials) -- skipping", provider_name)
                continue
            if "bars" in datasets:
                all_stats.extend(await self.backfill_bars(provider_name, symbol, start, end, resolutions))
            if "option_chain" in datasets:
                all_stats.append(await self.backfill_option_chain(provider_name, symbol))
            if "option_bars" in datasets:
                all_stats.extend(await self.backfill_option_bars(provider_name, symbol, start, end, resolutions))
            if "fundamentals" in datasets:
                all_stats.append(await self.backfill_fundamentals(provider_name, symbol))
            if "corporate_actions" in datasets:
                all_stats.append(await self.backfill_corporate_actions(provider_name, symbol, start, end))
        return all_stats
