"""
Multi-chain registry. Mirror of the FE's lib/chains/index.js.

Every chain the BE supports is registered here. The contracts factory and
chain reader look up chain config from this registry.

Adding a new chain:
  1. Add a new entry to CHAINS
  2. Fund admin + agents on the new chain
  3. Run bootstrap: python -m tools.bootstrap_check --chain <key> --run
  4. Done — all routes automatically work with ?chain=<key>
"""

import logging
import os
from typing import Optional
from dotenv import load_dotenv

# Ensure env is loaded before we read any config.
# Idempotent — safe if envs.py also calls it.
load_dotenv()

log = logging.getLogger("creditgraph.chains")


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(key)
    return v if v is not None and v != "" else default


# Chain definitions. Each has a `contracts` dict + RPC + metadata.
# Values pull from env vars so one deploy can serve different configs
# (prod vs. staging) without code changes.
CHAINS = {
    "arbitrum_sepolia": {
        "name": "Arbitrum Sepolia",
        "chain_id": int(_env("ARBITRUM_SEPOLIA_CHAIN_ID", "421614")),
        "rpc_url": _env("ARBITRUM_SEPOLIA_RPC_URL", "https://sepolia-rollup.arbitrum.io/rpc"),
        "explorer_url": _env("ARBITRUM_SEPOLIA_EXPLORER", "https://sepolia.arbiscan.io"),
        "native_symbol": "ETH",
        "is_testnet": True,
        "max_blocks_per_log_scan": int(_env("ARBITRUM_SEPOLIA_LOG_BATCH", "5000")),
        "indexer_start_block": int(_env("ARBITRUM_SEPOLIA_INDEXER_START", "0")),
        "contracts": {
            "USDC": _env("ARBITRUM_SEPOLIA_USDC"),
            "AccessController": _env("ARBITRUM_SEPOLIA_ACCESS_CONTROLLER"),
            "CreditIdentity": _env("ARBITRUM_SEPOLIA_CREDIT_IDENTITY"),
            "ScoreRegistry": _env("ARBITRUM_SEPOLIA_SCORE_REGISTRY"),
            "ScoringOracle": _env("ARBITRUM_SEPOLIA_SCORING_ORACLE"),
            "ZKAttestationVerifier": _env("ARBITRUM_SEPOLIA_ZK_VERIFIER"),
            "DataOracleAdapter": _env("ARBITRUM_SEPOLIA_DATA_ORACLE_ADAPTER"),
            "AgentRegistry": _env("ARBITRUM_SEPOLIA_AGENT_REGISTRY"),
            "X402PaymentRouter": _env("ARBITRUM_SEPOLIA_X402_PAYMENT_ROUTER"),
            "RepaymentGraduation": _env("ARBITRUM_SEPOLIA_REPAYMENT_GRADUATION"),
            "SocialAttestation": _env("ARBITRUM_SEPOLIA_SOCIAL_ATTESTATION"),
            "InterestRateModel": _env("ARBITRUM_SEPOLIA_INTEREST_RATE_MODEL"),
            "CreditLimitEngine": _env("ARBITRUM_SEPOLIA_CREDIT_LIMIT_ENGINE"),
            "LendingPool": _env("ARBITRUM_SEPOLIA_LENDING_POOL"),
            "LoanManager": _env("ARBITRUM_SEPOLIA_LOAN_MANAGER"),
            "InsuranceFund": _env("ARBITRUM_SEPOLIA_INSURANCE_FUND"),
            "CreditSlasher": _env("ARBITRUM_SEPOLIA_CREDIT_SLASHER"),
            "Treasury": _env("ARBITRUM_SEPOLIA_TREASURY"),
        },
    },
    "monad_testnet": {
        "name": "Monad Testnet",
        "chain_id": int(_env("MONAD_TESTNET_CHAIN_ID", "0")),  # fill in via env
        "rpc_url": _env("MONAD_TESTNET_RPC_URL", ""),
        "explorer_url": _env("MONAD_TESTNET_EXPLORER", ""),
        "native_symbol": "MON",
        "is_testnet": True,
        "max_blocks_per_log_scan": int(_env("MONAD_TESTNET_LOG_BATCH", "100")),
        "indexer_start_block": int(_env("MONAD_TESTNET_INDEXER_START", "0")),
        "contracts": {
            "USDC": _env("MONAD_TESTNET_USDC"),
            "AccessController": _env("MONAD_TESTNET_ACCESS_CONTROLLER"),
            "CreditIdentity": _env("MONAD_TESTNET_CREDIT_IDENTITY"),
            "ScoreRegistry": _env("MONAD_TESTNET_SCORE_REGISTRY"),
            "ScoringOracle": _env("MONAD_TESTNET_SCORING_ORACLE"),
            "ZKAttestationVerifier": _env("MONAD_TESTNET_ZK_VERIFIER"),
            "DataOracleAdapter": _env("MONAD_TESTNET_DATA_ORACLE_ADAPTER"),
            "AgentRegistry": _env("MONAD_TESTNET_AGENT_REGISTRY"),
            "X402PaymentRouter": _env("MONAD_TESTNET_X402_PAYMENT_ROUTER"),
            "RepaymentGraduation": _env("MONAD_TESTNET_REPAYMENT_GRADUATION"),
            "SocialAttestation": _env("MONAD_TESTNET_SOCIAL_ATTESTATION"),
            "InterestRateModel": _env("MONAD_TESTNET_INTEREST_RATE_MODEL"),
            "CreditLimitEngine": _env("MONAD_TESTNET_CREDIT_LIMIT_ENGINE"),
            "LendingPool": _env("MONAD_TESTNET_LENDING_POOL"),
            "LoanManager": _env("MONAD_TESTNET_LOAN_MANAGER"),
            "InsuranceFund": _env("MONAD_TESTNET_INSURANCE_FUND"),
            "CreditSlasher": _env("MONAD_TESTNET_CREDIT_SLASHER"),
            "Treasury": _env("MONAD_TESTNET_TREASURY"),
        },
    },
}


DEFAULT_CHAIN_KEY = _env("DEFAULT_CHAIN_KEY", "arbitrum_sepolia")


class UnknownChain(ValueError):
    pass


def get_chain(chain_key: Optional[str] = None) -> dict:
    """Returns the chain config dict. Defaults to DEFAULT_CHAIN_KEY."""
    key = chain_key or DEFAULT_CHAIN_KEY
    if key not in CHAINS:
        raise UnknownChain(f"Unknown chain: {key}. Known: {list(CHAINS.keys())}")
    return CHAINS[key]


def list_chains() -> list[dict]:
    """Returns public-safe chain metadata (no private keys, no RPC credentials)."""
    out = []
    for key, chain in CHAINS.items():
        if not _is_configured(chain):
            continue
        out.append({
            "key": key,
            "name": chain["name"],
            "chain_id": chain["chain_id"],
            "explorer_url": chain["explorer_url"],
            "native_symbol": chain["native_symbol"],
            "is_testnet": chain["is_testnet"],
            "contracts": chain["contracts"],
        })
    return out


def _is_configured(chain: dict) -> bool:
    """A chain counts as configured only if its RPC + core contracts are set."""
    return bool(
        chain.get("rpc_url")
        and chain.get("chain_id")
        and chain["contracts"].get("CreditIdentity")
        and chain["contracts"].get("LoanManager")
    )


def is_configured(chain_key: str) -> bool:
    try:
        return _is_configured(get_chain(chain_key))
    except UnknownChain:
        return False