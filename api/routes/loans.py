"""Loan routes, chain-aware."""

import asyncio
import logging
import math
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
from web3 import Web3

from models.schemas import BorrowRequest, RepayRequest, MarkLateRequest, MarkDefaultRequest
from core.contracts import get_contract, admin_signer, send_tx
from core.web3_utils import get_receipt, find_event, find_all_events
from core.database import loans_collection
from services import chain_reader
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.loans")
router = APIRouter()


async def _index_loan_originated(chain_key: str, wallet: str, args: dict, tx_hash: str) -> dict:
    loan_id = int(args["loanId"])
    token_id = int(args["tokenId"])
    amount_units = int(args["amount"])
    apr_bps = int(args["aprBps"])
    due_at = int(args["dueAt"])

    doc = {
        "chain": chain_key,
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
        {"chain": chain_key, "loan_id_onchain": loan_id}, doc, upsert=True
    )
    return doc


async def _index_loan_repaid(chain_key: str, args: dict, tx_hash: str) -> None:
    loan_id = int(args["loanId"])
    principal_paid = int(args["principalPaid"])
    interest_paid = int(args["interestPaid"])
    fully = bool(args["fullyRepaid"])

    existing = await loans_collection.find_one({"chain": chain_key, "loan_id_onchain": loan_id})
    new_outstanding = (existing.get("outstanding_usdc", 0) - principal_paid / 1e6) if existing else 0
    new_outstanding = max(0.0, new_outstanding)

    await loans_collection.update_one(
        {"chain": chain_key, "loan_id_onchain": loan_id},
        {
            "$set": {
                "outstanding_usdc": new_outstanding,
                "state": "Repaid" if fully else "Active",
                "last_repayment_tx": tx_hash,
                "last_repayment_at": datetime.utcnow(),
            },
            "$inc": {"interest_paid_usdc": interest_paid / 1e6},
        },
    )


@router.post("/borrow")
async def borrow_funds(req: BorrowRequest, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(req.wallet_address)
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    ev = find_event(chain, "LoanManager", "LoanOriginated", receipt)
    if not ev:
        raise HTTPException(status_code=400, detail="No LoanOriginated event in receipt")

    doc = await _index_loan_originated(chain, wallet, ev, req.tx_hash)
    return {
        "chain": chain,
        "message": "Loan originated",
        "loan_id": doc["loan_id_onchain"],
        "principal_usdc": doc["principal_usdc"],
        "apr_bps": doc["apr_bps"],
        "due_at": doc["due_at_ts"],
    }


@router.post("/repay")
async def repay_loan(req: RepayRequest, chain: str = Depends(chain_param)):
    receipt = get_receipt(chain, req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    repaid_events = find_all_events(chain, "LoanManager", "LoanRepaid", receipt)
    if not repaid_events:
        raise HTTPException(status_code=400, detail="No LoanRepaid event in receipt")

    for ev in repaid_events:
        await _index_loan_repaid(chain, ev, req.tx_hash)

    last = repaid_events[-1]
    return {
        "chain": chain,
        "message": "Repayment recorded",
        "loan_id": int(last["loanId"]),
        "principal_paid_usdc": int(last["principalPaid"]) / 1e6,
        "interest_paid_usdc": int(last["interestPaid"]) / 1e6,
        "fully_repaid": bool(last["fullyRepaid"]),
    }


@router.post("/loan/mark-late")
async def mark_late(req: MarkLateRequest, chain: str = Depends(chain_param)):
    try:
        fn = get_contract(chain, "LoanManager").functions.markLate(req.loan_id)
        result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
        await loans_collection.update_one(
            {"chain": chain, "loan_id_onchain": req.loan_id}, {"$set": {"state": "Late"}}
        )
        return {"chain": chain, "tx_hash": result["tx_hash"], "loan_id": req.loan_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/loan/mark-default")
async def mark_default(req: MarkDefaultRequest, chain: str = Depends(chain_param)):
    try:
        fn = get_contract(chain, "LoanManager").functions.markDefault(req.loan_id)
        result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
        await loans_collection.update_one(
            {"chain": chain, "loan_id_onchain": req.loan_id},
            {"$set": {"state": "Defaulted", "defaulted_tx": result["tx_hash"]}},
        )
        return {"chain": chain, "tx_hash": result["tx_hash"], "loan_id": req.loan_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/loan/{loan_id}")
async def get_loan(loan_id: int, chain: str = Depends(chain_param)):
    loan = chain_reader.get_loan(chain, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    loan["chain"] = chain
    return loan


@router.get("/user/{wallet_address}/loans")
async def get_loan_history(
    wallet_address: str,
    chain: str = Depends(chain_param),
    page: int = Query(1, ge=1),
    size: int = Query(5, ge=1, le=50),
):
    wallet = Web3.to_checksum_address(wallet_address)
    skip = (page - 1) * size

    filter_q = {"chain": chain, "wallet_address": wallet}
    total_count = await loans_collection.count_documents(filter_q)
    total_pages = max(1, math.ceil(total_count / size))

    cursor = loans_collection.find(filter_q).sort("originated_at", -1).skip(skip).limit(size)
    docs = await cursor.to_list(length=size)

    items = []
    for d in docs:
        on_chain = chain_reader.get_loan(chain, d["loan_id_onchain"]) if "loan_id_onchain" in d else None
        items.append({
            "loan_id": d.get("loan_id_onchain"),
            "principal_usdc": d.get("principal_usdc", 0),
            "outstanding_usdc": on_chain["outstanding_total_usdc"] if on_chain else d.get("outstanding_usdc", 0),
            "apr_bps": d.get("apr_bps", 0),
            "due_at": d.get("due_at_ts", 0),
            "state": on_chain["state"] if on_chain else d.get("state", "Unknown"),
            "originated_at": d.get("originated_at"),
        })

    return {
        "chain": chain,
        "items": items,
        "total_pages": total_pages,
        "current_page": page,
        "total": total_count,
    }