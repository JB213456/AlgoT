"""Adapter for backfill_engine's data model (Alpaca + London Strategic Edge).

backfill_engine.providers/* are not implemented yet (per the current state of that
package), so this adapter is duck-typed against the field names already fixed in
backfill_engine/models.py (Bar, Trade, Quote, OptionContract, OptionBar,
FundamentalRecord, CorporateAction) rather than importing that package. Once a
provider module exists, feeding it into this database is just:

    from market_data_db.adapters.backfill_adapter import ingest_record
    for rec in provider.fetch_bars(...):
        ingest_record(db, rec)

No changes to backfill_engine are required; this module does not read from or
write to backfill_engine's own SQLite file (backfill_data/market_data.sqlite3) —
it only knows how to translate its dataclasses into this database's schema.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..db import MarketDataDB
from ..schema import Dataset

_PERIOD_RE = re.compile(r"^(\d{4})-(Q[1-4]|FY)$")


def provider_key(obj: Any) -> str:
    """Fold provider+feed into one provenance string (e.g. 'alpaca_iex',
    'lse_default') so two feeds from the same vendor never collide."""
    feed = getattr(obj, "feed", None)
    return f"{obj.provider}_{feed}" if feed else obj.provider


def infer_asset_class(symbol: str) -> str:
    return "crypto" if "/" in symbol else "equity"


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_period(period: str) -> tuple[int, str]:
    m = _PERIOD_RE.match(period)
    if not m:
        raise ValueError(f"unrecognized fiscal period format: {period!r}")
    return int(m.group(1)), m.group(2)


def bar_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol,
        "asset_class": infer_asset_class(obj.symbol),
        "timeframe": obj.resolution,
        "timestamp": _parse_ts(obj.timestamp),
        "open": obj.open,
        "high": obj.high,
        "low": obj.low,
        "close": obj.close,
        "volume": obj.volume,
        "trade_count": obj.trade_count,
        "vwap": obj.vwap,
    }


def option_bar_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.contract_symbol,
        "asset_class": "option",
        "timeframe": obj.resolution,
        "timestamp": _parse_ts(obj.timestamp),
        "open": obj.open,
        "high": obj.high,
        "low": obj.low,
        "close": obj.close,
        "volume": obj.volume,
        "trade_count": None,
        "vwap": None,
    }


def trade_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol,
        "asset_class": infer_asset_class(obj.symbol),
        "timestamp": _parse_ts(obj.timestamp),
        "price": obj.price,
        "size": obj.size,
        "exchange": obj.exchange,
        "trade_id": obj.trade_id,
        "conditions": obj.conditions or None,
        "tape": None,
    }


def quote_to_record(obj: Any) -> dict:
    return {
        "symbol": obj.symbol,
        "asset_class": infer_asset_class(obj.symbol),
        "timestamp": _parse_ts(obj.timestamp),
        "bid_price": obj.bid_price,
        "bid_size": obj.bid_size,
        "ask_price": obj.ask_price,
        "ask_size": obj.ask_size,
        "bid_exchange": obj.bid_exchange,
        "ask_exchange": obj.ask_exchange,
        "conditions": None,
    }


def option_contract_to_quote_record(obj: Any) -> dict:
    return {
        "option_symbol": obj.contract_symbol,
        "underlying_symbol": obj.underlying,
        "expiration": _parse_ts(obj.expiry).date(),
        "strike": obj.strike,
        "option_type": obj.option_type,
        "timestamp": _parse_ts(obj.as_of),
        "bid_price": obj.bid,
        "bid_size": None,
        "ask_price": obj.ask,
        "ask_size": None,
        "last_price": obj.last,
        "last_size": None,
        "volume": obj.volume,
        "open_interest": obj.open_interest,
    }


def option_contract_to_greeks_record(obj: Any) -> dict | None:
    if all(getattr(obj, g, None) is None for g in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility")):
        return None
    return {
        "option_symbol": obj.contract_symbol,
        "underlying_symbol": obj.underlying,
        "timestamp": _parse_ts(obj.as_of),
        "delta": obj.delta,
        "gamma": obj.gamma,
        "theta": obj.theta,
        "vega": obj.vega,
        "rho": obj.rho,
        "implied_volatility": obj.implied_volatility,
        "underlying_price": None,
    }


def corporate_action_to_record(obj: Any) -> dict:
    ex_date = _parse_ts(obj.ex_date).date()
    return {
        "symbol": obj.symbol,
        "action_type": obj.action_type,
        "ex_date": ex_date,
        "record_date": _parse_ts(obj.record_date).date() if obj.record_date else None,
        "pay_date": _parse_ts(obj.pay_date).date() if obj.pay_date else None,
        "ratio": obj.ratio,
        "amount": obj.cash_amount,
        "details": None,
        "timestamp": datetime.combine(ex_date, datetime.min.time(), tzinfo=timezone.utc),
    }


def fundamental_to_records(obj: Any) -> tuple[Dataset, list[dict]]:
    """FundamentalRecord.fields_json is a raw JSON blob whose shape depends on
    statement_type; flatten it into this database's normalized long-format rows."""
    fields = json.loads(obj.fields_json) if obj.fields_json else {}
    as_of = _parse_ts(obj.as_of)

    if obj.statement_type == "earnings":
        fiscal_year, fiscal_period = _parse_period(obj.period)
        return Dataset.EARNINGS, [{
            "symbol": obj.symbol,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
            "eps_estimate": fields.get("eps_estimate"),
            "eps_actual": fields.get("eps_actual"),
            "revenue_estimate": fields.get("revenue_estimate"),
            "revenue_actual": fields.get("revenue_actual"),
            "timestamp": as_of,
        }]

    if obj.statement_type == "profile":
        return Dataset.FUNDAMENTALS, [
            {"symbol": obj.symbol, "metric": k, "value": v, "timestamp": as_of}
            for k, v in fields.items()
            if isinstance(v, (int, float))
        ]

    # income / balance_sheet / cash_flow
    fiscal_year, fiscal_period = _parse_period(obj.period)
    return Dataset.FINANCIAL_STATEMENTS, [
        {
            "symbol": obj.symbol,
            "statement_type": obj.statement_type,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
            "period_end_date": None,
            "filed_date": as_of.date(),
            "line_item": k,
            "value": v,
            "currency": fields.get("currency"),
            "timestamp": as_of,
        }
        for k, v in fields.items()
        if isinstance(v, (int, float))
    ]


