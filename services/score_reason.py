"""
Builds the score reason payload and a stable hash to embed on-chain as reasonHash.
We store the full payload in Mongo so the FE can render a "Why this score?" view.
"""

import json
import hashlib
from datetime import datetime
from typing import Optional

from web3 import Web3
from core.database import db


def build_reason(token_id: int, wallet: str, score: int, tier: int, components: dict) -> dict:
    """
    components example:
      {
        "mobile_money": {"weight": 0.35, "value": 0.78, "note": "12 months of regular inflows"},
        "onchain_history": {"weight": 0.20, "value": 0.55, "note": "Wallet active for 4 months"},
        "attestations": {"weight": 0.20, "value": 0.40, "note": "1 attester, Tier 2"},
        "identity_kyc": {"weight": 0.15, "value": 0.80, "note": "Phone verified"},
        "graduation": {"weight": 0.10, "value": 0.50, "note": "2 on-time repayments"},
      }
    """
    return {
        "version": "0.1",
        "token_id": token_id,
        "wallet": wallet,
        "score": score,
        "tier": tier,
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "model": "creditgraph-v0-heuristic",
        "components": components,
        "explainer": _explainer_text(score, tier, components),
    }


def hash_reason(reason: dict) -> bytes:
    """bytes32 hash of the canonical JSON form."""
    canonical = json.dumps(reason, sort_keys=True, separators=(",", ":"))
    return Web3.keccak(text=canonical)


def _explainer_text(score: int, tier: int, components: dict) -> str:
    strongest = max(components.items(), key=lambda kv: kv[1].get("value", 0) * kv[1].get("weight", 0))
    weakest = min(components.items(), key=lambda kv: kv[1].get("value", 0) * kv[1].get("weight", 0))
    return (
        f"You're at Tier {tier} with a score of {score}. Your strongest signal is "
        f"{strongest[0].replace('_', ' ')} — {strongest[1].get('note', '')}. "
        f"To improve, focus on {weakest[0].replace('_', ' ')}."
    )


async def save_reason(reason_hash_hex: str, reason: dict) -> None:
    key = reason_hash_hex.lower()
    await db.score_reasons.replace_one(
        {"hash": key},
        {"hash": key, "payload": reason, "saved_at": datetime.utcnow()},
        upsert=True,
    )


async def load_reason(reason_hash_hex: str) -> Optional[dict]:
    doc = await db.score_reasons.find_one({"hash": reason_hash_hex.lower()})
    return doc["payload"] if doc else None