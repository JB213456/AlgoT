#!/bin/bash
#
# Run market data backfill inside the algot-data-ingestion Docker container
# using docker exec directly (not docker compose exec)
#

set -e

CONTAINER="algot-data-ingestion"

echo "================================================================================"
echo "AlgoT Market Data Backfill - Docker Service (algot-data-ingestion)"
echo "================================================================================"
echo "Container: $CONTAINER"
echo "Start time: $(date)"
echo ""

# Phase 1: Daily OHLCV bars for core equities (100)
echo "PHASE 1: Daily OHLCV Bars - 100 Core Equities"
echo "================================================================================"

EQUITIES=(
  "AAPL" "MSFT" "NVDA" "AMD" "AVGO" "ORCL" "CRM" "ADBE" "IBM" "INTC"
  "QCOM" "MU" "AMAT" "LRCX" "KLAC" "TXN" "ADI" "MRVL" "ON" "MCHP"
  "JPM" "BAC" "GS" "MS" "WFC" "C" "BLK" "SCHW" "V" "MA"
  "UNH" "JNJ" "LLY" "ABBV" "MRK" "PFE" "AMGN" "GILD" "TMO" "ABT"
  "XOM" "CVX" "COP" "SLB" "EOG" "OXY" "MPC" "VLO" "PSX" "HAL"
  "AMZN" "TSLA" "HD" "LOW" "NKE" "SBUX" "MCD" "BKNG" "F" "GM"
  "KO" "PEP" "PG" "COST" "WMT" "CL" "MDLZ" "KMB" "GIS" "KHC"
  "CAT" "DE" "HON" "GE" "RTX" "LMT" "BA" "UPS" "FDX" "UNP"
  "META" "GOOGL" "NFLX" "DIS" "T" "VZ" "TMUS" "CMCSA" "CHTR" "EA"
  "LIN" "APD" "FCX" "NEM" "NUE" "NEE" "DUK" "SO" "AMT" "PLD"
)

count=0
for symbol in "${EQUITIES[@]}"; do
  count=$((count + 1))
  printf "[%3d/100] %-8s ... " "$count" "$symbol"
  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset bars --resolution 1Day --log-level WARNING >/dev/null 2>&1; then
    echo "✓"
  else
    echo "✗"
  fi
done

echo ""

# Phase 2: Market benchmark ETFs (4)
echo "PHASE 2: Daily OHLCV Bars - Market Benchmark ETFs"
echo "================================================================================"

BENCHMARK_ETFS=("SPY" "QQQ" "IWM" "DIA")

count=0
for symbol in "${BENCHMARK_ETFS[@]}"; do
  count=$((count + 1))
  printf "[%d/4] %-8s ... " "$count" "$symbol"
  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset bars --resolution 1Day --log-level WARNING >/dev/null 2>&1; then
    echo "✓"
  else
    echo "✗"
  fi
done

echo ""

# Phase 3: Sector ETFs (11)
echo "PHASE 3: Daily OHLCV Bars - Sector ETFs"
echo "================================================================================"

SECTOR_ETFS=("XLK" "XLF" "XLE" "XLV" "XLI" "XLY" "XLP" "XLC" "XLB" "XLU" "XLRE")

count=0
for symbol in "${SECTOR_ETFS[@]}"; do
  count=$((count + 1))
  printf "[%2d/11] %-8s ... " "$count" "$symbol"
  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset bars --resolution 1Day --log-level WARNING >/dev/null 2>&1; then
    echo "✓"
  else
    echo "✗"
  fi
done

echo ""

# Phase 4: Cross-asset ETFs (10)
echo "PHASE 4: Daily OHLCV Bars - Cross-Asset ETFs"
echo "================================================================================"

CROSS_ASSET_ETFS=("GLD" "SLV" "DBC" "USO" "TLT" "IEF" "SHY" "BIL" "EFA" "EEM")

count=0
for symbol in "${CROSS_ASSET_ETFS[@]}"; do
  count=$((count + 1))
  printf "[%2d/10] %-8s ... " "$count" "$symbol"
  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset bars --resolution 1Day --log-level WARNING >/dev/null 2>&1; then
    echo "✓"
  else
    echo "✗"
  fi
done

echo ""

# Phase 5: Fundamentals & Corporate Actions
echo "PHASE 5: Fundamentals & Corporate Actions - All Equity/ETF Symbols"
echo "================================================================================"

ALL_EQUITY_SYMBOLS=("${EQUITIES[@]}" "${BENCHMARK_ETFS[@]}" "${SECTOR_ETFS[@]}" "${CROSS_ASSET_ETFS[@]}")

count=0
for symbol in "${ALL_EQUITY_SYMBOLS[@]}"; do
  count=$((count + 1))
  total=$((${#EQUITIES[@]} + ${#BENCHMARK_ETFS[@]} + ${#SECTOR_ETFS[@]} + ${#CROSS_ASSET_ETFS[@]}))
  printf "[%3d/%3d] %-8s fundamentals/corporate_actions ... " "$count" "$total" "$symbol"

  fundamentals_ok=false
  corporate_actions_ok=false

  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset fundamentals --log-level WARNING >/dev/null 2>&1; then
    fundamentals_ok=true
  fi

  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset corporate_actions --log-level WARNING >/dev/null 2>&1; then
    corporate_actions_ok=true
  fi

  if $fundamentals_ok || $corporate_actions_ok; then
    echo "✓"
  else
    echo "⊘"
  fi
done

echo ""

# Phase 6: Crypto and Supplementary
echo "PHASE 6: Daily Bars - Crypto & Supplementary Symbols"
echo "================================================================================"

SUPPLEMENTARY=("BTC/USD" "ETH/USD" "EUR/USD" "USD/JPY" "XAU/USD" "WTI" "UUP" "HYG" "LQD" "SMH" "VIX" "US02Y" "US10Y" "US30Y")

count=0
for symbol in "${SUPPLEMENTARY[@]}"; do
  count=$((count + 1))
  printf "[%2d/14] %-12s ... " "$count" "$symbol"
  if docker exec "$CONTAINER" market-backfill --symbol "$symbol" --dataset bars --resolution 1Day --log-level WARNING >/dev/null 2>&1; then
    echo "✓"
  else
    echo "✗"
  fi
done

echo ""
echo "================================================================================"
echo "Backfill Complete"
echo "================================================================================"
echo "End time: $(date)"
echo ""
echo "Check coverage inside container:"
echo "  docker exec $CONTAINER market-backfill --symbol AAPL --coverage"
echo ""
