"""Pool routes, chain-aware."""

import logging
from fastapi import APIRouter, HTTPException, Depends
from web3 import Web3

from models.schemas import SupplyRequest, WithdrawRequest, PoolStatsResponse
from core.web3_utils import get_receipt
from services import chain_reader
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.pool")
router = APIRouter()


@router.post("/pool/supply")
async def supply(req: SupplyRequest, chain: str = Depends(chain_param)):
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")
    wallet = Web3.to_checksum_address(req.wallet_address)
    bal = chain_reader.cgusdc_balance(chain, wallet)
    return {"chain": chain, "message": "Supply confirmed", "wallet_address": wallet, **bal, "tx_hash": req.tx_hash}


@router.post("/pool/withdraw")
async def withdraw(req: WithdrawRequest, chain: str = Depends(chain_param)):
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")
    wallet = Web3.to_checksum_address(req.wallet_address)
    bal = chain_reader.cgusdc_balance(chain, wallet)
    return {"chain": chain, "message": "Withdrawal confirmed", "wallet_address": wallet, **bal, "tx_hash": req.tx_hash}


@router.get("/pool/stats")
async def stats(chain: str = Depends(chain_param)):
    s = chain_reader.pool_stats(chain)
    if not s:
        raise HTTPException(status_code=503, detail=f"Pool stats unavailable on {chain}")
    s["chain"] = chain
    return s


@router.get("/pool/balance/{wallet_address}")
async def balance(wallet_address: str, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(wallet_address)
    bal = chain_reader.cgusdc_balance(chain, wallet)
    bal["chain"] = chain
    bal["wallet_address"] = wallet
    return bal


@router.get("/pool/rates")
async def rates(chain: str = Depends(chain_param)):
    curves = chain_reader.rate_curves(chain)
    curves["chain"] = chain
    return curves


@router.get("/pool/apr-preview")
async def apr_preview(tier: int, utilization_bps: int = 0, chain: str = Depends(chain_param)):
    if tier < 1 or tier > 5:
        raise HTTPException(status_code=400, detail="tier must be 1..5")
    apr = chain_reader.borrow_apr(chain, tier, utilization_bps)
    return {"chain": chain, "tier": tier, "utilization_bps": utilization_bps, "borrow_apr_bps": apr}