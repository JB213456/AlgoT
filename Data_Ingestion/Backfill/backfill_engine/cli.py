"""market-backfill CLI: historical data backfill from Alpaca and London
Strategic Edge free tiers into the local SQLite store.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from . import db
from .config import Settings
from .engine import EARLIEST_DEFAULT_START, BackfillEngine

ALL_PROVIDERS = ["alpaca", "lse"]
ALL_DATASETS = ["bars", "option_chain", "option_bars", "fundamentals", "corporate_actions"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-backfill", description="Historical market data backfill engine")
    parser.add_argument("--symbol", required=True, help='Instrument symbol, e.g. AAPL, SPY, "BTC/USD"')
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: earliest available)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--provider", default="all", help="alpaca | lse | all (default: all)")
    parser.add_argument("--dataset", default="all",
                         help="Comma list of: bars,option_chain,fundamentals,corporate_actions (default: all)")
    parser.add_argument("--resolution", default=None,
                         help="Comma list of resolutions to fetch (default: provider's full set)")
    parser.add_argument("--db-path", default=None, help="Override the SQLite database path")
    parser.add_argument("--env-file", default=None, help="Path to a .env file")
    parser.add_argument("--coverage", action="store_true", help="Print coverage report for --symbol and exit")
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL")
    return parser


def _resolve_providers(arg: str) -> list[str]:
    if arg == "all":
        return ALL_PROVIDERS
    requested = [p.strip() for p in arg.split(",") if p.strip()]
    unknown = set(requested) - set(ALL_PROVIDERS)
    if unknown:
        raise SystemExit(f"unknown provider(s): {unknown}, expected subset of {ALL_PROVIDERS}")
    return requested


def _resolve_datasets(arg: str) -> list[str]:
    if arg == "all":
        return ALL_DATASETS
    requested = [d.strip() for d in arg.split(",") if d.strip()]
    unknown = set(requested) - set(ALL_DATASETS)
    if unknown:
        raise SystemExit(f"unknown dataset(s): {unknown}, expected subset of {ALL_DATASETS}")
    return requested


def _print_coverage(settings: Settings, symbol: str) -> None:
    conn = db.connect(settings.db_path)
    rows = db.coverage_report(conn, symbol)
    conn.close()
    if not rows:
        print(f"No coverage recorded yet for {symbol}.")
        return
    print(f"Coverage for {symbol}:")
    for row in rows:
        print(f"  [{row['provider']}/{row['feed']}] {row['dataset']} {row['resolution']}: "
              f"{row['min_ts']} -> {row['max_ts']} ({row['rows_fetched']} rows, last run {row['last_run_at']})")


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env(env_file=args.env_file)
    if args.db_path:
        settings = Settings(**{**settings.__dict__, "db_path": args.db_path})
    if args.log_level:
        settings = Settings(**{**settings.__dict__, "log_level": args.log_level})

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.coverage:
        _print_coverage(settings, args.symbol)
        return 0

    if not settings.alpaca_configured and not settings.lse_configured:
        print("No provider credentials configured. Set ALPACA_API_KEY/ALPACA_SECRET_KEY and/or "
              "LSE_API_KEY (see .env.example).", file=sys.stderr)
        return 1

    providers = _resolve_providers(args.provider)
    datasets = _resolve_datasets(args.dataset)
    resolutions = [r.strip() for r in args.resolution.split(",")] if args.resolution else None
    start = args.start or EARLIEST_DEFAULT_START
    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    engine = BackfillEngine(settings)
    try:
        stats = await engine.run(args.symbol, providers, datasets, start, end, resolutions)
    finally:
        await engine.close()

    print(f"\nBackfill summary for {args.symbol} [{start} -> {end}]:")
    any_error = False
    for s in stats:
        status = f"ERROR: {s.error}" if s.error else f"{s.rows} rows"
        if s.error:
            any_error = True
        print(f"  [{s.provider}] {s.dataset} {s.resolution}: {status}")

    _print_coverage(settings, args.symbol)
    return 1 if any_error and all(s.error for s in stats) else 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
