"""SQLite-backed storage for backfilled historical market data.

Schema is provider/feed aware: the same symbol+timestamp from two different
providers (or two feeds of one provider) are stored as distinct rows, never
collapsed. A `coverage` table tracks, per (provider, feed, dataset, symbol,
resolution), the contiguous [min_ts, max_ts] window already downloaded, so
repeated runs only fetch what's missing.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .models import Bar, CorporateAction, FundamentalRecord, OptionBar, OptionContract, Quote, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    resolution TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    trade_count INTEGER, vwap REAL, adjustment TEXT,
    PRIMARY KEY (provider, feed, symbol, resolution, timestamp, adjustment)
);

CREATE TABLE IF NOT EXISTS trades (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    price REAL, size REAL, trade_id TEXT, exchange TEXT, conditions TEXT,
    PRIMARY KEY (provider, feed, symbol, timestamp, trade_id, price, size)
);

CREATE TABLE IF NOT EXISTS quotes (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    bid_price REAL, bid_size REAL, ask_price REAL, ask_size REAL,
    bid_exchange TEXT, ask_exchange TEXT,
    PRIMARY KEY (provider, feed, symbol, timestamp, bid_price, ask_price)
);

CREATE TABLE IF NOT EXISTS option_chain (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    underlying TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    expiry TEXT, strike REAL, option_type TEXT, as_of TEXT NOT NULL,
    bid REAL, ask REAL, last REAL, open_interest REAL, volume REAL,
    implied_volatility REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    PRIMARY KEY (provider, feed, contract_symbol, as_of)
);

CREATE TABLE IF NOT EXISTS option_bars (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    resolution TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (provider, feed, contract_symbol, resolution, timestamp)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    statement_type TEXT NOT NULL,
    period TEXT NOT NULL,
    fields_json TEXT,
    PRIMARY KEY (provider, feed, symbol, statement_type, period, as_of)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    declared_date TEXT, record_date TEXT, pay_date TEXT,
    ratio REAL, cash_amount REAL,
    PRIMARY KEY (provider, feed, symbol, action_type, ex_date)
);

CREATE TABLE IF NOT EXISTS coverage (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    dataset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    resolution TEXT NOT NULL,
    min_ts TEXT,
    max_ts TEXT,
    rows_fetched INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    PRIMARY KEY (provider, feed, dataset, symbol, resolution)
);

CREATE TABLE IF NOT EXISTS backfill_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    dataset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    resolution TEXT NOT NULL,
    requested_start TEXT,
    requested_end TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    last_cursor TEXT,
    error TEXT,
    updated_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _upsert_many(conn: sqlite3.Connection, table: str, columns: Sequence[str], rows: Iterable[tuple]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in columns)
    col_list = ",".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    with closing(conn.cursor()) as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


_BAR_COLS = ["provider", "feed", "symbol", "resolution", "timestamp", "open", "high", "low", "close",
             "volume", "trade_count", "vwap", "adjustment"]


def upsert_bars(conn: sqlite3.Connection, bars: Iterable[Bar]) -> int:
    rows = [tuple(getattr(b, c) for c in _BAR_COLS) for b in bars]
    return _upsert_many(conn, "bars", _BAR_COLS, rows)


_TRADE_COLS = ["provider", "feed", "symbol", "timestamp", "price", "size", "trade_id", "exchange", "conditions"]


def upsert_trades(conn: sqlite3.Connection, trades: Iterable[Trade]) -> int:
    rows = [tuple(getattr(t, c) for c in _TRADE_COLS) for t in trades]
    return _upsert_many(conn, "trades", _TRADE_COLS, rows)


_QUOTE_COLS = ["provider", "feed", "symbol", "timestamp", "bid_price", "bid_size", "ask_price", "ask_size",
               "bid_exchange", "ask_exchange"]


def upsert_quotes(conn: sqlite3.Connection, quotes: Iterable[Quote]) -> int:
    rows = [tuple(getattr(q, c) for c in _QUOTE_COLS) for q in quotes]
    return _upsert_many(conn, "quotes", _QUOTE_COLS, rows)


_OPTION_CHAIN_COLS = ["provider", "feed", "underlying", "contract_symbol", "expiry", "strike", "option_type",
                       "as_of", "bid", "ask", "last", "open_interest", "volume", "implied_volatility",
                       "delta", "gamma", "theta", "vega", "rho"]


def upsert_option_chain(conn: sqlite3.Connection, contracts: Iterable[OptionContract]) -> int:
    rows = [tuple(getattr(c, col) for col in _OPTION_CHAIN_COLS) for c in contracts]
    return _upsert_many(conn, "option_chain", _OPTION_CHAIN_COLS, rows)


_OPTION_BAR_COLS = ["provider", "feed", "contract_symbol", "resolution", "timestamp", "open", "high", "low",
                     "close", "volume"]


def upsert_option_bars(conn: sqlite3.Connection, bars: Iterable[OptionBar]) -> int:
    rows = [tuple(getattr(b, c) for c in _OPTION_BAR_COLS) for b in bars]
    return _upsert_many(conn, "option_bars", _OPTION_BAR_COLS, rows)


_FUNDAMENTALS_COLS = ["provider", "feed", "symbol", "as_of", "statement_type", "period", "fields_json"]


def upsert_fundamentals(conn: sqlite3.Connection, records: Iterable[FundamentalRecord]) -> int:
    rows = [tuple(getattr(r, c) for c in _FUNDAMENTALS_COLS) for r in records]
    return _upsert_many(conn, "fundamentals", _FUNDAMENTALS_COLS, rows)


_CORP_ACTION_COLS = ["provider", "feed", "symbol", "action_type", "ex_date", "declared_date", "record_date",
                      "pay_date", "ratio", "cash_amount"]


def upsert_corporate_actions(conn: sqlite3.Connection, actions: Iterable[CorporateAction]) -> int:
    rows = [tuple(getattr(a, c) for c in _CORP_ACTION_COLS) for a in actions]
    return _upsert_many(conn, "corporate_actions", _CORP_ACTION_COLS, rows)


def get_coverage(conn: sqlite3.Connection, provider: str, feed: str, dataset: str, symbol: str,
                  resolution: str) -> Optional[tuple[str, str]]:
    cur = conn.execute(
        "SELECT min_ts, max_ts FROM coverage WHERE provider=? AND feed=? AND dataset=? AND symbol=? AND resolution=?",
        (provider, feed, dataset, symbol, resolution),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return (row[0], row[1])


def update_coverage(conn: sqlite3.Connection, provider: str, feed: str, dataset: str, symbol: str,
                     resolution: str, min_ts: str, max_ts: str, rows_fetched: int, run_at: str) -> None:
    existing = get_coverage(conn, provider, feed, dataset, symbol, resolution)
    if existing is not None:
        min_ts = min(min_ts, existing[0])
        max_ts = max(max_ts, existing[1])
    conn.execute(
        """
        INSERT INTO coverage (provider, feed, dataset, symbol, resolution, min_ts, max_ts, rows_fetched, last_run_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, feed, dataset, symbol, resolution)
        DO UPDATE SET min_ts=excluded.min_ts, max_ts=excluded.max_ts,
                      rows_fetched=coverage.rows_fetched + excluded.rows_fetched,
                      last_run_at=excluded.last_run_at
        """,
        (provider, feed, dataset, symbol, resolution, min_ts, max_ts, rows_fetched, run_at),
    )
    conn.commit()


def coverage_report(conn: sqlite3.Connection, symbol: Optional[str] = None) -> list[dict]:
    sql = "SELECT provider, feed, dataset, symbol, resolution, min_ts, max_ts, rows_fetched, last_run_at FROM coverage"
    params: tuple = ()
    if symbol:
        # Also surface option-contract coverage rows (symbol stored as the
        # contract, e.g. "AAPL  261125C00270000") nested under the underlying.
        sql += " WHERE symbol = ? OR symbol LIKE ?"
        params = (symbol, f"{symbol}%")
    sql += " ORDER BY symbol, dataset, provider, feed, resolution"
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
