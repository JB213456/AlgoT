"""Dataset registry: pyarrow schemas, primary keys and partition columns for every
supported market data type. Adding a new dataset means adding one entry here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pyarrow as pa

UTC_TS = pa.timestamp("us", tz="UTC")
UTC_TS_NS = pa.timestamp("ns", tz="UTC")

_COMMON = [
    pa.field("provider", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),  # 'live' | 'backfill'
    pa.field("ingested_at", UTC_TS, nullable=False),
    pa.field("raw", pa.string(), nullable=True),  # optional raw provider payload (JSON)
]


class Dataset(str, Enum):
    BARS = "bars"
    TRADES = "trades"
    QUOTES = "quotes"
    OPTIONS_QUOTES = "options_quotes"
    OPTIONS_GREEKS = "options_greeks"
    FINANCIAL_STATEMENTS = "financial_statements"
    FUNDAMENTALS = "fundamentals"
    EARNINGS = "earnings"
    CORPORATE_ACTIONS = "corporate_actions"
    MACRO = "macro"
    REFERENCE = "reference"


@dataclass(frozen=True)
class DatasetSpec:
    name: Dataset
    schema: pa.Schema
    primary_key: tuple[str, ...]
    time_col: str
    symbol_col: str | None
    source_priority: tuple[str, ...] = ("live", "backfill")


def _schema(fields: list[pa.Field]) -> pa.Schema:
    return pa.schema(fields + _COMMON)


_SPECS: dict[Dataset, DatasetSpec] = {
    Dataset.BARS: DatasetSpec(
        name=Dataset.BARS,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("asset_class", pa.string(), nullable=False),  # equity/crypto/forex/commodity/index
            pa.field("timeframe", pa.string(), nullable=False),  # 1Min/5Min/1Day/...
            pa.field("timestamp", UTC_TS, nullable=False),  # bar start, UTC
            pa.field("open", pa.float64(), nullable=False),
            pa.field("high", pa.float64(), nullable=False),
            pa.field("low", pa.float64(), nullable=False),
            pa.field("close", pa.float64(), nullable=False),
            pa.field("volume", pa.float64(), nullable=False),
            pa.field("trade_count", pa.int64(), nullable=True),
            pa.field("vwap", pa.float64(), nullable=True),
        ]),
        primary_key=("symbol", "timeframe", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.TRADES: DatasetSpec(
        name=Dataset.TRADES,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("asset_class", pa.string(), nullable=False),
            pa.field("timestamp", UTC_TS_NS, nullable=False),
            pa.field("price", pa.float64(), nullable=False),
            pa.field("size", pa.float64(), nullable=False),
            pa.field("exchange", pa.string(), nullable=True),
            pa.field("trade_id", pa.string(), nullable=True),
            pa.field("conditions", pa.string(), nullable=True),
            pa.field("tape", pa.string(), nullable=True),
        ]),
        primary_key=("symbol", "timestamp", "exchange", "trade_id", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.QUOTES: DatasetSpec(
        name=Dataset.QUOTES,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("asset_class", pa.string(), nullable=False),
            pa.field("timestamp", UTC_TS_NS, nullable=False),
            pa.field("bid_price", pa.float64(), nullable=True),
            pa.field("bid_size", pa.float64(), nullable=True),
            pa.field("ask_price", pa.float64(), nullable=True),
            pa.field("ask_size", pa.float64(), nullable=True),
            pa.field("bid_exchange", pa.string(), nullable=True),
            pa.field("ask_exchange", pa.string(), nullable=True),
            pa.field("conditions", pa.string(), nullable=True),
        ]),
        primary_key=("symbol", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.OPTIONS_QUOTES: DatasetSpec(
        name=Dataset.OPTIONS_QUOTES,
        schema=_schema([
            pa.field("option_symbol", pa.string(), nullable=False),  # OCC symbol
            pa.field("underlying_symbol", pa.string(), nullable=False),
            pa.field("expiration", pa.date32(), nullable=False),
            pa.field("strike", pa.float64(), nullable=False),
            pa.field("option_type", pa.string(), nullable=False),  # call/put
            pa.field("timestamp", UTC_TS, nullable=False),
            pa.field("bid_price", pa.float64(), nullable=True),
            pa.field("bid_size", pa.float64(), nullable=True),
            pa.field("ask_price", pa.float64(), nullable=True),
            pa.field("ask_size", pa.float64(), nullable=True),
            pa.field("last_price", pa.float64(), nullable=True),
            pa.field("last_size", pa.float64(), nullable=True),
            pa.field("volume", pa.float64(), nullable=True),
            pa.field("open_interest", pa.float64(), nullable=True),
        ]),
        primary_key=("option_symbol", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="underlying_symbol",
    ),
    Dataset.OPTIONS_GREEKS: DatasetSpec(
        name=Dataset.OPTIONS_GREEKS,
        schema=_schema([
            pa.field("option_symbol", pa.string(), nullable=False),
            pa.field("underlying_symbol", pa.string(), nullable=False),
            pa.field("timestamp", UTC_TS, nullable=False),
            pa.field("delta", pa.float64(), nullable=True),
            pa.field("gamma", pa.float64(), nullable=True),
            pa.field("theta", pa.float64(), nullable=True),
            pa.field("vega", pa.float64(), nullable=True),
            pa.field("rho", pa.float64(), nullable=True),
            pa.field("implied_volatility", pa.float64(), nullable=True),
            pa.field("underlying_price", pa.float64(), nullable=True),
        ]),
        primary_key=("option_symbol", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="underlying_symbol",
    ),
    Dataset.FINANCIAL_STATEMENTS: DatasetSpec(
        name=Dataset.FINANCIAL_STATEMENTS,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("statement_type", pa.string(), nullable=False),  # income/balance/cashflow
            pa.field("fiscal_year", pa.int32(), nullable=False),
            pa.field("fiscal_period", pa.string(), nullable=False),  # Q1/Q2/Q3/Q4/FY
            pa.field("period_end_date", pa.date32(), nullable=True),
            pa.field("filed_date", pa.date32(), nullable=True),
            pa.field("line_item", pa.string(), nullable=False),
            pa.field("value", pa.float64(), nullable=True),
            pa.field("currency", pa.string(), nullable=True),
            # timestamp column required by DatasetSpec contract; mirrors filed/period end
            pa.field("timestamp", UTC_TS, nullable=False),
        ]),
        primary_key=("symbol", "statement_type", "fiscal_year", "fiscal_period", "line_item", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.FUNDAMENTALS: DatasetSpec(
        name=Dataset.FUNDAMENTALS,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("metric", pa.string(), nullable=False),
            pa.field("value", pa.float64(), nullable=True),
            pa.field("timestamp", UTC_TS, nullable=False),  # as-of date, midnight UTC
        ]),
        primary_key=("symbol", "metric", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.EARNINGS: DatasetSpec(
        name=Dataset.EARNINGS,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("fiscal_year", pa.int32(), nullable=False),
            pa.field("fiscal_period", pa.string(), nullable=False),
            pa.field("eps_estimate", pa.float64(), nullable=True),
            pa.field("eps_actual", pa.float64(), nullable=True),
            pa.field("revenue_estimate", pa.float64(), nullable=True),
            pa.field("revenue_actual", pa.float64(), nullable=True),
            pa.field("timestamp", UTC_TS, nullable=False),  # report date, midnight UTC
        ]),
        primary_key=("symbol", "fiscal_year", "fiscal_period", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.CORPORATE_ACTIONS: DatasetSpec(
        name=Dataset.CORPORATE_ACTIONS,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("action_type", pa.string(), nullable=False),  # split/dividend/merger/spinoff/other
            pa.field("ex_date", pa.date32(), nullable=False),
            pa.field("record_date", pa.date32(), nullable=True),
            pa.field("pay_date", pa.date32(), nullable=True),
            pa.field("ratio", pa.float64(), nullable=True),
            pa.field("amount", pa.float64(), nullable=True),
            pa.field("details", pa.string(), nullable=True),  # json blob
            pa.field("timestamp", UTC_TS, nullable=False),  # mirrors ex_date, midnight UTC
        ]),
        primary_key=("symbol", "action_type", "ex_date", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
    Dataset.MACRO: DatasetSpec(
        name=Dataset.MACRO,
        schema=_schema([
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("timestamp", UTC_TS, nullable=False),
            pa.field("value", pa.float64(), nullable=True),
            pa.field("unit", pa.string(), nullable=True),
        ]),
        primary_key=("series_id", "timestamp", "provider"),
        time_col="timestamp",
        symbol_col="series_id",
    ),
    Dataset.REFERENCE: DatasetSpec(
        name=Dataset.REFERENCE,
        schema=_schema([
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("asset_class", pa.string(), nullable=False),
            pa.field("exchange", pa.string(), nullable=True),
            pa.field("name", pa.string(), nullable=True),
            pa.field("currency", pa.string(), nullable=True),
            pa.field("active", pa.bool_(), nullable=True),
            pa.field("timestamp", UTC_TS, nullable=False),  # as-of, midnight UTC
        ]),
        primary_key=("symbol", "provider"),
        time_col="timestamp",
        symbol_col="symbol",
    ),
}


def get_spec(dataset: Dataset | str) -> DatasetSpec:
    ds = Dataset(dataset)
    return _SPECS[ds]


def all_datasets() -> list[Dataset]:
    return list(_SPECS.keys())
