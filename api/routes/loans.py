"""
Loan routes. The FE submits loan txs (originate, repay) via MetaMask directly
to LoanManager. After mining, the FE posts the tx hash here so we can:
  - Verify the tx really succeeded
  - Parse the on-chain event for the new loan_id / amounts
  - Write to Mongo so the History tab can paginate without re-reading chain
"""

import logging
import math
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from web3 import Web3

from models.schemas import BorrowRequest, RepayRequest, MarkLateRequest, MarkDefaultRequest
from core.contracts import contracts, admin_signer, send_tx
from core.web3_utils import get_receipt, find_event, find_all_events
from core.database import loans_collection
import asyncio
from services import chain_reader

log = logging.getLogger("creditgraph.routes.loans")
router = APIRouter()


async def _index_loan_originated(wallet: str, args: dict, tx_hash: str) -> dict:
    """Persist a LoanOriginated event into Mongo."""
    loan_id = int(args["loanId"])
    token_id = int(args["tokenId"])
    amount_units = int(args["amount"])
    apr_bps = int(args["aprBps"])
    due_at = int(args["dueAt"])

    doc = {
        "loan_id_onchain": loan_id,
        "wallet_address": wallet,
        "token_id": token_id,
        "principal_usdc": amount_units / 1e6,
        "outstanding_usdc": amount_units / 1e6,
        "apr_bps": apr_bps,
        "originated_at": datetime.utcnow(),
        "due_at_ts": due_at,
        "state": "Active",
        "originate_tx": tx_hash,
    }
    await loans_collection.replace_one(
        {"loan_id_onchain": loan_id}, doc, upsert=True
    )
    return doc


async def _index_loan_repaid(args: dict, tx_hash: str) -> None:
    loan_id = int(args["loanId"])
    principal_paid = int(args["principalPaid"])
    interest_paid = int(args["interestPaid"])
    fully = bool(args["fullyRepaid"])

    existing = await loans_collection.find_one({"loan_id_onchain": loan_id})
    new_outstanding = (existing.get("outstanding_usdc", 0) - principal_paid / 1e6) if existing else 0
    new_outstanding = max(0.0, new_outstanding)

    await loans_collection.update_one(
        {"loan_id_onchain": loan_id},
        {
            "$set": {
                "outstanding_usdc": new_outstanding,
                "state": "Repaid" if fully else "Active",
                "last_repayment_tx": tx_hash,
                "last_repayment_at": datetime.utcnow(),
            },
            "$inc": {
                "interest_paid_usdc": interest_paid / 1e6,
            },
        },
    )


@router.post("/borrow")
async def borrow_funds(req: BorrowRequest):
    """
    FE has already called LoanManager.originate via MetaMask. We verify the
    receipt and parse the LoanOriginated event.
    """
    wallet = Web3.to_checksum_address(req.wallet_address)
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    ev = find_event("LoanManager", "LoanOriginated", receipt)
    if not ev:
        raise HTTPException(
            status_code=400,
            detail="No LoanOriginated event in this receipt — wrong tx_hash?",
        )

    doc = await _index_loan_originated(wallet, ev, req.tx_hash)
    return {
        "message": "Loan originated",
        "loan_id": doc["loan_id_onchain"],
        "principal_usdc": doc["principal_usdc"],
        "apr_bps": doc["apr_bps"],
        "due_at": doc["due_at_ts"],
    }


@router.post("/repay")
async def repay_loan(req: RepayRequest):
    """FE has called LoanManager.repay. Verify + index."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    repaid_events = find_all_events("LoanManager", "LoanRepaid", receipt)
    if not repaid_events:
        raise HTTPException(status_code=400, detail="No LoanRepaid event in this receipt")

    for ev in repaid_events:
        await _index_loan_repaid(ev, req.tx_hash)

    last = repaid_events[-1]
    return {
        "message": "Repayment recorded",
        "loan_id": int(last["loanId"]),
        "principal_paid_usdc": int(last["principalPaid"]) / 1e6,
        "interest_paid_usdc": int(last["interestPaid"]) / 1e6,
        "fully_repaid": bool(last["fullyRepaid"]),
    }


@router.post("/loan/mark-late")
async def mark_late(req: MarkLateRequest):
    """Permissionless poke — admin calls markLate on behalf of the protocol."""
    try:
        fn = contracts["LoanManager"].functions.markLate(req.loan_id)
        result = await asyncio.to_thread(send_tx ,fn, admin_signer)
        await loans_collection.update_one(
            {"loan_id_onchain": req.loan_id}, {"$set": {"state": "Late"}}
        )
        return {"tx_hash": result["tx_hash"], "loan_id": req.loan_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/loan/mark-default")
async def mark_default(req: MarkDefaultRequest):
    """Permissionless after grace period. Cascades into slash + insurance."""
    try:
        fn = contracts["LoanManager"].functions.markDefault(req.loan_id)
        result = await asyncio.to_thread(send_tx ,fn, admin_signer)
        await loans_collection.update_one(
            {"loan_id_onchain": req.loan_id},
            {"$set": {"state": "Defaulted", "defaulted_tx": result["tx_hash"]}},
        )
        return {"tx_hash": result["tx_hash"], "loan_id": req.loan_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/loan/{loan_id}")
async def get_loan(loan_id: int):
    """Live loan state from chain (includes accrued interest)."""
    loan = await chain_reader.a_get_loan(loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


@router.get("/user/{wallet_address}/loans")
async def get_loan_history(
    wallet_address: str,
    page: int = Query(1, ge=1),
    size: int = Query(5, ge=1, le=50),
):
    """Paginated loan history. Backed by indexed events + chain enrichment."""
    wallet = Web3.to_checksum_address(wallet_address)
    skip = (page - 1) * size

    total_count = await loans_collection.count_documents({"wallet_address": wallet})
    total_pages = max(1, math.ceil(total_count / size))

    cursor = (
        loans_collection.find({"wallet_address": wallet})
        .sort("originated_at", -1)
        .skip(skip)
        .limit(size)
    )
    docs = await cursor.to_list(length=size)

    # Enrich with live chain data so outstanding reflects accrued interest
    items = []
    for d in docs:
        on_chain = (
            await chain_reader.a_get_loan(d["loan_id_onchain"])
            if "loan_id_onchain" in d
            else None
        )
        items.append({
            "loan_id": d.get("loan_id_onchain"),
            "principal_usdc": d.get("principal_usdc", 0),
            "outstanding_usdc": (
                on_chain["outstanding_total_usdc"] if on_chain else d.get("outstanding_usdc", 0)
            ),
            "apr_bps": d.get("apr_bps", 0),
            "due_at": d.get("due_at_ts", 0),
            "state": (on_chain["state"] if on_chain else d.get("state", "Unknown")),
            "originated_at": d.get("originated_at"),
        })

    return {
        "items": items,
        "total_pages": total_pages,
        "current_page": page,
        "total": total_count,
    }