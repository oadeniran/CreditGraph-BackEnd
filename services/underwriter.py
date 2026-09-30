"""
The underwriter quorum.

Pipeline per user:
  1. compute_score(wallet, token_id) -> (score, tier, components)
  2. build reasonHash + persist payload
  3. 3 agents sign an EIP-712 ScoreSubmission
  4. submit signatures via ScoringOracle.submitScore (one tx, by admin)
  5. wait challenge_period (0 on testnet, 24h otherwise)
  6. finalize via ScoringOracle.finalizeScore

The signers are deterministic agents created in bootstrap. Signatures must be
sorted by ascending signer address (the contract enforces this for dedup).
"""

import logging
import secrets
import time
from typing import Optional

from eth_account.messages import encode_typed_data
from web3 import Web3

from core.contracts import (
    w3, get, admin_signer, agent_signers, send_tx,
)
from services import chain_reader, score_engine
from services.score_reason import build_reason, hash_reason, save_reason

log = logging.getLogger("creditgraph.underwriter")

EIP712_DOMAIN_NAME = "CreditGraph ScoringOracle"
EIP712_DOMAIN_VERSION = "1"


def _build_typed_data(token_id: int, score: int, tier: int, reason_hash: bytes, nonce: bytes) -> dict:
    """
    Mirrors the contract's SCORE_TYPEHASH exactly:
      ScoreSubmission(uint256 tokenId,uint16 score,uint8 tier,bytes32 reasonHash,bytes32 nonce)
    """
    oracle = get("ScoringOracle")
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "ScoreSubmission": [
                {"name": "tokenId", "type": "uint256"},
                {"name": "score", "type": "uint16"},
                {"name": "tier", "type": "uint8"},
                {"name": "reasonHash", "type": "bytes32"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "ScoreSubmission",
        "domain": {
            "name": EIP712_DOMAIN_NAME,
            "version": EIP712_DOMAIN_VERSION,
            "chainId": w3.eth.chain_id,
            "verifyingContract": oracle.address,
        },
        "message": {
            "tokenId": token_id,
            "score": score,
            "tier": tier,
            "reasonHash": reason_hash,
            "nonce": nonce,
        },
    }


def _sign_with_each_agent(typed_data: dict) -> list[bytes]:
    """
    Each agent signs the EIP-712 message. Returns signatures sorted by ascending
    signer address (contract requirement to dedup and bound gas).
    """
    signable = encode_typed_data(full_message=typed_data)
    signed = []
    for agent in agent_signers:
        sig = agent.sign_message(signable)
        signed.append((agent.address.lower(), sig.signature))

    # Sort by signer address ascending (case-insensitive)
    signed.sort(key=lambda x: int(x[0], 16))
    return [s[1] for s in signed]


async def underwrite(wallet: str) -> dict:
    """
    Full underwriting flow for a wallet. Returns a dict describing what happened:
      {
        'status': 'submitted' | 'submitted_and_finalized' | 'no_identity',
        'score': int, 'tier': int,
        'submit_tx': '0x...',
        'finalize_tx': '0x...' (optional),
        'finalize_after': unix_ts,
        'reason_hash': '0x...',
      }
    """
    wallet = Web3.to_checksum_address(wallet)
    token_id = chain_reader.token_id_of(wallet)
    if token_id == 0:
        return {"status": "no_identity", "wallet": wallet}

    # 1. Compute score
    score, tier, components = score_engine.compute_score(wallet, token_id)

    # 2. Build + persist reason
    reason = build_reason(token_id, wallet, score, tier, components)
    reason_hash = hash_reason(reason)
    reason_hash_hex = "0x" + reason_hash.hex()
    await save_reason(reason_hash_hex, reason)

    # 3. Sign with each agent
    nonce = secrets.token_bytes(32)
    typed_data = _build_typed_data(token_id, score, tier, reason_hash, nonce)
    signatures = _sign_with_each_agent(typed_data)

    log.info(
        f"Underwriting {wallet} token={token_id} score={score} tier={tier} "
        f"reason_hash={reason_hash_hex[:14]}... signatures={len(signatures)}"
    )

    # 4. Verify all agents are authorized before we pay gas
    reg = get("AgentRegistry")
    for agent in agent_signers:
        if not reg.functions.isAuthorized(agent.address, 2).call():
            raise RuntimeError(
                f"Agent {agent.address} is not authorized as Underwriter. "
                f"Run bootstrap first."
            )

    # 5. Submit to ScoringOracle (admin pays gas; this is fine — any address can call submitScore)
    oracle = get("ScoringOracle")
    submit_fn = oracle.functions.submitScore(
        token_id, score, tier, reason_hash, signatures, nonce
    )
    submit_result = await asyncio.to_thread(send_tx ,submit_fn, admin_signer)

    # 6. Read pending state to find out when we can finalize
    challenge_period = oracle.functions.challengePeriod().call()
    finalize_after = int(time.time()) + int(challenge_period)

    result = {
        "status": "submitted",
        "wallet": wallet,
        "token_id": token_id,
        "score": score,
        "tier": tier,
        "reason_hash": reason_hash_hex,
        "submit_tx": submit_result["tx_hash"],
        "challenge_period_seconds": int(challenge_period),
        "finalize_after": finalize_after,
    }

    # 7. If challenge period is 0, finalize immediately in the same call
    if challenge_period == 0:
        finalize_result = finalize_score(token_id)
        if finalize_result:
            result["status"] = "submitted_and_finalized"
            result["finalize_tx"] = finalize_result

    return result


def finalize_score(token_id: int) -> Optional[str]:
    """
    Finalize a pending score. Permissionless — anyone can call. Returns tx hash
    on success, None if there's nothing to finalize or the window isn't open yet.
    """
    oracle = get("ScoringOracle")
    try:
        score, tier, reason_hash, submitted_at, finalized, challenged = (
            oracle.functions.pending(token_id).call()
        )
    except Exception as e:
        log.warning(f"finalize_score: cannot read pending({token_id}): {e}")
        return None

    if submitted_at == 0:
        log.info(f"finalize_score({token_id}): nothing pending")
        return None
    if finalized:
        log.info(f"finalize_score({token_id}): already finalized")
        return None
    if challenged:
        log.warning(f"finalize_score({token_id}): submission was challenged")
        return None

    challenge_period = oracle.functions.challengePeriod().call()
    now = int(time.time())
    if now < submitted_at + challenge_period:
        log.info(
            f"finalize_score({token_id}): window still open "
            f"({submitted_at + challenge_period - now}s remaining)"
        )
        return None

    fn = oracle.functions.finalizeScore(token_id)
    result = send_tx(fn, admin_signer)
    log.info(f"finalize_score({token_id}): finalized in tx {result['tx_hash']}")
    return result["tx_hash"]


def challenge_score(token_id: int) -> Optional[str]:
    """
    Permissionless challenge during the window. For demo / admin tooling only.
    Returns tx hash if a challenge was registered.
    """
    oracle = get("ScoringOracle")
    try:
        fn = oracle.functions.challengeScore(token_id)
        result = send_tx(fn, admin_signer)
        return result["tx_hash"]
    except Exception as e:
        log.warning(f"challenge_score({token_id}) failed: {e}")
        return None