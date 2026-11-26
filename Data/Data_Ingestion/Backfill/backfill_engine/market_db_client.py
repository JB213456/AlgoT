"""Optional HTTP client for the platform-level market_data_db service.

Soft dependency: when MARKET_DATA_DB_URL is set, every record this engine
fetches is also POSTed to that service's REST API (over the network -- this
package does NOT import or depend on the `market_data_db` library itself,
so the backfill image stays independent of the database image), in addition
to this engine's own coverage-tracking SQLite (which remains the source of
truth for incremental gap detection -- the remote database knows nothing
about "what date range did we request", only "what rows exist").

Record -> JSON-dict conversion here intentionally mirrors
market_data_db/adapters/backfill_adapter.py's shapes (that module is the
source of truth for the wire schema) without importing it, so this engine
has zero dependency on the database's Python package or its heavier
transitive dependencies (duckdb, pyarrow, sqlalchemy).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx

from .retry import retry_request

logger = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^(\d{4})-(Q[1-4]|FY)$")


def _provider_key(obj: Any) -> str:
    feed = getattr(obj, "feed", None)
    return f"{obj.provider}_{feed}" if feed else obj.provider


def _infer_asset_class(symbol: str) -> str:
    return "crypto" if "/" in symbol else "equity"


def _parse_ts(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "T" in s:
        dt = datetime.fromisoformat(s)
    else:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_date_only(value: Any) -> str:
    return _parse_ts(value)[:10]


def _parse_period(period: str) -> tuple[int, str]:
    m = _PERIOD_RE.match(period)
    if not m:
        raise ValueError(f"unrecognized fiscal period format: {period!r}")
    return int(m.group(1)), m.group(2)


def _bar_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol, "asset_class": _infer_asset_class(obj.symbol), "timeframe": obj.resolution,
        "timestamp": _parse_ts(obj.timestamp), "open": obj.open, "high": obj.high, "low": obj.low,
        "close": obj.close, "volume": obj.volume, "trade_count": obj.trade_count, "vwap": obj.vwap,
    }


def _option_bar_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.contract_symbol, "asset_class": "option", "timeframe": obj.resolution,
        "timestamp": _parse_ts(obj.timestamp), "open": obj.open, "high": obj.high, "low": obj.low,
        "close": obj.close, "volume": obj.volume, "trade_count": None, "vwap": None,
    }


def _trade_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol, "asset_class": _infer_asset_class(obj.symbol), "timestamp": _parse_ts(obj.timestamp),
        "price": obj.price, "size": obj.size, "exchange": obj.exchange, "trade_id": obj.trade_id,
        "conditions": obj.conditions or None, "tape": None,
    }


def _quote_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol, "asset_class": _infer_asset_class(obj.symbol), "timestamp": _parse_ts(obj.timestamp),
        "bid_price": obj.bid_price, "bid_size": obj.bid_size, "ask_price": obj.ask_price, "ask_size": obj.ask_size,
        "bid_exchange": obj.bid_exchange, "ask_exchange": obj.ask_exchange, "conditions": None,
    }


def _option_contract_to_quote_record(obj: Any) -> dict:
    return {
        "option_symbol": obj.contract_symbol, "underlying_symbol": obj.underlying,
        "expiration": _parse_date_only(obj.expiry), "strike": obj.strike, "option_type": obj.option_type,
        "timestamp": _parse_ts(obj.as_of), "bid_price": obj.bid, "bid_size": None, "ask_price": obj.ask,
        "ask_size": None, "last_price": obj.last, "last_size": None, "volume": obj.volume,
        "open_interest": obj.open_interest,
    }


def _option_contract_to_greeks_record(obj: Any) -> Optional[dict]:
    if all(getattr(obj, g, None) is None for g in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility")):
        return None
    return {
        "option_symbol": obj.contract_symbol, "underlying_symbol": obj.underlying, "timestamp": _parse_ts(obj.as_of),
        "delta": obj.delta, "gamma": obj.gamma, "theta": obj.theta, "vega": obj.vega, "rho": obj.rho,
        "implied_volatility": obj.implied_volatility, "underlying_price": None,
    }


def _corporate_action_to_record(obj: Any) -> dict:
    ex_date = _parse_date_only(obj.ex_date)
    return {
        "symbol": obj.symbol, "action_type": obj.action_type, "ex_date": ex_date,
        "record_date": _parse_date_only(obj.record_date) if obj.record_date else None,
        "pay_date": _parse_date_only(obj.pay_date) if obj.pay_date else None,
        "ratio": obj.ratio, "amount": obj.cash_amount, "details": None,
        "timestamp": f"{ex_date}T00:00:00+00:00",
    }


def _fundamental_to_records(obj: Any) -> tuple[str, list[dict]]:
    fields = json.loads(obj.fields_json) if obj.fields_json else {}
    as_of = _parse_ts(obj.as_of)

    if obj.statement_type == "earnings":
        fiscal_year, fiscal_period = _parse_period(obj.period)
        return "earnings", [{
            "symbol": obj.symbol, "fiscal_year": fiscal_year, "fiscal_period": fiscal_period,
            "eps_estimate": fields.get("eps_estimate"), "eps_actual": fields.get("eps_actual"),
            "revenue_estimate": fields.get("revenue_estimate"), "revenue_actual": fields.get("revenue_actual"),
            "timestamp": as_of,
        }]

    if obj.statement_type == "profile":
        return "fundamentals", [
            {"symbol": obj.symbol, "metric": k, "value": v, "timestamp": as_of}
            for k, v in fields.items() if isinstance(v, (int, float))
        ]

    fiscal_year, fiscal_period = _parse_period(obj.period)
    return "financial_statements", [
        {
            "symbol": obj.symbol, "statement_type": obj.statement_type, "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period, "period_end_date": None, "filed_date": as_of[:10],
            "line_item": k, "value": v, "currency": fields.get("currency"), "timestamp": as_of,
        }
        for k, v in fields.items() if isinstance(v, (int, float))
    ]


_TYPE_DISPATCH = {
    "Bar": ("bars", _bar_to_record),
    "Trade": ("trades", _trade_to_record),
    "Quote": ("quotes", _quote_to_record),
    "OptionBar": ("bars", _option_bar_to_record),
    "CorporateAction": ("corporate_actions", _corporate_action_to_record),
}


class MarketDBClient:
    def __init__(self, base_url: str, api_key: str, max_retries: int = 5, retry_base_delay: float = 1.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": api_key}, timeout=30.0)
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    async def close(self) -> None:
        await self._client.aclose()

    async def assert_backfill_capacity(self) -> None:
        response = await self._client.get("/v1/storage/status")
        response.raise_for_status()
        status = response.json()
        if not status.get("backfill_allowed", False):
            raise RuntimeError(
                f"backfill suspended: database free space {status.get('free_bytes')} bytes "
                f"is below the safety threshold {status.get('minimum_free_bytes')} bytes"
            )

    async def _write(self, dataset: str, records: list[dict], provider: str, source: str = "backfill") -> int:
        if not records:
            return 0
        response = await retry_request(
            lambda: self._client.post(f"/v1/datasets/{dataset}/write",
                                        json={"provider": provider, "source": source, "records": records}),
            max_retries=self._max_retries, base_delay=self._retry_base_delay,
            description=f"market_data_db write {dataset}",
        )
        response.raise_for_status()
        return response.json().get("rows_written", len(records))

    async def ingest_record(self, obj: Any) -> int:
        cls_name = type(obj).__name__
        provider = _provider_key(obj)

        if cls_name == "OptionContract":
            rows = await self._write("options_quotes", [_option_contract_to_quote_record(obj)], provider)
            greeks = _option_contract_to_greeks_record(obj)
            if greeks is not None:
                rows += await self._write("options_greeks", [greeks], provider)
            return rows

        if cls_name == "FundamentalRecord":
            dataset, records = _fundamental_to_records(obj)
            return await self._write(dataset, records, provider)

        if cls_name not in _TYPE_DISPATCH:
            raise TypeError(f"no market_data_db wire mapping for record type {cls_name!r}")
        dataset, converter = _TYPE_DISPATCH[cls_name]
        return await self._write(dataset, [converter(obj)], provider)

    async def ingest_many(self, records: Iterable[Any]) -> int:
        await self.assert_backfill_capacity()
        total = 0
        for rec in records:
            # Coverage must advance only after the independent database has
            # accepted every record. Propagate failures so the backfill can
            # retry the same range after a database interruption.
            total += await self.ingest_record(rec)
        return total


def build_market_db_client() -> Optional[MarketDBClient]:
    base_url = os.environ.get("MARKET_DATA_DB_URL", "").strip()
    if not base_url:
        return None
    api_key = os.environ.get("MARKET_DATA_DB_API_KEY", "").strip()
    if not api_key:
        logger.warning("MARKET_DATA_DB_URL is set but MARKET_DATA_DB_API_KEY is not; skipping shared-DB writes")
        return None
    return MarketDBClient(base_url, api_key)
