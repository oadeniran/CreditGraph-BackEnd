"""Admin/demo helpers: faucet, set FX, seed insurance fund, etc."""

import logging
from fastapi import APIRouter, HTTPException
from web3 import Web3

from models.schemas import FaucetRequest
from core.contracts import contracts, admin_signer, send_tx

import asyncio

log = logging.getLogger("creditgraph.routes.admin")
router = APIRouter()


@router.post("/faucet")
async def faucet(req: FaucetRequest):
    """Admin mints test USDC to a wallet via MockUSDC.mintTo (permissionless mock)."""
    wallet = Web3.to_checksum_address(req.wallet_address)
    units = int(req.amount_usdc * 1e6)
    try:
        fn = contracts["USDC"].functions.mintTo(wallet, units)
        result = await asyncio.to_thread(send_tx, fn, admin_signer)
        return {"tx_hash": result["tx_hash"], "amount_usdc": req.amount_usdc, "to": wallet}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/fx/set")
async def set_fx_price(pair: str, value: int, decimals: int = 8):
    """Push an FX price into DataOracleAdapter (requires CONFIG_ROLE on admin)."""
    pair_id = Web3.keccak(text=pair)
    try:
        fn = contracts["DataOracleAdapter"].functions.setPrice(pair_id, value, decimals)
        result = await asyncio.to_thread(send_tx, fn, admin_signer)
        return {
            "tx_hash": result["tx_hash"],
            "pair": pair,
            "pair_id": "0x" + pair_id.hex(),
            "value": value,
            "decimals": decimals,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/fx/{pair}")
async def get_fx_price(pair: str):
    """Read FX price (returns null if not set or stale)."""
    pair_id = Web3.keccak(text=pair)
    try:
        value, updated_at, decimals, exists = contracts["DataOracleAdapter"].functions.peek(pair_id).call()
        return {
            "pair": pair,
            "exists": exists,
            "value": int(value) if exists else None,
            "updated_at": int(updated_at) if exists else None,
            "decimals": int(decimals) if exists else None,
        }
    except Exception:
        return {"pair": pair, "exists": False}


@router.post("/admin/insurance/seed")
async def seed_insurance(amount_usdc: float = 50.0):
    """
    Admin funds the InsuranceFund with USDC. Useful so the demo default flow
    can actually cover losses.
    """
    units = int(amount_usdc * 1e6)
    try:
        usdc = contracts["USDC"]
        fund = contracts["InsuranceFund"]
        # Admin needs USDC; mint to self first if needed
        bal = usdc.functions.balanceOf(admin_signer.address).call()
        if bal < units:
            mint_fn = usdc.functions.mintTo(admin_signer.address, units)
            await asyncio.to_thread(send_tx, mint_fn, admin_signer)
        approve_fn = usdc.functions.approve(fund.address, units)
        await asyncio.to_thread(send_tx, approve_fn, admin_signer)
        fund_fn = fund.functions.fund(units)
        result = await asyncio.to_thread(send_tx, fund_fn, admin_signer)
        return {"tx_hash": result["tx_hash"], "amount_usdc": amount_usdc}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))