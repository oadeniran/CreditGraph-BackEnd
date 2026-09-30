"""
Background event indexer. Polls a sliding window of blocks for events from our
contracts and writes them to Mongo. Idempotent — re-running over the same range
is safe because we upsert on the on-chain primary key (loan_id, attestation_id,
channel_id, or token_id+timestamp).

Run as a background asyncio task from main.py.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.contracts import w3, contracts
from core.database import (
    db, indexer_state_collection,
    loans_collection, attestations_collection, scores_collection,
    identities_collection, channels_collection,
    graduations_collection, defaults_collection,
)
from envs import INDEXER_POLL_SECONDS, INDEXER_START_BLOCK, INDEXER_ENABLED

log = logging.getLogger("creditgraph.indexer")

# Max blocks to scan per pass; tune based on RPC limits
MAX_BLOCKS_PER_PASS = 5_000


async def _get_last_block() -> int:
    doc = await indexer_state_collection.find_one({"key": "last_block"})
    return int(doc["value"]) if doc else INDEXER_START_BLOCK


async def _set_last_block(block: int) -> None:
    await indexer_state_collection.replace_one(
        {"key": "last_block"},
        {"key": "last_block", "value": int(block), "updated_at": datetime.utcnow()},
        upsert=True,
    )


async def _wallet_for_token(token_id: int) -> str:
    doc = await identities_collection.find_one({"token_id": int(token_id)})
    return doc["wallet_address"] if doc else ""


# ----------------------------------------------------------------
# Per-event handlers
# ----------------------------------------------------------------

async def _handle_identity_minted(ev):
    args = ev["args"]
    await identities_collection.update_one(
        {"token_id": int(args["tokenId"])},
        {
            "$set": {
                "wallet_address": args["user"],
                "token_id": int(args["tokenId"]),
                "metadata_hash": "0x" + args["metadataHash"].hex(),
                "minted_at": datetime.utcnow(),
                "mint_block": ev["blockNumber"],
                "mint_tx": ev["transactionHash"].hex(),
            }
        },
        upsert=True,
    )


async def _handle_score_finalized(ev):
    args = ev["args"]
    await scores_collection.insert_one({
        "token_id": int(args["tokenId"]),
        "score": int(args["score"]),
        "tier": int(args["tier"]),
        "updated_at": datetime.utcnow(),
        "block": ev["blockNumber"],
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_loan_originated(ev):
    args = ev["args"]
    token_id = int(args["tokenId"])
    wallet = await _wallet_for_token(token_id)
    amount = int(args["amount"])
    await loans_collection.update_one(
        {"loan_id_onchain": int(args["loanId"])},
        {
            "$set": {
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
            }
        },
        upsert=True,
    )


async def _handle_loan_repaid(ev):
    args = ev["args"]
    loan_id = int(args["loanId"])
    fully = bool(args["fullyRepaid"])
    principal = int(args["principalPaid"]) / 1e6
    interest = int(args["interestPaid"]) / 1e6
    existing = await loans_collection.find_one({"loan_id_onchain": loan_id})
    new_outstanding = max(0.0, (existing.get("outstanding_usdc", 0) if existing else 0) - principal)
    await loans_collection.update_one(
        {"loan_id_onchain": loan_id},
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


async def _handle_loan_marked_late(ev):
    loan_id = int(ev["args"]["loanId"])
    await loans_collection.update_one(
        {"loan_id_onchain": loan_id}, {"$set": {"state": "Late"}}
    )


async def _handle_loan_defaulted(ev):
    args = ev["args"]
    loan_id = int(args["loanId"])
    await loans_collection.update_one(
        {"loan_id_onchain": loan_id},
        {"$set": {"state": "Defaulted", "defaulted_at": datetime.utcnow()}},
    )
    await defaults_collection.insert_one({
        "loan_id": loan_id,
        "outstanding_usdc": int(args["outstanding"]) / 1e6,
        "at": datetime.utcnow(),
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_attested(ev):
    args = ev["args"]
    attester_token_id = int(args["attesterTokenId"])
    subject_token_id = int(args["subjectTokenId"])
    attester_addr = await _wallet_for_token(attester_token_id)
    subject_addr = await _wallet_for_token(subject_token_id)
    rel_type = args["relationshipType"]
    rel_hex = "0x" + rel_type.hex() if hasattr(rel_type, "hex") else str(rel_type)
    await attestations_collection.update_one(
        {"attestation_id_onchain": int(args["attestationId"])},
        {
            "$set": {
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
            }
        },
        upsert=True,
    )


async def _handle_revoked(ev):
    args = ev["args"]
    await attestations_collection.update_one(
        {"attestation_id_onchain": int(args["attestationId"])},
        {
            "$set": {
                "active": False,
                "returned_usdc": int(args["returned"]) / 1e6,
                "revoked_at": datetime.utcnow(),
            }
        },
    )


async def _handle_attestations_slashed(ev):
    args = ev["args"]
    subject_token_id = int(args["subjectTokenId"])
    # Mark all active attestations on subject as needing refresh (chain has true state)
    log.info(
        f"Indexer saw AttestationsSlashed: subject={subject_token_id} "
        f"loss={int(args['lossAmount']) / 1e6} recovered={int(args['recovered']) / 1e6}"
    )


async def _handle_tier_promoted(ev):
    args = ev["args"]
    await graduations_collection.insert_one({
        "token_id": int(args["tokenId"]),
        "old_tier": int(args["oldTier"]),
        "new_tier": int(args["newTier"]),
        "at": datetime.utcnow(),
        "tx_hash": ev["transactionHash"].hex(),
    })


async def _handle_channel_opened(ev):
    args = ev["args"]
    channel_id = "0x" + args["channelId"].hex()
    await channels_collection.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "channel_id": channel_id,
                "payer": args["payer"],
                "payee": args["payee"],
                "deposit_usdc": int(args["deposit"]) / 1e6,
                "expires_at": int(args["expiresAt"]),
                "open": True,
                "open_tx": ev["transactionHash"].hex(),
                "opened_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


async def _handle_settled(ev):
    args = ev["args"]
    channel_id = "0x" + args["channelId"].hex()
    await channels_collection.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "claimed_usdc": int(args["cumulativeAmount"]) / 1e6,
                "last_settle_at": datetime.utcnow(),
                "last_settle_tx": ev["transactionHash"].hex(),
            }
        },
    )


# ----------------------------------------------------------------
# Event -> handler map
# ----------------------------------------------------------------

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


# ----------------------------------------------------------------
# Polling loop
# ----------------------------------------------------------------

async def _process_range(from_block: int, to_block: int) -> int:
    """Scans a block range for all events. Returns the highest block actually processed."""
    highest = from_block
    for contract_name, event_name, handler in _HANDLERS:
        contract = contracts.get(contract_name)
        if contract is None:
            continue
        try:
            event_obj = getattr(contract.events, event_name)
            event_filter = event_obj.get_logs(from_block=from_block, to_block=to_block)
            for ev in event_filter:
                try:
                    await handler(ev)
                    highest = max(highest, ev["blockNumber"])
                except Exception as e:
                    log.warning(f"Handler error {contract_name}.{event_name} at block {ev['blockNumber']}: {e}")
        except Exception as e:
            log.warning(f"get_logs failed for {contract_name}.{event_name} [{from_block}..{to_block}]: {e}")
    return highest


async def run_indexer():
    """Background loop. Polls forever."""
    if not INDEXER_ENABLED:
        log.info("Indexer disabled via INDEXER_ENABLED=false")
        return

    log.info(f"Indexer starting (poll every {INDEXER_POLL_SECONDS}s)")
    while True:
        try:
            last = await _get_last_block()
            latest = w3.eth.block_number
            if last == 0:
                # First run: start from a few blocks back to catch recent activity
                last = max(0, latest - 1000)
            if last >= latest:
                await asyncio.sleep(INDEXER_POLL_SECONDS)
                continue

            to_block = min(latest, last + MAX_BLOCKS_PER_PASS)
            from_block = last + 1
            log.debug(f"Indexer scanning {from_block}..{to_block}")
            await _process_range(from_block, to_block)
            await _set_last_block(to_block)
        except Exception as e:
            log.error(f"Indexer error: {e}")
        await asyncio.sleep(INDEXER_POLL_SECONDS)