"""
LendingPool routes: ERC-4626 supply/withdraw verification + pool stats + rate curves.
"""

import logging
from fastapi import APIRouter, HTTPException
from web3 import Web3

from models.schemas import SupplyRequest, WithdrawRequest, PoolStatsResponse
from core.web3_utils import get_receipt
from services import chain_reader

log = logging.getLogger("creditgraph.routes.pool")
router = APIRouter()


@router.post("/pool/supply")
async def supply(req: SupplyRequest):
    """User has called LendingPool.deposit via FE. Verify receipt."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    wallet = Web3.to_checksum_address(req.wallet_address)
    bal = await chain_reader.a_cgusdc_balance(wallet)
    return {
        "message": "Supply confirmed",
        "wallet_address": wallet,
        "cgusdc_shares": bal["shares"],
        "cgusdc_assets_usdc": bal["assets_usdc"],
        "tx_hash": req.tx_hash,
    }


@router.post("/pool/withdraw")
async def withdraw(req: WithdrawRequest):
    """User has called LendingPool.withdraw via FE. Verify receipt."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    wallet = Web3.to_checksum_address(req.wallet_address)
    bal = await chain_reader.a_cgusdc_balance(wallet)
    return {
        "message": "Withdrawal confirmed",
        "wallet_address": wallet,
        "cgusdc_shares": bal["shares"],
        "cgusdc_assets_usdc": bal["assets_usdc"],
        "tx_hash": req.tx_hash,
    }


@router.get("/pool/stats", response_model=PoolStatsResponse)
async def stats():
    s = await chain_reader.a_pool_stats()
    if not s:
        raise HTTPException(status_code=503, detail="Pool stats unavailable")
    return s


@router.get("/pool/balance/{wallet_address}")
async def balance(wallet_address: str):
    wallet = Web3.to_checksum_address(wallet_address)
    bal = await chain_reader.a_cgusdc_balance(wallet)
    bal["wallet_address"] = wallet
    return bal


@router.get("/pool/rates")
async def rates():
    """Full rate-curve snapshot — for the Market tab visualization."""
    curves = await chain_reader.a_rate_curves()
    return curves


@router.get("/pool/apr-preview")
async def apr_preview(tier: int, utilization_bps: int = 0):
    """Live APR for a hypothetical borrow at this tier + utilization."""
    if tier < 1 or tier > 5:
        raise HTTPException(status_code=400, detail="tier must be 1..5")
    apr = await chain_reader.a_borrow_apr(tier, utilization_bps)
    return {"tier": tier, "utilization_bps": utilization_bps, "borrow_apr_bps": apr}