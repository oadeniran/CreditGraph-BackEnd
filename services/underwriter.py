"""
Chain-aware underwriter quorum. Agents are the same across chains, but signatures
differ because the EIP-712 domain separator includes the per-chain chainId +
ScoringOracle address.
"""

import asyncio
import logging
import secrets
import time
from typing import Optional

from eth_account.messages import encode_typed_data
from web3 import Web3

from core.contracts import (
    get_w3, get_contract, admin_signer, agent_signers, send_tx,
)
from services import chain_reader, score_engine
from services.score_reason import build_reason, hash_reason, save_reason

log = logging.getLogger("creditgraph.underwriter")

EIP712_DOMAIN_NAME = "CreditGraph ScoringOracle"
EIP712_DOMAIN_VERSION = "1"


def _build_typed_data(chain_key: str, token_id: int, score: int, tier: int, reason_hash: bytes, nonce: bytes) -> dict:
    """
    Mirrors ScoringOracle's SCORE_TYPEHASH. Chain-specific because:
      - domain.chainId comes from the live chain
      - domain.verifyingContract is the ScoringOracle on THAT chain
    """
    w3 = get_w3(chain_key)
    oracle = get_contract(chain_key, "ScoringOracle")
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
    signable = encode_typed_data(full_message=typed_data)
    signed = []
    for agent in agent_signers:
        sig = agent.sign_message(signable)
        signed.append((agent.address.lower(), sig.signature))
    signed.sort(key=lambda x: int(x[0], 16))
    return [s[1] for s in signed]


async def underwrite(chain_key: str, wallet: str) -> dict:
    """
    Full underwriting flow on the specified chain.
    """
    wallet = Web3.to_checksum_address(wallet)
    token_id = chain_reader.token_id_of(chain_key, wallet)
    if token_id == 0:
        return {"status": "no_identity", "wallet": wallet, "chain_key": chain_key}

    # 1. Compute score (chain-aware so attestation weight comes from the right chain)
    score, tier, components = score_engine.compute_score(chain_key, wallet, token_id)

    # 2. Build + persist reason
    reason = build_reason(chain_key, token_id, wallet, score, tier, components)
    reason_hash = hash_reason(reason)
    reason_hash_hex = "0x" + reason_hash.hex()
    await save_reason(reason_hash_hex, reason)

    # 3. Sign with each agent (chain-specific EIP-712 domain)
    nonce = secrets.token_bytes(32)
    typed_data = _build_typed_data(chain_key, token_id, score, tier, reason_hash, nonce)
    signatures = _sign_with_each_agent(typed_data)

    log.info(
        f"[{chain_key}] Underwriting {wallet} token={token_id} score={score} "
        f"tier={tier} reason_hash={reason_hash_hex[:14]}... signatures={len(signatures)}"
    )

    # 4. Verify agents are authorized on THIS chain
    reg = get_contract(chain_key, "AgentRegistry")
    for agent in agent_signers:
        if not reg.functions.isAuthorized(agent.address, 2).call():
            raise RuntimeError(
                f"Agent {agent.address} not authorized as Underwriter on {chain_key}. "
                f"Run bootstrap first."
            )

    # 5. Submit (admin pays gas; submitScore is permissionless so this is fine)
    oracle = get_contract(chain_key, "ScoringOracle")
    submit_fn = oracle.functions.submitScore(
        token_id, score, tier, reason_hash, signatures, nonce
    )
    submit_result = await asyncio.to_thread(send_tx, chain_key, submit_fn, admin_signer)

    challenge_period = oracle.functions.challengePeriod().call()
    finalize_after = int(time.time()) + int(challenge_period)

    result = {
        "status": "submitted",
        "chain_key": chain_key,
        "wallet": wallet,
        "token_id": token_id,
        "score": score,
        "tier": tier,
        "reason_hash": reason_hash_hex,
        "submit_tx": submit_result["tx_hash"],
        "challenge_period_seconds": int(challenge_period),
        "finalize_after": finalize_after,
    }

    # 6. Finalize immediately if window is 0
    if challenge_period == 0:
        finalize_tx = await asyncio.to_thread(finalize_score, chain_key, token_id)
        if finalize_tx:
            result["status"] = "submitted_and_finalized"
            result["finalize_tx"] = finalize_tx

    return result


def finalize_score(chain_key: str, token_id: int) -> Optional[str]:
    oracle = get_contract(chain_key, "ScoringOracle")
    try:
        score, tier, reason_hash, submitted_at, finalized, challenged = (
            oracle.functions.pending(token_id).call()
        )
    except Exception as e:
        log.warning(f"[{chain_key}] finalize_score: cannot read pending({token_id}): {e}")
        return None

    if submitted_at == 0:
        return None
    if finalized:
        return None
    if challenged:
        log.warning(f"[{chain_key}] finalize_score({token_id}): submission challenged")
        return None

    challenge_period = oracle.functions.challengePeriod().call()
    now = int(time.time())
    if now < submitted_at + challenge_period:
        log.info(
            f"[{chain_key}] finalize_score({token_id}): window still open "
            f"({submitted_at + challenge_period - now}s remaining)"
        )
        return None

    fn = oracle.functions.finalizeScore(token_id)
    result = send_tx(chain_key, fn, admin_signer)
    log.info(f"[{chain_key}] finalize_score({token_id}): tx {result['tx_hash']}")
    return result["tx_hash"]


def challenge_score(chain_key: str, token_id: int) -> Optional[str]:
    oracle = get_contract(chain_key, "ScoringOracle")
    try:
        fn = oracle.functions.challengeScore(token_id)
        result = send_tx(chain_key, fn, admin_signer)
        return result["tx_hash"]
    except Exception as e:
        log.warning(f"[{chain_key}] challenge_score({token_id}) failed: {e}")
        return None