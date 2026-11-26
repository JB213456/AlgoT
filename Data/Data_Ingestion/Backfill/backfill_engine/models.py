"""Normalized data models shared across providers.

Every record carries `provider` and `feed` so provenance is preserved end to
end into the database (two providers, or two feeds of the same provider,
covering the same symbol/timestamp are never collapsed into one row).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Dataset(str, Enum):
    BARS = "bars"
    TRADES = "trades"
    QUOTES = "quotes"
    OPTION_CHAIN = "option_chain"
    OPTION_BARS = "option_bars"
    FUNDAMENTALS = "fundamentals"
    CORPORATE_ACTIONS = "corporate_actions"


@dataclass(frozen=True)
class Bar:
    provider: str
    feed: str
    symbol: str
    resolution: str  # e.g. "1Min", "1Day"
    timestamp: str  # ISO-8601 UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[int] = None
    vwap: Optional[float] = None
    adjustment: str = "raw"


@dataclass(frozen=True)
class Trade:
    provider: str
    feed: str
    symbol: str
    timestamp: str
    price: float
    size: float
    trade_id: Optional[str] = None
    exchange: Optional[str] = None
    conditions: str = ""


@dataclass(frozen=True)
class Quote:
    provider: str
    feed: str
    symbol: str
    timestamp: str
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    bid_exchange: Optional[str] = None
    ask_exchange: Optional[str] = None


@dataclass(frozen=True)
class OptionContract:
    provider: str
    feed: str
    underlying: str
    contract_symbol: str
    expiry: str  # YYYY-MM-DD
    strike: float
    option_type: str  # "call" | "put"
    as_of: str  # snapshot timestamp of this chain row
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    open_interest: Optional[float] = None
    volume: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None


@dataclass(frozen=True)
class OptionBar:
    provider: str
    feed: str
    contract_symbol: str
    resolution: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FundamentalRecord:
    provider: str
    feed: str
    symbol: str
    as_of: str
    statement_type: str  # "income" | "balance_sheet" | "cash_flow" | "earnings" | "profile"
    period: str  # e.g. "2024-Q4" or "2024-FY"
    fields_json: str  # raw normalized JSON blob of the statement/earnings fields


@dataclass(frozen=True)
class CorporateAction:
    provider: str
    feed: str
    symbol: str
    action_type: str  # "split" | "dividend"
    ex_date: str
    declared_date: Optional[str] = None
    record_date: Optional[str] = None
    pay_date: Optional[str] = None
    ratio: Optional[float] = None  # split ratio (new/old)
    cash_amount: Optional[float] = None  # dividend amount