_TYPE_DISPATCH = {
    "Bar": (Dataset.BARS, bar_to_record),
    "Trade": (Dataset.TRADES, trade_to_record),
    "Quote": (Dataset.QUOTES, quote_to_record),
    "OptionBar": (Dataset.BARS, option_bar_to_record),
    "CorporateAction": (Dataset.CORPORATE_ACTIONS, corporate_action_to_record),
}


def ingest_record(db: MarketDataDB, obj: Any, *, source: str = "backfill") -> int:
    """Write one backfill_engine record (any of its dataclasses) into the database.
    Returns the number of rows written (OptionContract and FundamentalRecord may
    expand into more than one row/dataset)."""
    cls_name = type(obj).__name__
    provider = provider_key(obj)

    if cls_name == "OptionContract":
        rows = 0
        rows += db.write(Dataset.OPTIONS_QUOTES, [option_contract_to_quote_record(obj)], provider=provider, source=source)
        greeks = option_contract_to_greeks_record(obj)
        if greeks is not None:
            rows += db.write(Dataset.OPTIONS_GREEKS, [greeks], provider=provider, source=source)
        return rows

    if cls_name == "FundamentalRecord":
        dataset, records = fundamental_to_records(obj)
        if not records:
            return 0
        return db.write(dataset, records, provider=provider, source=source)

    if cls_name not in _TYPE_DISPATCH:
        raise TypeError(f"no backfill adapter for record type {cls_name!r}")
    dataset, converter = _TYPE_DISPATCH[cls_name]
    return db.write(dataset, [converter(obj)], provider=provider, source=source)
