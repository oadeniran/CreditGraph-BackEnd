"""
Shared route dependencies. The chain query param lives here so every route
uses the same validation + default.
"""

from fastapi import Query, HTTPException
from core.chains import is_configured, DEFAULT_CHAIN_KEY


def chain_param(chain: str = Query(DEFAULT_CHAIN_KEY, description="Chain key")) -> str:
    """Validates ?chain= and returns the chain key, defaulting to the server default."""
    if not is_configured(chain):
        raise HTTPException(
            status_code=400,
            detail=f"Chain '{chain}' is not configured. Hit /api/chains to see available chains.",
        )
    return chain