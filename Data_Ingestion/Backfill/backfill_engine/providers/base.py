"""Common provider interface.

Every fetch_* method is an async generator yielding pages (lists) of
normalized model records, so the engine can persist incrementally and track
coverage as it goes instead of buffering an entire backfill in memory.

A provider that doesn't support a dataset for a given symbol should raise
`DatasetUnavailable` rather than crash the whole run -- the engine logs it
and moves on to the next dataset/provider.
"""
from __future__ import annotations

from typing import AsyncIterator, Optional, Sequence

from ..models import Bar, CorporateAction, FundamentalRecord, OptionBar, OptionContract, Quote, Trade


class DatasetUnavailable(RuntimeError):
    """Raised when a provider/feed has no access to a requested dataset (free-tier limit, 4xx)."""


class Provider:
    name: str = "base"

    async def close(self) -> None:
        raise NotImplementedError

    async def discover(self, symbol: str) -> dict:
        raise NotImplementedError

    def fetch_bars(self, symbol: str, resolution: str, start: str, end: str,
                    feed: Optional[str] = None) -> AsyncIterator[Sequence[Bar]]:
        raise NotImplementedError

    def fetch_trades(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Trade]]:
        raise NotImplementedError

    def fetch_quotes(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[Quote]]:
        raise NotImplementedError

    def fetch_option_chain(self, underlying: str) -> AsyncIterator[Sequence[OptionContract]]:
        raise NotImplementedError

    def fetch_option_bars(self, contract_symbol: str, resolution: str, start: str,
                            end: str) -> AsyncIterator[Sequence[OptionBar]]:
        raise NotImplementedError

    def fetch_fundamentals(self, symbol: str) -> AsyncIterator[Sequence[FundamentalRecord]]:
        raise NotImplementedError

    def fetch_corporate_actions(self, symbol: str, start: str, end: str) -> AsyncIterator[Sequence[CorporateAction]]:
        raise NotImplementedError
