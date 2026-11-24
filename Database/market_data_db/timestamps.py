from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd


def to_utc(ts) -> pd.Timestamp:
    """Normalize any timestamp-like value to a tz-aware UTC pandas.Timestamp.

    Naive datetimes are assumed to already be UTC (callers must convert otherwise
    before calling); tz-aware values are converted.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t


def utc_series(values) -> pd.Series:
    s = pd.to_datetime(pd.Series(values), utc=True)
    return s


def partition_date(ts) -> date:
    return to_utc(ts).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
