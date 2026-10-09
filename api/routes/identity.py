"""Identity routes, chain-aware."""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from web3 import Web3

from models.schemas import UserOnboardRequest, MintIdentityRequest
from core.contracts import get_contract, admin_signer, send_tx, role_hash
from core.database import identities_collection
from core.web3_utils import find_event
from services import chain_reader
from services.underwriter import underwrite
from api.deps import chain_param

log = logging.getLogger("creditgraph.routes.identity")
router = APIRouter()


def _admin_has_minter_role(chain_key: str) -> bool:
    try:
        ac = get_contract(chain_key, "AccessController")
        return ac.functions.hasRole(role_hash("MINTER_ROLE"), admin_signer.address).call()
    except Exception:
        return False


@router.post("/onboard")
async def onboard_user(req: UserOnboardRequest, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(req.wallet_address)
    log.info(f"[{chain}] Onboarding {wallet}")

    existing_token_id = chain_reader.token_id_of(chain, wallet)

    if existing_token_id == 0:
        if not _admin_has_minter_role(chain):
            raise HTTPException(
                status_code=503,
                detail=f"Admin lacks MINTER_ROLE on {chain}. Ask the protocol admin to grant it.",
            )

        metadata = Web3.keccak(text=f"creditgraph-v0:{chain}:{wallet}:{datetime.utcnow().isoformat()}")
        identity_contract = get_contract(chain, "CreditIdentity")

        try:
            fn = identity_contract.functions.mint(wallet, metadata)
            result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Identity mint failed on {chain}: {e}")

        ev = find_event(chain, "CreditIdentity", "IdentityMinted", result["receipt"])
        token_id = int(ev["tokenId"]) if ev else chain_reader.token_id_of(chain, wallet)

        await identities_collection.replace_one(
            {"chain": chain, "wallet_address": wallet},
            {
                "chain": chain,
                "wallet_address": wallet,
                "token_id": token_id,
                "metadata_hash": "0x" + metadata.hex(),
                "minted_at": datetime.utcnow(),
                "mint_tx": result["tx_hash"],
            },
            upsert=True,
        )
        log.info(f"[{chain}] Minted identity #{token_id} for {wallet} in tx {result['tx_hash']}")
    else:
        token_id = existing_token_id
        await identities_collection.replace_one(
            {"chain": chain, "wallet_address": wallet},
            {
                "chain": chain,
                "wallet_address": wallet,
                "token_id": token_id,
                "minted_at": datetime.utcnow(),
                "mint_tx": "pre-existing",
            },
            upsert=True,
        )

    underwrite_result = None
    try:
        underwrite_result = await underwrite(chain, wallet)
    except Exception as e:
        log.warning(f"[{chain}] Underwriting failed for {wallet}: {e}")
        underwrite_result = {"status": "underwrite_failed", "error": str(e)}

    return {
        "message": "Identity ready",
        "chain": chain,
        "wallet_address": wallet,
        "token_id": token_id,
        "underwriting": underwrite_result,
    }


@router.post("/identity/mint")
async def mint_identity(req: MintIdentityRequest, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(req.wallet_address)
    if not _admin_has_minter_role(chain):
        raise HTTPException(status_code=503, detail=f"Admin lacks MINTER_ROLE on {chain}")

    if chain_reader.token_id_of(chain, wallet) != 0:
        raise HTTPException(status_code=400, detail=f"Identity already exists on {chain} for this wallet")

    metadata = (
        Web3.to_bytes(hexstr=req.metadata_hash)
        if req.metadata_hash
        else Web3.keccak(text=f"creditgraph-v0:{chain}:{wallet}")
    )

    fn = get_contract(chain, "CreditIdentity").functions.mint(wallet, metadata)
    result = await asyncio.to_thread(send_tx, chain, fn, admin_signer)
    ev = find_event(chain, "CreditIdentity", "IdentityMinted", result["receipt"])
    token_id = int(ev["tokenId"]) if ev else chain_reader.token_id_of(chain, wallet)

    return {
        "chain": chain,
        "token_id": token_id,
        "tx_hash": result["tx_hash"],
        "metadata_hash": "0x" + metadata.hex(),
    }


@router.get("/identity/{wallet_address}")
async def get_identity(wallet_address: str, chain: str = Depends(chain_param)):
    wallet = Web3.to_checksum_address(wallet_address)
    token_id = chain_reader.token_id_of(chain, wallet)
    if token_id == 0:
        return {"chain": chain, "wallet_address": wallet, "has_identity": False, "token_id": 0}

    cached = await identities_collection.find_one({"chain": chain, "wallet_address": wallet})
    return {
        "chain": chain,
        "wallet_address": wallet,
        "has_identity": True,
        "token_id": token_id,
        "minted_at": cached.get("minted_at") if cached else None,
        "mint_tx": cached.get("mint_tx") if cached else None,
    }