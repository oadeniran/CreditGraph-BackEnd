"""
Single source of truth for all chain access.
Exposes:
  - w3                : Web3 instance
  - contracts         : dict of name -> Contract object
  - admin_signer      : LocalAccount used for admin/deployer ops (mint, configure)
  - agent_signers     : list of 3 LocalAccount used for the underwriter quorum
  - send_tx(...)      : helper to build/sign/broadcast a tx
  - role_hash(name)   : keccak256 of a role string ("CONFIG_ROLE", etc.)
"""

import hashlib
import logging
from typing import Optional, Any

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from eth_account.signers.local import LocalAccount

from envs import (
    RPC_URL, CHAIN_ID, ADDR,
    DEPLOYER_PRIVATE_KEY, ADMIN_SEED, AGENT_SEEDS,
)
from core.abis import load as load_abi

log = logging.getLogger("creditgraph.contracts")

# ----------------------------------------------------------------
# Web3 + signers
# ----------------------------------------------------------------

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
# Arbitrum is L2 but POA middleware is harmless and avoids the extraData length issue some RPCs return
try:
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
except Exception:
    pass


def _account_from_seed(seed: str) -> LocalAccount:
    """Deterministically derive a LocalAccount from a string seed."""
    pk_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
    return Account.from_key(pk_bytes)


def _build_admin_signer() -> LocalAccount:
    if DEPLOYER_PRIVATE_KEY:
        pk = DEPLOYER_PRIVATE_KEY
        if not pk.startswith("0x"):
            pk = "0x" + pk
        acct = Account.from_key(pk)
        log.info(f"Admin signer from DEPLOYER_PRIVATE_KEY: {acct.address}")
        return acct
    acct = _account_from_seed(ADMIN_SEED)
    log.info(f"Admin signer derived from ADMIN_SEED: {acct.address}")
    return acct


admin_signer: LocalAccount = _build_admin_signer()
agent_signers: list[LocalAccount] = [_account_from_seed(s) for s in AGENT_SEEDS]
for i, a in enumerate(agent_signers, 1):
    log.info(f"Agent {i} signer: {a.address}")

# ----------------------------------------------------------------
# Contract objects
# ----------------------------------------------------------------

_ABI_NAME_BY_KEY = {
    "USDC": "USDC",
    "AccessController": "AccessController",
    "CreditIdentity": "CreditIdentity",
    "ScoreRegistry": "ScoreRegistry",
    "ScoringOracle": "ScoringOracle",
    "AgentRegistry": "AgentRegistry",
    "CreditLimitEngine": "CreditLimitEngine",
    "InterestRateModel": "InterestRateModel",
    "LendingPool": "LendingPool",
    "LoanManager": "LoanManager",
    "SocialAttestation": "SocialAttestation",
    "RepaymentGraduation": "RepaymentGraduation",
    "InsuranceFund": "InsuranceFund",
    "Treasury": "Treasury",
    "X402PaymentRouter": "X402PaymentRouter",
    "DataOracleAdapter": "DataOracleAdapter",
}


def _build_contracts():
    out = {}
    for key, abi_name in _ABI_NAME_BY_KEY.items():
        addr = ADDR.get(key)
        if not addr:
            log.warning(f"Address for {key} is empty — skipping contract.")
            continue
        try:
            out[key] = w3.eth.contract(
                address=Web3.to_checksum_address(addr),
                abi=load_abi(abi_name),
            )
        except FileNotFoundError:
            log.error(f"Missing ABI file core/abis/{abi_name}.json")
        except Exception as e:
            log.error(f"Failed to build contract {key}: {e}")
    return out


contracts: dict[str, Any] = _build_contracts()


def get(name: str):
    """Convenience getter that raises if a contract isn't loaded."""
    c = contracts.get(name)
    if c is None:
        raise RuntimeError(f"Contract '{name}' not loaded. Check address + ABI.")
    return c


# ----------------------------------------------------------------
# Tx helpers
# ----------------------------------------------------------------

def role_hash(role_name: str) -> bytes:
    """keccak256 of a role string, matching libraries/Roles.sol."""
    return Web3.keccak(text=role_name)


def _gas_with_buffer(estimated: int) -> int:
    # 25% buffer; cheap on Arbitrum
    return int(estimated * 1.25) + 50_000


def send_tx(
    fn,
    signer: LocalAccount,
    value: int = 0,
    gas_override: Optional[int] = None,
) -> dict:
    nonce = w3.eth.get_transaction_count(signer.address, "pending")

    tx_params = {
        "from": signer.address,
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "value": value,
        "type": 2,
    }

    try:
        if gas_override:
            tx_params["gas"] = gas_override
        else:
            estimated = fn.estimate_gas({"from": signer.address, "value": value})
            tx_params["gas"] = _gas_with_buffer(estimated)
    except Exception as e:
        raise RuntimeError(f"Gas estimation failed (likely revert): {e}")

    # EIP-1559 with 2x basefee headroom so we never get rejected for "max fee < base fee"
    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.to_wei(0.1, "gwei"))
        priority = w3.to_wei(0.1, "gwei")
        tx_params["maxPriorityFeePerGas"] = priority
        tx_params["maxFeePerGas"] = int(base_fee * 2) + priority
    except Exception:
        tx_params["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        tx_params["maxFeePerGas"] = w3.to_wei(0.5, "gwei")

    tx = fn.build_transaction(tx_params)
    signed = signer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted on-chain: {tx_hash.hex()}")

    return {
        "tx_hash": tx_hash.hex(),
        "status": receipt.status,
        "block": receipt.blockNumber,
        "receipt": receipt,
    }


def parse_event(contract, event_name: str, receipt) -> list[dict]:
    """Decode all instances of `event_name` from a tx receipt."""
    event_obj = getattr(contract.events, event_name)
    logs = event_obj().process_receipt(receipt, errors="ignore")
    return [dict(e["args"]) for e in logs if e.get("args") is not None]


def verify_receipt(tx_hash: str) -> Optional[Any]:
    """
    Fetch a receipt for an already-submitted tx_hash (sent by the FE via MetaMask).
    Returns the receipt on success (status==1), None if not found / pending,
    raises if it reverted on-chain.
    """
    if not tx_hash or not tx_hash.startswith("0x"):
        return None
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    except Exception:
        return None
    if receipt is None:
        return None
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash}")
    return receipt


def is_connected() -> bool:
    try:
        return w3.is_connected() and w3.eth.chain_id == CHAIN_ID
    except Exception:
        return False