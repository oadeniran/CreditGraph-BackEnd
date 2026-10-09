"""Scoring routes, chain-aware."""

import logging
from fastapi import APIRouter, HTTPException, Depends
from web3 import Web3

from models.schemas import UnderwriteRequest, FinalizeScoreRequest, ChallengeScoreRequest
from services import chain_reader
from services.underwriter import underwrite, finalize_score, challenge_score
from services.score_reason import load_reason
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.scoring")
router = APIRouter()


@router.post("/score/underwrite")
async def trigger_underwrite(req: UnderwriteRequest, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(req.wallet_address)
    try:
        result = await underwrite(chain, wallet)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@router.post("/score/finalize")
async def finalize(req: FinalizeScoreRequest, chain: str = Depends(chain_param)):
    tx = finalize_score(chain, req.token_id)
    if not tx:
        raise HTTPException(
            status_code=400,
            detail="Nothing to finalize: no pending submission, already finalized, or window still open."
        )
    return {"chain": chain, "finalize_tx": tx}


@router.post("/score/challenge")
async def challenge(req: ChallengeScoreRequest, chain: str = Depends(chain_param)):
    tx = challenge_score(chain, req.token_id)
    if not tx:
        raise HTTPException(status_code=400, detail="Could not challenge")
    return {"chain": chain, "challenge_tx": tx}


@router.get("/score/pending/{token_id}")
async def get_pending(token_id: int, chain: str = Depends(chain_param)):
    pending = chain_reader.get_pending_score(chain, token_id)
    return {"chain": chain, "pending": pending}


@router.get("/score/reason/{reason_hash}")
async def get_reason(reason_hash: str):
    """Reasons are globally keyed by hash — no chain param needed."""
    if not reason_hash.startswith("0x"):
        reason_hash = "0x" + reason_hash
    payload = await load_reason(reason_hash.lower())
    if not payload:
        payload = await load_reason(reason_hash)
    if not payload:
        raise HTTPException(status_code=404, detail="Reason payload not found")
    return payload