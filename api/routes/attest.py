"""
Attestation routes: verify FE-submitted attest/revoke txs, paginated history.
"""

import logging
import math
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from web3 import Web3

from models.schemas import AttestRequest, AttestRevokeRequest
from core.web3_utils import get_receipt, find_event
from core.database import attestations_collection, identities_collection
from services import chain_reader

log = logging.getLogger("creditgraph.routes.attest")
router = APIRouter()


async def _wallet_for_token(token_id: int) -> str:
    doc = await identities_collection.find_one({"token_id": token_id})
    return doc["wallet_address"] if doc else ""


@router.post("/attest")
async def submit_attestation(req: AttestRequest):
    """FE has called SocialAttestation.attest. Verify + index."""
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    ev = find_event("SocialAttestation", "Attested", receipt)
    if not ev:
        raise HTTPException(status_code=400, detail="No Attested event in this receipt")

    attestation_id = int(ev["attestationId"])
    attester_token_id = int(ev["attesterTokenId"])
    subject_token_id = int(ev["subjectTokenId"])
    bond = int(ev["bondAmount"])
    rel_type = "0x" + ev["relationshipType"].hex() if hasattr(ev["relationshipType"], "hex") else str(ev["relationshipType"])

    attester_addr = await _wallet_for_token(attester_token_id)
    subject_addr = await _wallet_for_token(subject_token_id)

    doc = {
        "attestation_id_onchain": attestation_id,
        "attester_token_id": attester_token_id,
        "subject_token_id": subject_token_id,
        "attester_address": attester_addr,
        "subject_address": subject_addr,
        "bond_usdc": bond / 1e6,
        "active": True,
        "relationship_type": rel_type,
        "created_at": datetime.utcnow(),
        "attest_tx": req.tx_hash,
    }
    await attestations_collection.replace_one(
        {"attestation_id_onchain": attestation_id}, doc, upsert=True
    )
    return {
        "message": "Attestation recorded",
        "attestation_id": attestation_id,
        "bond_usdc": bond / 1e6,
    }


@router.post("/attest/revoke")
async def revoke_attestation(req: AttestRevokeRequest):
    """
    Two-step revoke: action='request' (requestRevoke + cooldown), then
    action='finalize' (revoke + reclaim bond after cooldown).
    The FE has already signed the tx via MetaMask; we verify + index.
    """
    receipt = get_receipt(req.tx_hash)
    if receipt is None or receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction not found or reverted")

    if req.action == "request":
        await attestations_collection.update_one(
            {"attestation_id_onchain": req.attestation_id},
            {"$set": {"revoke_requested_at": datetime.utcnow(), "revoke_request_tx": req.tx_hash}},
        )
        # Also pull on-chain unlock time
        att = await chain_reader.a_get_attestation(req.attestation_id)
        return {
            "message": "Revoke requested. Cooldown started.",
            "attestation_id": req.attestation_id,
            "unlock_at": att.get("revoke_unlock_at") if att else None,
        }

    elif req.action == "finalize":
        ev = find_event("SocialAttestation", "Revoked", receipt)
        returned = int(ev["returned"]) / 1e6 if ev else 0
        await attestations_collection.update_one(
            {"attestation_id_onchain": req.attestation_id},
            {
                "$set": {
                    "active": False,
                    "revoked_at": datetime.utcnow(),
                    "revoke_tx": req.tx_hash,
                    "returned_usdc": returned,
                }
            },
        )
        return {
            "message": "Bond reclaimed",
            "attestation_id": req.attestation_id,
            "returned_usdc": returned,
        }

    raise HTTPException(status_code=400, detail="action must be 'request' or 'finalize'")


@router.get("/attestation/{attestation_id}")
async def get_attestation_detail(attestation_id: int):
    """Full attestation state from chain."""
    att = await chain_reader.a_get_attestation(attestation_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attestation not found")
    return att


@router.get("/user/{wallet_address}/attestations")
async def get_attestation_history(
    wallet_address: str,
    page: int = Query(1, ge=1),
    size: int = Query(5, ge=1, le=50),
    role: str = Query("given", pattern="^(given|received)$"),
):
    """
    Paginated attestation history.
    role='given'    -> attestations this wallet made on others
    role='received' -> attestations made on this wallet
    """
    wallet = Web3.to_checksum_address(wallet_address)
    skip = (page - 1) * size

    field = "attester_address" if role == "given" else "subject_address"
    total_count = await attestations_collection.count_documents({field: wallet})
    total_pages = max(1, math.ceil(total_count / size))

    cursor = (
        attestations_collection.find({field: wallet})
        .sort("created_at", -1)
        .skip(skip)
        .limit(size)
    )
    docs = await cursor.to_list(length=size)

    items = []
    for d in docs:
        items.append({
            "attestation_id": d.get("attestation_id_onchain"),
            "attester_address": d.get("attester_address"),
            "subject_address": d.get("subject_address"),
            "bond_usdc": d.get("bond_usdc", 0),
            "active": d.get("active", False),
            "created_at": d.get("created_at"),
            "relationship_type": d.get("relationship_type"),
        })

    return {
        "items": items,
        "total_pages": total_pages,
        "current_page": page,
        "total": total_count,
        "role": role,
    }