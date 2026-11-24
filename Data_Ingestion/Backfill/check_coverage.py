#!/usr/bin/env python3
"""
Check database coverage for backfilled symbols.
Displays what data is available in the market data database.
"""

import subprocess
from datetime import datetime

SYMBOLS_TO_CHECK = [
    # Sample of core equities
    "AAPL", "MSFT", "NVDA", "TSLA", "JPM",
    # Sample of ETFs
    "SPY", "QQQ", "IWM", "XLK", "GLD",
    # Crypto
    "BTC/USD",
]

def check_symbol(symbol: str):
    """Check coverage for a single symbol."""
    cmd = ["market-backfill", "--symbol", symbol, "--coverage", "--log-level", "ERROR"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if lines:
                print(f"\n{symbol}:")
                for line in lines[1:]:  # Skip header
                    if line.strip():
                        print(f"  {line}")
        else:
            print(f"\n{symbol}: Error checking coverage")
    except subprocess.TimeoutExpired:
        print(f"\n{symbol}: Timeout")
    except Exception as e:
        print(f"\n{symbol}: {e}")

def main():
    print("=" * 80)
    print("Market Data Database Coverage Check")
    print("=" * 80)
    print(f"Time: {datetime.now().isoformat()}")
    print()

    for symbol in SYMBOLS_TO_CHECK:
        check_symbol(symbol)

    print("\n" + "=" * 80)
    print("Full coverage check complete")
    print()
    print("To check a specific symbol:")
    print("  market-backfill --symbol <SYMBOL> --coverage")
    print()

if __name__ == "__main__":
    main()
