"""Normalized data models used throughout the engine.

These are deliberately independent of alpaca-py's pydantic models so the rest
of the engine (and downstream consumers) are insulated from SDK changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Tuple


class EventType(str, Enum):
    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"
    UPDATED_BAR = "updated_bar"
    DAILY_BAR = "daily_bar"
    STATUS = "status"


class Source(str, Enum):
    WEBSOCKET = "websocket"
    REST = "rest"


@dataclass(frozen=True)
class Trade:
    symbol: str
    timestamp: datetime  # tz-aware UTC
    price: float
    size: int
    exchange: Optional[str] = None
    trade_id: Optional[int] = None
    conditions: Tuple[str, ...] = ()
    tape: Optional[str] = None
    source: Source = Source.WEBSOCKET

    def dedup_key(self) -> tuple:
        if self.trade_id is not None:
            return ("trade", self.symbol, self.trade_id)
        return ("trade", self.symbol, self.timestamp.isoformat(), self.price, self.size)


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid_price: float
    bid_size: int
    ask_price: float
    ask_size: int
    bid_exchange: Optional[str] = None
    ask_exchange: Optional[str] = None
    conditions: Tuple[str, ...] = ()
    tape: Optional[str] = None
    source: Source = Source.WEBSOCKET

    def dedup_key(self) -> tuple:
        return (
            "quote",
            self.symbol,
            self.timestamp.isoformat(),
            self.bid_price,
            self.bid_size,
            self.ask_price,
            self.ask_size,
        )


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime  # bar open time
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: Optional[int] = None
    vwap: Optional[float] = None
    updated: bool = False
    daily: bool = False
    source: Source = Source.WEBSOCKET

    def dedup_key(self) -> tuple:
        kind = "daily_bar" if self.daily else ("updated_bar" if self.updated else "bar")
        return (kind, self.symbol, self.timestamp.isoformat())


@dataclass(frozen=True)
class MarketDataEvent:
    type: EventType
    symbol: str
    timestamp: datetime
    payload: Any
    received_at: datetime
    source: Source = Source.WEBSOCKET
