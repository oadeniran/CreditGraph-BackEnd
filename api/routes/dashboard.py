"""
Dashboard route: hydrates the FE with a complete on-chain view of the user.
"""

import logging
from fastapi import APIRouter, HTTPException
from web3 import Web3
import asyncio

from services import chain_reader
from core.database import attestations_collection, identities_collection, scores_collection

log = logging.getLogger("creditgraph.routes.dashboard")
router = APIRouter()


@router.get("/user/{wallet_address}")
async def get_dashboard(wallet_address: str):
    wallet = Web3.to_checksum_address(wallet_address)

    token_id = await chain_reader.a_token_id_of(wallet)
    if token_id == 0:
        raise HTTPException(
            status_code=404,
            detail="No identity found. Call /api/onboard first."
        )

    # Fire all independent reads in parallel — single network round-trip-ish
    (
        score_info,
        pending,
        challenge_period,
        credit,
        attest_weight,
        grad,
        loan_ids,
        received_raw,
        given_ids,
        usdc_bal,
        cgusdc,
    ) = await asyncio.gather(
        chain_reader.a_get_score(token_id),
        chain_reader.a_get_pending_score(token_id),
        chain_reader.a_oracle_challenge_period(),
        chain_reader.a_available_credit(token_id),
        chain_reader.a_total_attestation_weight(token_id),
        chain_reader.a_graduation_state(token_id),
        chain_reader.a_borrower_loan_ids(token_id),
        chain_reader.a_attestations_for(token_id),
        chain_reader.a_attestations_by(token_id),
        chain_reader.a_usdc_balance(wallet),
        chain_reader.a_cgusdc_balance(wallet),
    )

    # Loans: fetch each loan in parallel
    loan_results = await asyncio.gather(*[chain_reader.a_get_loan(lid) for lid in loan_ids])
    active_loans = [l for l in loan_results if l and l["state"] in ("Active", "Late")]

    # Attestations received — enrich with attester addresses
    received = []
    for a in received_raw:
        if not a["active"]:
            continue
        attester_doc = await identities_collection.find_one({"token_id": a["attester_token_id"]})
        received.append({
            **a,
            "attester_address": attester_doc.get("wallet_address") if attester_doc else None,
            "subject_address": wallet,
        })

    # Attestations given — fetch each in parallel
    given_attestations = await asyncio.gather(
        *[chain_reader.a_get_attestation(aid) for aid in given_ids]
    )
    given = []
    for att in given_attestations:
        if not att:
            continue
        subject_doc = await identities_collection.find_one({"token_id": att["subject_token_id"]})
        given.append({
            **att,
            "attester_address": wallet,
            "subject_address": subject_doc.get("wallet_address") if subject_doc else None,
        })

    latest_score_doc = await scores_collection.find_one(
        {"token_id": token_id}, sort=[("updated_at", -1)]
    )
    reason_hash = latest_score_doc.get("reason_hash") if latest_score_doc else None

    return {
        "wallet_address": wallet,
        "token_id": token_id,
        "has_identity": True,
        "credit_score": { **score_info, "reason_hash": reason_hash },
        "pending_score": pending,
        "challenge_period": challenge_period,
        "available_limit_usdc": credit["limit_usdc"],
        "current_exposure_usdc": credit["exposure_usdc"],
        "headroom_usdc": credit["headroom_usdc"],
        "attestation_weight_usdc": attest_weight,
        "graduation": grad,
        "active_loans": active_loans,
        "attestations_received": received,
        "attestations_given": given,
        "usdc_balance": usdc_bal,
        "cgusdc_shares": cgusdc["shares"],
        "cgusdc_assets_usdc": cgusdc["assets_usdc"],
    }