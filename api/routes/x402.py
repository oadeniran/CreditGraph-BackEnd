"""x402 routes, chain-aware."""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends

from eth_account.messages import encode_defunct
from web3 import Web3

from models.schemas import OpenChannelRequest, SettleChannelRequest, CloseChannelRequest
from core.contracts import get_w3, get_contract, admin_signer, agent_signers, send_tx
from core.web3_utils import get_receipt, find_event
from core.database import channels_collection, receipts_collection
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.x402")
router = APIRouter()


@router.post("/x402/open")
async def open_channel(req: OpenChannelRequest, chain: str = Depends(chain_param)):
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    ev = find_event(chain, "X402PaymentRouter", "ChannelOpened", receipt)
    if not ev:
        raise HTTPException(status_code=400, detail="No ChannelOpened event")

    doc = {
        "chain": chain,
        "channel_id": "0x" + ev["channelId"].hex(),
        "payer": ev["payer"],
        "payee": ev["payee"],
        "deposit_usdc": int(ev["deposit"]) / 1e6,
        "expires_at": int(ev["expiresAt"]),
        "claimed_usdc": 0.0,
        "open": True,
        "open_tx": req.tx_hash,
        "opened_at": datetime.utcnow(),
    }
    await channels_collection.replace_one({"chain": chain, "channel_id": doc["channel_id"]}, doc, upsert=True)
    return doc


@router.post("/x402/demo-open")
async def demo_open(chain: str = Depends(chain_param)):
    if len(agent_signers) < 2:
        raise HTTPException(status_code=503, detail="Need at least 2 agents")

    payer = agent_signers[0]
    payee = agent_signers[1]
    deposit_units = int(1 * 1e6)
    duration = 60 * 60

    usdc = get_contract(chain, "USDC")
    router_addr = get_contract(chain, "X402PaymentRouter").address

    await asyncio.to_thread(send_tx, chain, usdc.functions.approve(router_addr, deposit_units), payer)
    open_fn = get_contract(chain, "X402PaymentRouter").functions.openChannel(payee.address, deposit_units, duration)
    result = await asyncio.to_thread(send_tx, chain, open_fn, payer)
    ev = find_event(chain, "X402PaymentRouter", "ChannelOpened", result["receipt"])
    channel_id = "0x" + ev["channelId"].hex()

    doc = {
        "chain": chain,
        "channel_id": channel_id,
        "payer": payer.address,
        "payee": payee.address,
        "deposit_usdc": deposit_units / 1e6,
        "expires_at": int(ev["expiresAt"]),
        "claimed_usdc": 0.0,
        "open": True,
        "is_demo": True,
        "open_tx": result["tx_hash"],
        "opened_at": datetime.utcnow(),
    }
    await channels_collection.replace_one({"chain": chain, "channel_id": channel_id}, doc, upsert=True)
    return doc


@router.post("/x402/demo-settle")
async def demo_settle(req: SettleChannelRequest, chain: str = Depends(chain_param)):
    doc = await channels_collection.find_one({"chain": chain, "channel_id": req.channel_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not doc.get("is_demo"):
        raise HTTPException(status_code=400, detail="Use /x402/settle for non-demo channels")

    payer = next((a for a in agent_signers if a.address == doc["payer"]), None)
    payee = next((a for a in agent_signers if a.address == doc["payee"]), None)
    if not payer or not payee:
        raise HTTPException(status_code=500, detail="Demo signers not found")

    router_contract = get_contract(chain, "X402PaymentRouter")
    chain_id = get_w3(chain).eth.chain_id
    channel_id_bytes = Web3.to_bytes(hexstr=req.channel_id)

    voucher = Web3.solidity_keccak(
        ["address", "uint256", "bytes32", "uint256"],
        [router_contract.address, chain_id, channel_id_bytes, req.cumulative_amount],
    )
    signable = encode_defunct(voucher)
    signed = payer.sign_message(signable)
    sig = signed.signature

    fn = router_contract.functions.settle(channel_id_bytes, req.cumulative_amount, sig)
    result = await asyncio.to_thread(send_tx, chain, fn, payee)

    new_claimed = req.cumulative_amount / 1e6
    await channels_collection.update_one(
        {"chain": chain, "channel_id": req.channel_id},
        {"$set": {"claimed_usdc": new_claimed, "last_settle_tx": result["tx_hash"], "last_settle_at": datetime.utcnow()}},
    )
    return {"chain": chain, "channel_id": req.channel_id, "cumulative_amount_usdc": new_claimed, "settle_tx": result["tx_hash"]}


@router.post("/x402/close")
async def close_channel(req: CloseChannelRequest, chain: str = Depends(chain_param)):
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")
    await channels_collection.update_one(
        {"chain": chain, "channel_id": req.channel_id},
        {"$set": {"open": False, "close_tx": req.tx_hash, "closed_at": datetime.utcnow()}},
    )
    return {"chain": chain, "channel_id": req.channel_id, "tx_hash": req.tx_hash}


@router.get("/x402/channels")
async def list_channels(chain: str = Depends(chain_param)):
    cursor = channels_collection.find({"chain": chain}).sort("opened_at", -1).limit(20)
    docs = await cursor.to_list(length=20)
    for d in docs:
        d.pop("_id", None)
    return {"chain": chain, "channels": docs}