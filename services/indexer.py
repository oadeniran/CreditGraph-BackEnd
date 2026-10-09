"""
Per-chain event indexer. One background task per configured chain, each with
its own last_block pointer. Idempotent — upserts on (chain, primary_key).
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.chains import list_chains
from core.contracts import get_w3, get_contracts
from core.database import (
    indexer_state_collection,
    loans_collection, attestations_collection, scores_collection,
    identities_collection, channels_collection,
    graduations_collection, defaults_collection,
)
from envs import INDEXER_POLL_SECONDS, INDEXER_ENABLED

log = logging.getLogger("creditgraph.indexer")

MAX_BLOCKS_PER_PASS = 5_000


async def _get_last_block(chain_key: str) -> int:
    doc = await indexer_state_collection.find_one({"key": f"last_block:{chain_key}"})
    return int(doc["value"]) if doc else 0


async def _set_last_block(chain_key: str, block: int) -> None:
    await indexer_state_collection.replace_one(
        {"key": f"last_block:{chain_key}"},
        {"key": f"last_block:{chain_key}", "chain": chain_key, "value": int(block), "updated_at": datetime.utcnow()},
        upsert=True,
    )


async def _wallet_for_token(chain_key: str, token_id: int) -> str:
    doc = await identities_collection.find_one({"chain": chain_key, "token_id": int(token_id)})
    return doc["wallet_address"] if doc else ""


# ----------------------------------------------------------------
# Handlers — all take chain_key
# ----------------------------------------------------------------

async def _handle_identity_minted(chain_key, ev):
    args = ev["args"]
    await identities_collection.update_one(
        {"chain": chain_key, "token_id": int(args["tokenId"])},
        {"$set": {
            "chain": chain_key,
            "wallet_address": args["user"],
            "token_id": int(args["tokenId"]),
            "metadata_hash": "0x" + args["metadataHash"].hex(),
            "minted_at": datetime.utcnow(),
            "mint_block": ev["blockNumber"],
            "mint_tx": ev["transactionHash"].hex(),
        }},
        upsert=True,
    )


async def _handle_score_finalized(chain_key, ev):
    args = ev["args"]
    await scores_collection.insert_one({
        "chain": chain_key,
        "token_id": int(args["tokenId"]),
        "score": int(args["score"]),
        "tier": int(args["tier"]),
        "updated_at": datetime.utcnow(),
        "block": ev["blockNumber"],
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_loan_originated(chain_key, ev):
    args = ev["args"]
    token_id = int(args["tokenId"])
    wallet = await _wallet_for_token(chain_key, token_id)
    amount = int(args["amount"])
    await loans_collection.update_one(
        {"chain": chain_key, "loan_id_onchain": int(args["loanId"])},
        {"$set": {
            "chain": chain_key,
            "loan_id_onchain": int(args["loanId"]),
            "token_id": token_id,
            "wallet_address": wallet,
            "principal_usdc": amount / 1e6,
            "outstanding_usdc": amount / 1e6,
            "apr_bps": int(args["aprBps"]),
            "due_at_ts": int(args["dueAt"]),
            "state": "Active",
            "originated_at": datetime.utcnow(),
            "originate_tx": ev["transactionHash"].hex(),
        }},
        upsert=True,
    )


async def _handle_loan_repaid(chain_key, ev):
    args = ev["args"]
    loan_id = int(args["loanId"])
    fully = bool(args["fullyRepaid"])
    principal = int(args["principalPaid"]) / 1e6
    interest = int(args["interestPaid"]) / 1e6
    existing = await loans_collection.find_one({"chain": chain_key, "loan_id_onchain": loan_id})
    new_outstanding = max(0.0, (existing.get("outstanding_usdc", 0) if existing else 0) - principal)
    await loans_collection.update_one(
        {"chain": chain_key, "loan_id_onchain": loan_id},
        {
            "$set": {
                "outstanding_usdc": new_outstanding,
                "state": "Repaid" if fully else "Active",
                "last_repayment_tx": ev["transactionHash"].hex(),
                "last_repayment_at": datetime.utcnow(),
            },
            "$inc": {"interest_paid_usdc": interest},
        },
    )


async def _handle_loan_marked_late(chain_key, ev):
    loan_id = int(ev["args"]["loanId"])
    await loans_collection.update_one(
        {"chain": chain_key, "loan_id_onchain": loan_id}, {"$set": {"state": "Late"}}
    )


async def _handle_loan_defaulted(chain_key, ev):
    args = ev["args"]
    loan_id = int(args["loanId"])
    await loans_collection.update_one(
        {"chain": chain_key, "loan_id_onchain": loan_id},
        {"$set": {"state": "Defaulted", "defaulted_at": datetime.utcnow()}},
    )
    await defaults_collection.insert_one({
        "chain": chain_key,
        "loan_id": loan_id,
        "outstanding_usdc": int(args["outstanding"]) / 1e6,
        "at": datetime.utcnow(),
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_attested(chain_key, ev):
    args = ev["args"]
    attester_token_id = int(args["attesterTokenId"])
    subject_token_id = int(args["subjectTokenId"])
    attester_addr = await _wallet_for_token(chain_key, attester_token_id)
    subject_addr = await _wallet_for_token(chain_key, subject_token_id)
    rel_type = args["relationshipType"]
    rel_hex = "0x" + rel_type.hex() if hasattr(rel_type, "hex") else str(rel_type)
    await attestations_collection.update_one(
        {"chain": chain_key, "attestation_id_onchain": int(args["attestationId"])},
        {"$set": {
            "chain": chain_key,
            "attestation_id_onchain": int(args["attestationId"]),
            "attester_token_id": attester_token_id,
            "subject_token_id": subject_token_id,
            "attester_address": attester_addr,
            "subject_address": subject_addr,
            "bond_usdc": int(args["bondAmount"]) / 1e6,
            "active": True,
            "relationship_type": rel_hex,
            "created_at": datetime.utcnow(),
            "attest_tx": ev["transactionHash"].hex(),
        }},
        upsert=True,
    )


async def _handle_revoked(chain_key, ev):
    args = ev["args"]
    await attestations_collection.update_one(
        {"chain": chain_key, "attestation_id_onchain": int(args["attestationId"])},
        {"$set": {"active": False, "returned_usdc": int(args["returned"]) / 1e6, "revoked_at": datetime.utcnow()}},
    )


async def _handle_attestations_slashed(chain_key, ev):
    args = ev["args"]
    log.info(
        f"[{chain_key}] AttestationsSlashed: subject={int(args['subjectTokenId'])} "
        f"loss={int(args['lossAmount']) / 1e6} recovered={int(args['recovered']) / 1e6}"
    )


async def _handle_tier_promoted(chain_key, ev):
    args = ev["args"]
    await graduations_collection.insert_one({
        "chain": chain_key,
        "token_id": int(args["tokenId"]),
        "old_tier": int(args["oldTier"]),
        "new_tier": int(args["newTier"]),
        "at": datetime.utcnow(),
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_channel_opened(chain_key, ev):
    args = ev["args"]
    channel_id = "0x" + args["channelId"].hex()
    await channels_collection.update_one(
        {"chain": chain_key, "channel_id": channel_id},
        {"$set": {
            "chain": chain_key,
            "channel_id": channel_id,
            "payer": args["payer"],
            "payee": args["payee"],
            "deposit_usdc": int(args["deposit"]) / 1e6,
            "expires_at": int(args["expiresAt"]),
            "open": True,
            "open_tx": ev["transactionHash"].hex(),
            "opened_at": datetime.utcnow(),
        }},
        upsert=True,
    )


async def _handle_settled(chain_key, ev):
    args = ev["args"]
    channel_id = "0x" + args["channelId"].hex()
    await channels_collection.update_one(
        {"chain": chain_key, "channel_id": channel_id},
        {"$set": {
            "claimed_usdc": int(args["cumulativeAmount"]) / 1e6,
            "last_settle_at": datetime.utcnow(),
            "last_settle_tx": ev["transactionHash"].hex(),
        }},
    )


_HANDLERS = [
    ("CreditIdentity", "IdentityMinted", _handle_identity_minted),
    ("ScoringOracle", "ScoreFinalized", _handle_score_finalized),
    ("LoanManager", "LoanOriginated", _handle_loan_originated),
    ("LoanManager", "LoanRepaid", _handle_loan_repaid),
    ("LoanManager", "LoanMarkedLate", _handle_loan_marked_late),
    ("LoanManager", "LoanDefaulted", _handle_loan_defaulted),
    ("SocialAttestation", "Attested", _handle_attested),
    ("SocialAttestation", "Revoked", _handle_revoked),
    ("SocialAttestation", "AttestationsSlashed", _handle_attestations_slashed),
    ("RepaymentGraduation", "TierPromoted", _handle_tier_promoted),
    ("X402PaymentRouter", "ChannelOpened", _handle_channel_opened),
    ("X402PaymentRouter", "Settled", _handle_settled),
]


async def _process_range(chain_key: str, from_block: int, to_block: int) -> int:
    highest = from_block
    contracts = get_contracts(chain_key)
    for contract_name, event_name, handler in _HANDLERS:
        contract = contracts.get(contract_name)
        if contract is None:
            continue
        try:
            event_obj = getattr(contract.events, event_name)
            logs = event_obj.get_logs(from_block=from_block, to_block=to_block)
            for ev in logs:
                try:
                    await handler(chain_key, ev)
                    highest = max(highest, ev["blockNumber"])
                except Exception as e:
                    log.warning(f"[{chain_key}] handler error {contract_name}.{event_name} @ {ev['blockNumber']}: {e}")
        except Exception as e:
            log.warning(f"[{chain_key}] get_logs {contract_name}.{event_name} [{from_block}..{to_block}]: {e}")
    return highest


async def _run_chain_indexer(chain_key: str):
    """Single-chain polling loop."""
    from core.chains import get_chain
    chain = get_chain(chain_key)
    max_blocks = chain.get("max_blocks_per_log_scan", MAX_BLOCKS_PER_PASS)

    log.info(f"[{chain_key}] indexer starting (poll every {INDEXER_POLL_SECONDS}s, max {max_blocks} blocks/scan)")
    while True:
        try:
            last = await _get_last_block(chain_key)
            w3 = get_w3(chain_key)
            latest = w3.eth.block_number
            if last == 0:
                # Use configured start block if set, otherwise look back 1000 blocks
                configured_start = chain.get("indexer_start_block", 0)
                if configured_start > 0:
                    last = configured_start
                else:
                    last = max(0, latest - 1000)
            if last >= latest:
                await asyncio.sleep(INDEXER_POLL_SECONDS)
                continue

            to_block = min(latest, last + max_blocks)  # ← changed from MAX_BLOCKS_PER_PASS
            from_block = last + 1
            log.debug(f"[{chain_key}] scanning {from_block}..{to_block}")
            await _process_range(chain_key, from_block, to_block)
            await _set_last_block(chain_key, to_block)
        except Exception as e:
            log.error(f"[{chain_key}] indexer error: {e}")
        await asyncio.sleep(INDEXER_POLL_SECONDS)


async def run_indexer():
    """Spawns one indexer loop per configured chain."""
    if not INDEXER_ENABLED:
        log.info("Indexer disabled via INDEXER_ENABLED=false")
        return

    tasks = []
    for chain in list_chains():
        tasks.append(asyncio.create_task(_run_chain_indexer(chain["key"])))

    if not tasks:
        log.warning("No chains configured — indexer idle")
        return

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise