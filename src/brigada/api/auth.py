"""API key authentication shared by every route that touches restaurant data."""

import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from brigada.config import Settings, get_settings

logger = logging.getLogger(__name__)


def get_allowed_restaurants(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> frozenset[int]:
    """Resolve the caller's API key to the restaurant ids it may query. Fails closed."""
    if x_api_key:
        for key, restaurant_ids in settings.api_key_scopes().items():
            if hmac.compare_digest(key.encode(), x_api_key.encode()):
                return restaurant_ids
    # Nothing of the key itself is logged: a mistyped real key would leak part of it.
    logger.warning("rejected API key (sent=%s, length=%d)", bool(x_api_key), len(x_api_key or ""))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid X-API-Key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def require_restaurant(restaurant_id: int, allowed: frozenset[int]) -> None:
    if restaurant_id not in allowed:
        logger.warning(
            "API key not allowed for restaurant %s (allowed: %s)", restaurant_id, sorted(allowed)
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key not allowed for this restaurant"
        )


def get_key_id(
    _allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """A short, non-reversible id of the authenticated key, for the audit trail.

    Depends on ``get_allowed_restaurants``, so it only exists for a key that was accepted.
    """
    return "key:" + hashlib.sha256((x_api_key or "").encode()).hexdigest()[:8]
