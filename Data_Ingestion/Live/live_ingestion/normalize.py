"""Normalization and validation of raw Alpaca data into engine models."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Tuple

from .models import Bar, Quote, Source, Trade

logger = logging.getLogger("live_ingestion.normalize")


class ValidationError(ValueError):
    """Raised when a raw market data record fails validation."""


def to_utc(ts: Any) -> datetime:
    """Coerce a timestamp (datetime, pandas.Timestamp, or ISO string) to tz-aware UTC."""
    if ts is None:
        raise ValidationError("missing timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if not isinstance(ts, datetime):
        # pandas.Timestamp duck-types to datetime via .to_pydatetime()
        to_pydatetime = getattr(ts, "to_pydatetime", None)
        if to_pydatetime is None:
            raise ValidationError(f"unsupported timestamp type: {type(ts)!r}")
        ts = to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _conditions(raw: Any) -> Tuple[str, ...]:
    if not raw:
        return ()
    return tuple(str(c) for c in raw)


def normalize_trade(raw: Any, source: Source = Source.WEBSOCKET) -> Trade:
    price = float(raw.price)
    size = int(raw.size)
    if price <= 0:
        raise ValidationError(f"non-positive trade price: {price}")
    if size < 0:
        raise ValidationError(f"negative trade size: {size}")
    return Trade(
        symbol=raw.symbol,
        timestamp=to_utc(raw.timestamp),
        price=price,
        size=size,
        exchange=getattr(raw, "exchange", None),
        trade_id=getattr(raw, "id", None),
        conditions=_conditions(getattr(raw, "conditions", None)),
        tape=getattr(raw, "tape", None),
        source=source,
    )


def normalize_quote(raw: Any, source: Source = Source.WEBSOCKET) -> Quote:
    bid_price = float(raw.bid_price)
    ask_price = float(raw.ask_price)
    bid_size = int(raw.bid_size)
    ask_size = int(raw.ask_size)
    if bid_price < 0 or ask_price < 0:
        raise ValidationError(f"negative quote price: bid={bid_price} ask={ask_price}")
    if bid_size < 0 or ask_size < 0:
        raise ValidationError(f"negative quote size: bid={bid_size} ask={ask_size}")
    if bid_price > 0 and ask_price > 0 and bid_price > ask_price:
        logger.warning(
            "crossed quote for %s: bid=%s > ask=%s (keeping, exchange artifact)",
            raw.symbol,
            bid_price,
            ask_price,
        )
    return Quote(
        symbol=raw.symbol,
        timestamp=to_utc(raw.timestamp),
        bid_price=bid_price,
        bid_size=bid_size,
        ask_price=ask_price,
        ask_size=ask_size,
        bid_exchange=getattr(raw, "bid_exchange", None),
        ask_exchange=getattr(raw, "ask_exchange", None),
        conditions=_conditions(getattr(raw, "conditions", None)),
        tape=getattr(raw, "tape", None),
        source=source,
    )


def normalize_bar(
    raw: Any,
    source: Source = Source.WEBSOCKET,
    updated: bool = False,
    daily: bool = False,
) -> Bar:
    o, h, l, c = float(raw.open), float(raw.high), float(raw.low), float(raw.close)
    volume = int(raw.volume)
    if min(o, h, l, c) <= 0:
        raise ValidationError(f"non-positive OHLC in bar: {o}/{h}/{l}/{c}")
    if not (l <= o <= h and l <= c <= h):
        raise ValidationError(f"inconsistent OHLC bar for {raw.symbol}: O={o} H={h} L={l} C={c}")
    if volume < 0:
        raise ValidationError(f"negative bar volume: {volume}")
    return Bar(
        symbol=raw.symbol,
        timestamp=to_utc(raw.timestamp),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
        trade_count=getattr(raw, "trade_count", None),
        vwap=getattr(raw, "vwap", None),
        updated=updated,
        daily=daily,
        source=source,
    )
