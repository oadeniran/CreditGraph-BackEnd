"""Market routes, chain-aware."""

import logging
from fastapi import APIRouter, Depends

from services import chain_reader
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.market")
router = APIRouter()


@router.get("/market/overview")
async def overview(chain: str = Depends(chain_param)):
    return {
        "chain": chain,
        "pool": chain_reader.pool_stats(chain),
        "insurance": chain_reader.insurance_state(chain),
        "treasury": chain_reader.treasury_state(chain),
        "rate_curves": chain_reader.rate_curves(chain),
        "tier_base_limits_usdc": chain_reader.tier_base_limits(chain),
        "grace_period_seconds": chain_reader.grace_period(chain),
    }


@router.get("/market/insurance")
async def insurance(chain: str = Depends(chain_param)):
    s = chain_reader.insurance_state(chain)
    s["chain"] = chain
    return s


@router.get("/market/treasury")
async def treasury(chain: str = Depends(chain_param)):
    s = chain_reader.treasury_state(chain)
    s["chain"] = chain
    return s