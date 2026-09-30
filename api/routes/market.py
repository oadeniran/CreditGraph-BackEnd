"""Protocol-level reads for a Market / Network tab."""

import logging
from fastapi import APIRouter

from services import chain_reader

log = logging.getLogger("creditgraph.routes.market")
router = APIRouter()


@router.get("/market/overview")
async def overview():
    return {
        "pool": await chain_reader.a_pool_stats(),
        "insurance": await chain_reader.a_insurance_state(),
        "treasury": await chain_reader.a_treasury_state(),
        "rate_curves": await chain_reader.a_rate_curves(),
        "tier_base_limits_usdc": await chain_reader.a_tier_base_limits(),
        "grace_period_seconds": await chain_reader.a_grace_period(),
    }


@router.get("/market/insurance")
async def insurance():
    return await chain_reader.a_insurance_state()


@router.get("/market/treasury")
async def treasury():
    return await chain_reader.a_treasury_state()