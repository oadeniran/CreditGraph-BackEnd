"""Chain discovery — tells the FE which chains the BE is configured for."""

import logging
from fastapi import APIRouter

from core.chains import list_chains, DEFAULT_CHAIN_KEY

log = logging.getLogger("creditgraph.routes.chains")
router = APIRouter()


@router.get("/chains")
async def get_chains():
    """Returns all chains the BE is configured to serve."""
    return {
        "default": DEFAULT_CHAIN_KEY,
        "chains": list_chains(),
    }