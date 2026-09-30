"""
x402 routes. Two flavors:
  - 'Real' channel: FE opens + funds via MetaMask, we verify
  - 'Demo' channel: BE opens a channel between two agents to showcase
    agent-to-agent micropayments. This is what makes the agentic-prize
    submission concrete.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

from eth_account.messages import encode_defunct
from web3 import Web3

from models.schemas import OpenChannelRequest, SettleChannelRequest, CloseChannelRequest
from core.contracts import w3, contracts, admin_signer, agent_signers, send_tx, get
from core.web3_utils import get_receipt, find_event
from core.database import channels_collection, receipts_collection

import asyncio

log = logging.getLogger("creditgraph.routes.x402")
router = APIRouter()


@router.post("/x402/open")
async def open_channel(req: OpenChannelRequest):
    """FE opened a channel via MetaMask; we verify and persist."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    ev = find_event("X402PaymentRouter", "ChannelOpened", receipt)
    if not ev:
        raise HTTPException(status_code=400, detail="No ChannelOpened event in receipt")

    doc = {
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
    await channels_collection.replace_one({"channel_id": doc["channel_id"]}, doc, upsert=True)
    return doc


@router.post("/x402/demo-open")
async def demo_open():
    """
    Open a demo channel from agent[0] (acting as Underwriter) to agent[1]
    (acting as Data Collector). Funds it with 1 USDC. Requires agent[0] to
    have ETH + USDC. Use this on the FE to flash 'Agents are paying each other!'.
    """
    if len(agent_signers) < 2:
        raise HTTPException(status_code=503, detail="Need at least 2 agent signers")

    payer = agent_signers[0]
    payee = agent_signers[1]
    deposit_units = int(1 * 1e6)  # 1 USDC
    duration = 60 * 60  # 1 hour

    usdc = contracts["USDC"]
    router_addr = contracts["X402PaymentRouter"].address

    # 1. Payer approves the router for the deposit
    approve_fn = usdc.functions.approve(router_addr, deposit_units)
    await asyncio.to_thread(send_tx ,approve_fn, payer)

    # 2. Open channel
    open_fn = contracts["X402PaymentRouter"].functions.openChannel(
        payee.address, deposit_units, duration
    )
    result = await asyncio.to_thread(send_tx ,open_fn, payer)
    ev = find_event("X402PaymentRouter", "ChannelOpened", result["receipt"])
    channel_id = "0x" + ev["channelId"].hex()

    doc = {
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
    await channels_collection.replace_one({"channel_id": channel_id}, doc, upsert=True)
    return doc


@router.post("/x402/demo-settle")
async def demo_settle(req: SettleChannelRequest):
    """
    Payer (agent[0]) signs a cumulative voucher off-chain; payee (agent[1])
    submits it on-chain to claim. This is the heart of x402 — one tx claims all
    accumulated payments to date.
    """
    doc = await channels_collection.find_one({"channel_id": req.channel_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not doc.get("is_demo"):
        raise HTTPException(status_code=400, detail="Use /x402/settle for non-demo channels")

    payer = next((a for a in agent_signers if a.address == doc["payer"]), None)
    payee = next((a for a in agent_signers if a.address == doc["payee"]), None)
    if not payer or not payee:
        raise HTTPException(status_code=500, detail="Demo signers not found in agent set")

    router_contract = contracts["X402PaymentRouter"]
    chain_id = w3.eth.chain_id
    channel_id_bytes = Web3.to_bytes(hexstr=req.channel_id)

    # Voucher = keccak(router_address || chainid || channel_id || cumulative_amount), eth-signed
    voucher = Web3.solidity_keccak(
        ["address", "uint256", "bytes32", "uint256"],
        [router_contract.address, chain_id, channel_id_bytes, req.cumulative_amount],
    )
    signable = encode_defunct(voucher)
    signed = payer.sign_message(signable)
    sig = signed.signature

    # Payee submits
    fn = router_contract.functions.settle(channel_id_bytes, req.cumulative_amount, sig)
    result = await asyncio.to_thread(send_tx ,fn, payee)

    new_claimed = req.cumulative_amount / 1e6
    await channels_collection.update_one(
        {"channel_id": req.channel_id},
        {
            "$set": {
                "claimed_usdc": new_claimed,
                "last_settle_tx": result["tx_hash"],
                "last_settle_at": datetime.utcnow(),
            }
        },
    )
    return {
        "channel_id": req.channel_id,
        "cumulative_amount_usdc": new_claimed,
        "settle_tx": result["tx_hash"],
    }


@router.post("/x402/demo-record-receipt")
async def demo_record_receipt(channel_id: str, data_hash: str = "0x" + "00" * 32, amount: int = 100000):
    """Record an off-chain data-delivery receipt on-chain for audit. Optional but neat for demo."""
    doc = await channels_collection.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Channel not found")
    payer = next((a for a in agent_signers if a.address == doc["payer"]), None)
    if not payer:
        raise HTTPException(status_code=500, detail="Demo payer not found")

    channel_id_bytes = Web3.to_bytes(hexstr=channel_id)
    data_hash_bytes = Web3.to_bytes(hexstr=data_hash)
    fn = contracts["X402PaymentRouter"].functions.recordReceipt(
        channel_id_bytes, data_hash_bytes, amount
    )
    result = await asyncio.to_thread(send_tx ,fn, payer)
    await receipts_collection.insert_one({
        "channel_id": channel_id,
        "data_hash": data_hash,
        "amount_usdc": amount / 1e6,
        "tx_hash": result["tx_hash"],
        "recorded_at": datetime.utcnow(),
    })
    return {"tx_hash": result["tx_hash"]}


@router.post("/x402/close")
async def close_channel(req: CloseChannelRequest):
    """FE-initiated close. We verify and update."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    await channels_collection.update_one(
        {"channel_id": req.channel_id},
        {"$set": {"open": False, "close_tx": req.tx_hash, "closed_at": datetime.utcnow()}},
    )
    return {"channel_id": req.channel_id, "tx_hash": req.tx_hash}


@router.get("/x402/channels")
async def list_channels():
    cursor = channels_collection.find().sort("opened_at", -1).limit(20)
    docs = await cursor.to_list(length=20)
    for d in docs:
        d.pop("_id", None)
    return {"channels": docs}