"""Canonical <-> provider-specific symbol mapping.

The CLI accepts one canonical symbol (e.g. "AAPL", "BTC/USD"). Each provider
has its own conventions:

- Alpaca: equities as-is ("AAPL"); crypto as "BASE/QUOTE" ("BTC/USD").
- LSE (London Strategic Edge): equities are case-insensitive, used as-is;
  crypto pairs are concatenated against a quote currency, Tether-quoted by
  convention ("BTCUSDT" rather than "BTC/USD").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolKind:
    is_crypto: bool
    base: str
    quote: str = ""


def classify(symbol: str) -> SymbolKind:
    symbol = symbol.strip().upper()
    if "/" in symbol:
        base, _, quote = symbol.partition("/")
        return SymbolKind(is_crypto=True, base=base, quote=quote)
    return SymbolKind(is_crypto=False, base=symbol)


def to_alpaca_symbol(symbol: str) -> str:
    kind = classify(symbol)
    if kind.is_crypto:
        return f"{kind.base}/{kind.quote}"
    return kind.base


# LSE crypto pairs are quoted against USDT unless the caller already supplied
# an explicit, non-USD quote currency (e.g. "ETH/BTC").
_LSE_CRYPTO_QUOTE_FALLBACK = {"USD": "USDT"}


def to_lse_symbol(symbol: str) -> str:
    kind = classify(symbol)
    if kind.is_crypto:
        quote = _LSE_CRYPTO_QUOTE_FALLBACK.get(kind.quote, kind.quote)
        return f"{kind.base}{quote}"
    return kind.base
