"""
Scoring routes: trigger underwriting, finalize pending submissions, challenge,
fetch the reason payload.
"""

import logging
from fastapi import APIRouter, HTTPException
from web3 import Web3

from models.schemas import UnderwriteRequest, FinalizeScoreRequest, ChallengeScoreRequest
from services import chain_reader
from services.underwriter import underwrite, finalize_score, challenge_score
from services.score_reason import load_reason

log = logging.getLogger("creditgraph.routes.scoring")
router = APIRouter()


@router.post("/score/underwrite")
async def trigger_underwrite(req: UnderwriteRequest):
    """Run the 3-agent quorum and submit a score for this wallet."""
    wallet = Web3.to_checksum_address(req.wallet_address)
    try:
        result = await underwrite(wallet)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@router.post("/score/finalize")
async def finalize(req: FinalizeScoreRequest):
    """Finalize a pending submission after its challenge window closes."""
    tx = finalize_score(req.token_id)
    if not tx:
        raise HTTPException(
            status_code=400,
            detail="Nothing to finalize: no pending submission, already finalized, or window still open."
        )
    return {"finalize_tx": tx}


@router.post("/score/challenge")
async def challenge(req: ChallengeScoreRequest):
    """Mark a pending submission as challenged (blocks finalization)."""
    tx = challenge_score(req.token_id)
    if not tx:
        raise HTTPException(status_code=400, detail="Could not challenge")
    return {"challenge_tx": tx}


@router.get("/score/pending/{token_id}")
async def get_pending(token_id: int):
    pending = await chain_reader.a_get_pending_score(token_id)
    if not pending:
        return {"pending": None}
    return {"pending": pending}


@router.get("/score/reason/{reason_hash}")
async def get_reason(reason_hash: str):
    """Fetch the human-readable reason payload that was hashed into the on-chain score."""
    if not reason_hash.startswith("0x"):
        reason_hash = "0x" + reason_hash
    payload = await load_reason(reason_hash.lower())
    if not payload:
        # Try without lowercasing
        payload = await load_reason(reason_hash)
    if not payload:
        raise HTTPException(status_code=404, detail="Reason payload not found")
    return payload