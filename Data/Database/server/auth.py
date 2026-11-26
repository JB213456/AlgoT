from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def _valid_keys() -> set[str]:
    raw = os.environ.get("MARKET_DATA_DB_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """Validate the X-API-Key header against MARKET_DATA_DB_API_KEYS (comma-separated).

    Constant-time comparison avoids leaking key length/prefix via timing. If no keys
    are configured, the server refuses to start serving write-capable routes rather
    than silently running open (see app.py startup check).
    """
    keys = _valid_keys()
    if x_api_key is None or not any(hmac.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing X-API-Key")
    return x_api_key
