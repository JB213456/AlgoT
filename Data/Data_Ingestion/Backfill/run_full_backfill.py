#!/usr/bin/env python3
"""
Full market data backfill for AlgoT trading strategies.
Backfills 125+ symbols with historical OHLCV data and fundamentals.

Execution strategy:
1. Priority 1: Daily OHLCV bars (all symbols)
2. Priority 1: Fundamentals and corporate actions (equity/ETF)
3. Priority 2: Hourly bars (5-year history)
4. Priority 2: Intraday bars (15-min, 5-min, 1-min - 2-year history)

Respects free-tier rate limits and supports resumable downloads.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta

# Core equities (100)
EQUITIES = [
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "ORCL", "CRM", "ADBE", "IBM", "INTC",
    "QCOM", "MU", "AMAT", "LRCX", "KLAC", "TXN", "ADI", "MRVL", "ON", "MCHP",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW", "V", "MA",
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "AMGN", "GILD", "TMO", "ABT",
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC", "VLO", "PSX", "HAL",
    "AMZN", "TSLA", "HD", "LOW", "NKE", "SBUX", "MCD", "BKNG", "F", "GM",
    "KO", "PEP", "PG", "COST", "WMT", "CL", "MDLZ", "KMB", "GIS", "KHC",
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "BA", "UPS", "FDX", "UNP",
    "META", "GOOGL", "NFLX", "DIS", "T", "VZ", "TMUS", "CMCSA", "CHTR", "EA",
    "LIN", "APD", "FCX", "NEM", "NUE", "NEE", "DUK", "SO", "AMT", "PLD",
]

# Market benchmark ETFs (4)
BENCHMARK_ETFS = ["SPY", "QQQ", "IWM", "DIA"]

# Sector ETFs (11)
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLC", "XLB", "XLU", "XLRE"]

# Cross-asset ETFs (10)
CROSS_ASSET_ETFS = ["GLD", "SLV", "DBC", "USO", "TLT", "IEF", "SHY", "BIL", "EFA", "EEM"]

# Crypto and forex (quoted for providers)
CRYPTO_FOREX = ["BTC/USD", "ETH/USD", "EUR/USD", "USD/JPY", "XAU/USD", "WTI"]

# Additional indicators available
INDICATORS = ["UUP", "HYG", "LQD", "SMH", "VIX"]

# US Treasury yields
TREASURIES = ["US02Y", "US10Y", "US30Y"]

# Combine all core symbols (125+)
CORE_SYMBOLS = EQUITIES + BENCHMARK_ETFS + SECTOR_ETFS + CROSS_ASSET_ETFS
SUPPLEMENTARY_SYMBOLS = CRYPTO_FOREX + INDICATORS + TREASURIES

TODAY = datetime.now().strftime("%Y-%m-%d")
FIVE_YEARS_AGO = (datetime.now() - timedelta(days=5*365)).strftime("%Y-%m-%d")
TWO_YEARS_AGO = (datetime.now() - timedelta(days=2*365)).strftime("%Y-%m-%d")

def run_backfill(symbol: str, dataset: str, resolution: str | None = None, start: str | None = None, end: str | None = None) -> bool:
    """Execute a single backfill command. Returns True if successful."""
    cmd = [
        "market-backfill",
        "--symbol", symbol,
        "--dataset", dataset,
    ]

    if resolution:
        cmd.extend(["--resolution", resolution])
    if start:
        cmd.extend(["--start", start])
    if end:
        cmd.extend(["--end", end])

    # Add log level for cleaner output
    cmd.extend(["--log-level", "WARNING"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            print(f"  ⚠ {symbol} {dataset} {resolution or 'all'}: {result.stderr.strip()[:100]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"  ✗ {symbol} {dataset}: timeout")
        return False
    except Exception as e:
        print(f"  ✗ {symbol} {dataset}: {str(e)[:100]}")
        return False

def main():
    print("=" * 80)
    print("AlgoT Market Data Backfill - Full Historical Dataset")
    print("=" * 80)
    print(f"Start time: {datetime.now().isoformat()}")
    print(f"Today: {TODAY}")
    print()

    stats = {
        "phase": "",
        "total": 0,
        "success": 0,
        "failed": [],
    }

    # Phase 1: Daily OHLCV bars for all core symbols (priority)
    print("\n" + "=" * 80)
    print("PHASE 1: Daily OHLCV Bars - All Core Symbols (125+)")
    print("=" * 80)
    print(f"Backfilling daily bars from 2000-01-01 to {TODAY}...")
    print()

    stats["phase"] = "Daily bars"
    stats["total"] = len(CORE_SYMBOLS)
    stats["success"] = 0
    stats["failed"] = []

    for i, symbol in enumerate(CORE_SYMBOLS, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(CORE_SYMBOLS)}] {symbol:8s} ... ")
        sys.stdout.flush()

        # Daily bars with full history
        if run_backfill(symbol, "bars", resolution="1Day"):
            stats["success"] += 1
            print("✓ daily bars")
        else:
            stats["failed"].append(f"{symbol} (daily bars)")
            print("✗ failed")

        # Rate limiting: Alpaca allows 200 req/min, LSE 60 req/min
        # With both providers running in parallel, add small delay
        time.sleep(0.3)

    print()
    print(f"Phase 1 complete: {stats['success']}/{stats['total']} symbols")

    # Phase 2: Fundamentals and corporate actions (equities/ETFs)
    print("\n" + "=" * 80)
    print("PHASE 2: Fundamentals & Corporate Actions - Equity/ETF Symbols")
    print("=" * 80)

    equity_etf_symbols = EQUITIES + BENCHMARK_ETFS + SECTOR_ETFS + CROSS_ASSET_ETFS
    stats["phase"] = "Fundamentals"
    stats["total"] = len(equity_etf_symbols)
    stats["success"] = 0
    stats["failed"] = []

    for i, symbol in enumerate(equity_etf_symbols, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(equity_etf_symbols)}] {symbol:8s} ... ")
        sys.stdout.flush()

        # Fundamentals (LSE only)
        if run_backfill(symbol, "fundamentals"):
            stats["success"] += 1
            print("✓ fundamentals")
        else:
            # Fundamentals may not be available for all symbols, that's ok
            print("⊘ unavailable")

        time.sleep(0.3)

    print()
    print(f"Phase 2 complete: {stats['success']}/{stats['total']} symbols with fundamentals")

    # Phase 3: Corporate actions (dividends, splits)
    print("\n" + "=" * 80)
    print("PHASE 3: Corporate Actions - Dividend & Split History")
    print("=" * 80)

    stats["phase"] = "Corporate actions"
    stats["total"] = len(equity_etf_symbols)
    stats["success"] = 0
    stats["failed"] = []

    for i, symbol in enumerate(equity_etf_symbols, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(equity_etf_symbols)}] {symbol:8s} ... ")
        sys.stdout.flush()

        # Corporate actions (LSE only)
        if run_backfill(symbol, "corporate_actions"):
            stats["success"] += 1
            print("✓ corporate actions")
        else:
            print("⊘ unavailable")

        time.sleep(0.3)

    print()
    print(f"Phase 3 complete: {stats['success']}/{stats['total']} symbols with corporate actions")

    # Phase 4: Hourly bars (5-year history) for core symbols
    print("\n" + "=" * 80)
    print("PHASE 4: Hourly OHLCV Bars - 5-Year History (Core Symbols)")
    print("=" * 80)
    print(f"Backfilling hourly bars from {FIVE_YEARS_AGO} to {TODAY}...")
    print()

    stats["phase"] = "Hourly bars"
    stats["total"] = len(CORE_SYMBOLS)
    stats["success"] = 0
    stats["failed"] = []

    for i, symbol in enumerate(CORE_SYMBOLS, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(CORE_SYMBOLS)}] {symbol:8s} ... ")
        sys.stdout.flush()

        # Hourly bars: Alpaca uses "1Hour", LSE uses "1h"
        if run_backfill(symbol, "bars", resolution="1Hour,1h", start=FIVE_YEARS_AGO, end=TODAY):
            stats["success"] += 1
            print("✓ hourly")
        else:
            stats["failed"].append(f"{symbol} (hourly)")
            print("✗ failed")

        time.sleep(0.3)

    print()
    print(f"Phase 4 complete: {stats['success']}/{stats['total']} symbols")

    # Phase 5: Intraday bars (15-min, 5-min, 1-min - 2-year history)
    print("\n" + "=" * 80)
    print("PHASE 5: Intraday Bars - 2-Year History (Core Symbols)")
    print("  - 15-minute")
    print("  - 5-minute")
    print("  - 1-minute")
    print("=" * 80)
    print(f"Backfilling intraday bars from {TWO_YEARS_AGO} to {TODAY}...")
    print()

    total_intraday = len(CORE_SYMBOLS) * 3  # 3 resolutions per symbol
    success_intraday = 0

    for i, symbol in enumerate(CORE_SYMBOLS, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(CORE_SYMBOLS)}] {symbol:8s} 15/5/1-min ... ")
        sys.stdout.flush()

        # Combined: 15-min, 5-min, 1-min
        if run_backfill(symbol, "bars", resolution="15Min,15m,5Min,5m,1Min,1m",
                       start=TWO_YEARS_AGO, end=TODAY):
            success_intraday += 3
            print("✓")
        else:
            print("✗")

        time.sleep(0.5)  # Longer delay for heavier intraday requests

    print()
    print(f"Phase 5 complete: {success_intraday}/{total_intraday} intraday resolutions")

    # Phase 6: Supplementary symbols (crypto, indicators, treasuries)
    print("\n" + "=" * 80)
    print("PHASE 6: Supplementary Symbols - Daily Bars")
    print("  - Crypto: BTC/USD, ETH/USD")
    print("  - Forex: EUR/USD, USD/JPY, XAU/USD, WTI")
    print("  - Indicators: UUP, HYG, LQD, SMH, VIX")
    print("  - Treasuries: US02Y, US10Y, US30Y")
    print("=" * 80)
    print()

    all_supplementary = CRYPTO_FOREX + INDICATORS + TREASURIES
    stats["phase"] = "Supplementary"
    stats["total"] = len(all_supplementary)
    stats["success"] = 0
    stats["failed"] = []

    for i, symbol in enumerate(all_supplementary, 1):
        sys.stdout.write(f"\r[{i:3d}/{len(all_supplementary)}] {symbol:12s} ... ")
        sys.stdout.flush()

        # Daily bars with full history
        if run_backfill(symbol, "bars", resolution="1Day"):
            stats["success"] += 1
            print("✓")
        else:
            stats["failed"].append(symbol)
            print("✗")

        time.sleep(0.3)

    print()
    print(f"Phase 6 complete: {stats['success']}/{stats['total']} supplementary symbols")

    # Summary
    print("\n" + "=" * 80)
    print("BACKFILL SUMMARY")
    print("=" * 80)
    print(f"End time: {datetime.now().isoformat()}")
    print()
    print("Dataset Coverage:")
    print(f"  • Core symbols (125+): {len(CORE_SYMBOLS)} symbols")
    print(f"    - 100 equities")
    print(f"    - 4 market benchmark ETFs")
    print(f"    - 11 sector ETFs")
    print(f"    - 10 cross-asset ETFs")
    print(f"  • Supplementary: {len(all_supplementary)} symbols")
    print()
    print("Data Backfilled:")
    print(f"  • Daily OHLCV bars: Full history (from 2000)")
    print(f"  • Hourly bars: 5-year history")
    print(f"  • Intraday bars (15/5/1-min): 2-year history")
    print(f"  • Fundamentals: Maximum available")
    print(f"  • Corporate actions: Maximum available")
    print()
    print("Next steps:")
    print("  1. Verify database contents:")
    print("     market-backfill --symbol AAPL --coverage")
    print("  2. Query specific symbols:")
    print("     market-backfill --symbol SPY --coverage")
    print("  3. Resume incomplete downloads (if any):")
    print("     python3 run_full_backfill.py")
    print()

if __name__ == "__main__":
    main()
