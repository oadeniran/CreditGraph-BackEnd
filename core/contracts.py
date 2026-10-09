"""
Multi-chain Web3 client factory.

Exposes:
  - admin_signer, agent_signers  : shared across chains
  - get_w3(chain_key)            : cached Web3 instance for that chain
  - get_contracts(chain_key)     : cached dict {name -> Contract}
  - get_contract(chain_key, name): one contract (raises if missing)
  - send_tx(chain_key, fn, signer): build/sign/broadcast on the given chain
  - role_hash(role_name)         : keccak256 of role string
  - verify_receipt(chain_key, tx_hash)
  - is_connected(chain_key)
"""

import hashlib
import logging
from typing import Any, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from eth_account.signers.local import LocalAccount

from envs import DEPLOYER_PRIVATE_KEY, ADMIN_SEED, AGENT_SEEDS
from core.chains import get_chain, is_configured, UnknownChain
from core.abis import load as load_abi

log = logging.getLogger("creditgraph.contracts")


# ----------------------------------------------------------------
# Signers (shared across all chains)
# ----------------------------------------------------------------

def _account_from_seed(seed: str) -> LocalAccount:
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
# Per-chain caches
# ----------------------------------------------------------------

_W3_CACHE: dict[str, Web3] = {}
_CONTRACTS_CACHE: dict[str, dict[str, Any]] = {}

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


def get_w3(chain_key: str) -> Web3:
    """Cached Web3 client for a chain."""
    if chain_key in _W3_CACHE:
        return _W3_CACHE[chain_key]

    chain = get_chain(chain_key)
    if not chain.get("rpc_url"):
        raise RuntimeError(f"Chain {chain_key} has no RPC URL configured")

    w3 = Web3(Web3.HTTPProvider(chain["rpc_url"], request_kwargs={"timeout": 30}))
    try:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass

    _W3_CACHE[chain_key] = w3
    return w3


def get_contracts(chain_key: str) -> dict[str, Any]:
    """Cached dict of contract objects for a chain."""
    if chain_key in _CONTRACTS_CACHE:
        return _CONTRACTS_CACHE[chain_key]

    if not is_configured(chain_key):
        raise RuntimeError(f"Chain {chain_key} is not fully configured (missing addresses or RPC)")

    chain = get_chain(chain_key)
    w3 = get_w3(chain_key)
    out = {}
    for name, abi_name in _ABI_NAME_BY_KEY.items():
        addr = chain["contracts"].get(name)
        if not addr:
            log.warning(f"[{chain_key}] no address for {name}, skipping")
            continue
        try:
            out[name] = w3.eth.contract(
                address=Web3.to_checksum_address(addr),
                abi=load_abi(abi_name),
            )
        except FileNotFoundError:
            log.error(f"Missing ABI file core/abis/{abi_name}.json")
        except Exception as e:
            log.error(f"[{chain_key}] failed to build {name}: {e}")

    _CONTRACTS_CACHE[chain_key] = out
    log.info(f"[{chain_key}] loaded {len(out)} contracts")
    return out


def get_contract(chain_key: str, name: str):
    """Convenience: fetch a specific contract on a chain, raise if missing."""
    contracts = get_contracts(chain_key)
    c = contracts.get(name)
    if c is None:
        raise RuntimeError(f"Contract '{name}' not loaded on chain '{chain_key}'")
    return c


# ----------------------------------------------------------------
# Role hashing
# ----------------------------------------------------------------

def role_hash(role_name: str) -> bytes:
    return Web3.keccak(text=role_name)


# ----------------------------------------------------------------
# Tx helpers (chain-aware)
# ----------------------------------------------------------------

def _gas_with_buffer(estimated: int) -> int:
    return int(estimated * 1.25) + 50_000


