"""
Identity routes: mint CreditIdentity NFT and look up token IDs.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException
from web3 import Web3

from models.schemas import UserOnboardRequest, MintIdentityRequest
from core.contracts import contracts, admin_signer, send_tx, get, role_hash
from core.database import db, identities_collection
from services import chain_reader
from services.underwriter import underwrite
from core.web3_utils import find_event
import asyncio

log = logging.getLogger("creditgraph.routes.identity")
router = APIRouter()


def _admin_has_minter_role() -> bool:
    try:
        ac = get("AccessController")
        return ac.functions.hasRole(role_hash("MINTER_ROLE"), admin_signer.address).call()
    except Exception:
        return False


@router.post("/onboard")
async def onboard_user(req: UserOnboardRequest):
    """
    Full onboarding:
      1. If user has no on-chain identity, admin mints one (requires MINTER_ROLE)
      2. Cache the mapping in Mongo
      3. Kick off underwriting (3-agent quorum signs + submits score)
      4. Return token_id + initial underwriting status
    """
    wallet = Web3.to_checksum_address(req.wallet_address)
    log.info(f"Onboarding {wallet}")

    # Check if identity already exists
    existing_token_id = await chain_reader.a_token_id_of(wallet)

    if existing_token_id == 0:
        # Need to mint
        if not _admin_has_minter_role():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Admin does not hold MINTER_ROLE on CreditIdentity. "
                    f"Ask the protocol admin to grant MINTER_ROLE to {admin_signer.address} "
                    "or to mint an identity for this wallet directly."
                ),
            )

        # Build a metadata hash (just a deterministic placeholder for now)
        metadata = Web3.keccak(text=f"creditgraph-v0:{wallet}:{datetime.utcnow().isoformat()}")
        identity_contract = get("CreditIdentity")

        try:
            fn = identity_contract.functions.mint(wallet, metadata)
            result = await asyncio.to_thread(send_tx, fn, admin_signer)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Identity mint failed: {e}")

        # Parse the IdentityMinted event for the new tokenId
        from core.web3_utils import find_event
        ev = find_event("CreditIdentity", "IdentityMinted", result["receipt"])
        token_id = int(ev["tokenId"]) if ev else await chain_reader.a_token_id_of(wallet)

        await identities_collection.replace_one(
            {"wallet_address": wallet},
            {
                "wallet_address": wallet,
                "token_id": token_id,
                "metadata_hash": "0x" + metadata.hex(),
                "minted_at": datetime.utcnow(),
                "mint_tx": result["tx_hash"],
            },
            upsert=True,
        )
        log.info(f"Minted identity #{token_id} for {wallet} in tx {result['tx_hash']}")
    else:
        token_id = existing_token_id
        # Refresh cache
        await identities_collection.replace_one(
            {"wallet_address": wallet},
            {
                "wallet_address": wallet,
                "token_id": token_id,
                "minted_at": datetime.utcnow(),
                "mint_tx": "pre-existing",
            },
            upsert=True,
        )

    # Try to kick off underwriting (best-effort; if it fails the FE can retry)
    underwrite_result = None
    try:
        underwrite_result = await underwrite(wallet)
    except Exception as e:
        log.warning(f"Underwriting failed for {wallet}: {e}")
        underwrite_result = {"status": "underwrite_failed", "error": str(e)}

    return {
        "message": "Identity ready",
        "wallet_address": wallet,
        "token_id": token_id,
        "underwriting": underwrite_result,
    }


@router.post("/identity/mint")
async def mint_identity(req: MintIdentityRequest):
    """
    Admin endpoint: mint a CreditIdentity for any wallet. Useful when the FE
    flow can't mint directly (e.g. testing, bulk seeding, demo setup).
    """
    wallet = Web3.to_checksum_address(req.wallet_address)
    if not _admin_has_minter_role():
        raise HTTPException(status_code=503, detail="Admin lacks MINTER_ROLE")

    if await chain_reader.a_token_id_of(wallet) != 0:
        raise HTTPException(status_code=400, detail="Identity already exists for this wallet")

    metadata = (
        Web3.to_bytes(hexstr=req.metadata_hash)
        if req.metadata_hash
        else Web3.keccak(text=f"creditgraph-v0:{wallet}")
    )

    fn = contracts["CreditIdentity"].functions.mint(wallet, metadata)
    result = await asyncio.to_thread(send_tx, fn, admin_signer)
    ev = find_event("CreditIdentity", "IdentityMinted", result["receipt"])
    token_id = int(ev["tokenId"]) if ev else await chain_reader.a_token_id_of(wallet)

    return {
        "token_id": token_id,
        "tx_hash": result["tx_hash"],
        "metadata_hash": "0x" + metadata.hex(),
    }


@router.get("/identity/{wallet_address}")
async def get_identity(wallet_address: str):
    wallet = Web3.to_checksum_address(wallet_address)
    token_id = await chain_reader.a_token_id_of(wallet)
    if token_id == 0:
        return {"wallet_address": wallet, "has_identity": False, "token_id": 0}

    cached = await identities_collection.find_one({"wallet_address": wallet})
    return {
        "wallet_address": wallet,
        "has_identity": True,
        "token_id": token_id,
        "minted_at": cached.get("minted_at") if cached else None,
        "mint_tx": cached.get("mint_tx") if cached else None,
    }