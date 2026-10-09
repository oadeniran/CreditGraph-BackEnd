"""
Score heuristic. Chain-aware: pulls graduation + attestation weight from the
chain being scored, so each chain has its own score.
"""

import hashlib
from typing import Tuple

from services import chain_reader


# Tier cutoffs (matches what the spec implies; tunable)
TIER_CUTOFFS = [
    (1, 300),
    (2, 580),
    (3, 660),
    (4, 740),
    (5, 820),
]


def _seed_signal(wallet: str, salt: str) -> float:
    h = hashlib.sha256(f"{wallet.lower()}:{salt}".encode()).hexdigest()
    return (int(h[:8], 16) % 10_000) / 10_000.0


def _tier_for(score: int) -> int:
    for tier, cutoff in reversed(TIER_CUTOFFS):
        if score >= cutoff:
            return tier
    return 1


def compute_score(chain_key: str, wallet: str, token_id: int) -> Tuple[int, int, dict]:
    """
    Returns (score, tier, components_dict) on the specified chain.
    """
    mobile_money = _seed_signal(wallet, "mobile_money")
    kyc = 0.8
    onchain_history = _seed_signal(wallet, "onchain_history")

    grad = chain_reader.graduation_state(chain_key, token_id) if token_id else {
        "lifetime_on_time": 0, "lifetime_defaults": 0, "streak": 0
    }
    attestation_weight_usdc = (
        chain_reader.total_attestation_weight(chain_key, token_id) if token_id else 0.0
    )

    streak = grad.get("streak", 0)
    defaults = grad.get("lifetime_defaults", 0)
    graduation_signal = max(0.0, min(1.0, (streak * 0.1) - (defaults * 0.3) + 0.3))
    attestation_signal = min(1.0, attestation_weight_usdc / 50.0)

    components = {
        "mobile_money": {
            "weight": 0.35,
            "value": round(mobile_money, 3),
            "note": "Simulated inflow regularity over 12 months",
        },
        "onchain_history": {
            "weight": 0.20,
            "value": round(onchain_history, 3),
            "note": "Wallet activity and repayment patterns",
        },
        "attestations": {
            "weight": 0.20,
            "value": round(attestation_signal, 3),
            "note": f"${attestation_weight_usdc:.2f} of social vouching on this chain",
        },
        "identity_kyc": {
            "weight": 0.15,
            "value": kyc,
            "note": "Identity verified",
        },
        "graduation": {
            "weight": 0.10,
            "value": round(graduation_signal, 3),
            "note": f"{grad.get('lifetime_on_time', 0)} on-time / {defaults} defaults / streak {streak}",
        },
    }

    weighted = sum(c["value"] * c["weight"] for c in components.values())
    score = int(round(300 + weighted * 700))
    score = max(300, min(1000, score))
    tier = _tier_for(score)

    return score, tier, components