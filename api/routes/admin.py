"""Admin + faucet routes, chain-aware."""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Depends
from web3 import Web3

from models.schemas import FaucetRequest
from core.contracts import get_contract, admin_signer, send_tx
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.admin")
router = APIRouter()


@router.post("/faucet")
async def faucet(req: FaucetRequest, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(req.wallet_address)
    units = int(req.amount_usdc * 1e6)
    try:
        fn = get_contract(chain, "USDC").functions.mintTo(wallet, units)
        result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
        return {"chain": chain, "tx_hash": result["tx_hash"], "amount_usdc": req.amount_usdc, "to": wallet}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/fx/set")
async def set_fx_price(pair: str, value: int, decimals: int = 8, chain: str = Depends(chain_param)):
    pair_id = Web3.keccak(text=pair)
    try:
        fn = get_contract(chain, "DataOracleAdapter").functions.setPrice(pair_id, value, decimals)
        result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
        return {"chain": chain, "tx_hash": result["tx_hash"], "pair": pair, "value": value, "decimals": decimals}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/fx/{pair}")
async def get_fx_price(pair: str, chain: str = Depends(chain_param)):
    pair_id = Web3.keccak(text=pair)
    try:
        value, updated_at, decimals, exists = get_contract(chain, "DataOracleAdapter").functions.peek(pair_id).call()
        return {"chain": chain, "pair": pair, "exists": exists,
                "value": int(value) if exists else None,
                "updated_at": int(updated_at) if exists else None,
                "decimals": int(decimals) if exists else None}
    except Exception:
        return {"chain": chain, "pair": pair, "exists": False}


@router.post("/admin/insurance/seed")
async def seed_insurance(amount_usdc: float = 50.0, chain: str = Depends(chain_param)):
    units = int(amount_usdc * 1e6)
    try:
        usdc = get_contract(chain, "USDC")
        fund = get_contract(chain, "InsuranceFund")
        bal = usdc.functions.balanceOf(admin_signer.address).call()
        if bal < units:
            mint_fn = usdc.functions.mintTo(admin_signer.address, units)
            await asyncio.to_thread(send_tx, chain, mint_fn, admin_signer)
        approve_fn = usdc.functions.approve(fund.address, units)
        await asyncio.to_thread(send_tx, chain, approve_fn, admin_signer)
        fund_fn = fund.functions.fund(units)
        result = await asyncio.to_thread(send_tx, chain, fund_fn, admin_signer)
        return {"chain": chain, "tx_hash": result["tx_hash"], "amount_usdc": amount_usdc}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))