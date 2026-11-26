"""Basic sanity validation for incoming bar records before they're persisted."""
from __future__ import annotations

import logging

from .models import Bar

logger = logging.getLogger(__name__)


def is_valid_bar(bar: Bar) -> bool:
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
        return False
    if bar.volume < 0:
        return False
    if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
        return False
    if bar.low > bar.high:
        return False
    return True


def filter_valid_bars(bars: list[Bar]) -> list[Bar]:
    valid = []
    for bar in bars:
        if is_valid_bar(bar):
            valid.append(bar)
        else:
            logger.warning("dropping invalid bar: %s", bar)
    return valid