def send_tx(
    chain_key: str,
    fn,
    signer: LocalAccount,
    value: int = 0,
    gas_override: Optional[int] = None,
) -> dict:
    """
    Build, sign, send, wait for receipt on the specified chain.
    Returns {'tx_hash': '0x...', 'status': 1, 'block': N, 'receipt': <receipt>}.
    """
    w3 = get_w3(chain_key)
    chain = get_chain(chain_key)
    chain_id = chain["chain_id"]

    nonce = w3.eth.get_transaction_count(signer.address, "pending")

    tx_params = {
        "from": signer.address,
        "nonce": nonce,
        "chainId": chain_id,
        "value": value,
        "type": 2,
    }

    # --- Gas limit ---
    try:
        if gas_override:
            tx_params["gas"] = gas_override
        else:
            estimated = fn.estimate_gas({"from": signer.address, "value": value})
            tx_params["gas"] = _gas_with_buffer(estimated)
    except Exception as e:
        raise RuntimeError(f"Gas estimation failed on {chain_key} (likely revert): {e}")

    # --- Fee calculation with cap based on signer balance ---
    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.to_wei(0.1, "gwei"))
        priority = w3.to_wei(0.1, "gwei")

        # Headroom: 1.3x base + priority is enough for 1-2 block volatility.
        # (Was 2x — too aggressive on chains with high base fees like Monad.)
        desired_max_fee = int(base_fee * 13 // 10) + priority

        # Cap based on signer balance so we never reserve more than the signer can afford.
        # Reserve = gas * maxFeePerGas + value. We want this <= 90% of balance so
        # one tx doesn't eat the whole wallet.
        balance = w3.eth.get_balance(signer.address)
        gas_limit = tx_params["gas"]
        max_affordable_reservation = (balance * 90 // 100) - value
        if max_affordable_reservation <= 0:
            raise RuntimeError(
                f"Signer {signer.address} on {chain_key} has insufficient balance "
                f"({balance / 1e18} native) to cover value={value / 1e18}"
            )
        max_affordable_fee_per_gas = max_affordable_reservation // gas_limit

        # Pick the lower of what we want vs what fits in the wallet
        tx_params["maxFeePerGas"] = min(desired_max_fee, max_affordable_fee_per_gas)
        tx_params["maxPriorityFeePerGas"] = min(priority, tx_params["maxFeePerGas"])

        # Sanity: maxFeePerGas must be >= base_fee or chain rejects it
        if tx_params["maxFeePerGas"] < base_fee:
            raise RuntimeError(
                f"Signer {signer.address} on {chain_key} cannot afford current base fee: "
                f"needs {gas_limit * base_fee / 1e18} native but has {balance / 1e18}"
            )
    except RuntimeError:
        raise
    except Exception:
        tx_params["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        tx_params["maxFeePerGas"] = w3.to_wei(0.5, "gwei")

    tx = fn.build_transaction(tx_params)
    signed = signer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted on {chain_key}: {tx_hash.hex()}")

    return {
        "tx_hash": tx_hash.hex(),
        "status": receipt.status,
        "block": receipt.blockNumber,
        "receipt": receipt,
    }


def parse_event(chain_key: str, contract_name: str, event_name: str, receipt) -> list[dict]:
    """Decode all instances of `event_name` from a tx receipt."""
    contract = get_contract(chain_key, contract_name)
    event_obj = getattr(contract.events, event_name)
    from web3.logs import DISCARD
    logs = event_obj().process_receipt(receipt, errors=DISCARD)
    return [dict(e["args"]) for e in logs if e.get("args") is not None]


def verify_receipt(chain_key: str, tx_hash: str, timeout: int = 30) -> Optional[Any]:
    """Returns the receipt if mined + status==1, None if not found, raises if reverted."""
    if not tx_hash or not tx_hash.startswith("0x"):
        return None
    try:
        w3 = get_w3(chain_key)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception:
        return None
    if receipt is None:
        return None
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted on {chain_key}: {tx_hash}")
    return receipt


def is_connected(chain_key: str) -> bool:
    try:
        w3 = get_w3(chain_key)
        chain = get_chain(chain_key)
        return w3.is_connected() and w3.eth.chain_id == chain["chain_id"]
    except Exception:
        return False