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


def parse_utc_series(values) -> pd.Series:
    """Parse a column of timestamp-like values to tz-aware UTC.

    Uses format="ISO8601" for string input. Without it, pandas infers a single
    strptime format from the first element and then rejects every later element
    that does not match it exactly -- so a batch mixing whole-second and
    sub-second ISO timestamps, which real trade and quote feeds do constantly,
    fails outright:

        ValueError: time data "2026-02-02T14:30:00.100000+00:00"
                    doesn't match format "%Y-%m-%dT%H:%M:%S%z"

    That made write success depend on the precision of whichever row happened
    to come first, and it scales badly with batch size: one mixed batch of
    1000 rows loses all 1000.

    ISO8601 parses each element on its own merits but rejects bare epoch
    numbers, so it is applied only when the values are actually strings.
    """
    s = pd.Series(values)
    # Exclude only the dtypes ISO8601 cannot handle (bare epoch numbers) or
    # does not need (already-parsed datetimes). Testing for a string dtype
    # directly is not portable: pandas 2 gives a column of Python strings
    # dtype "object" while pandas 3 gives it "str".
    if s.empty or pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
        return pd.to_datetime(s, utc=True)
    return pd.to_datetime(s, utc=True, format="ISO8601")


# Retained for callers that imported the previous name.
def utc_series(values) -> pd.Series:
    return parse_utc_series(values)


def partition_date(ts) -> date:
    return to_utc(ts).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
