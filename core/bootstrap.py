"""
Chain-aware bootstrap. Runs per chain, idempotent.

Usage:
  from core.bootstrap import run_bootstrap
  status = run_bootstrap("arbitrum_sepolia")
  status = run_bootstrap("monad_testnet")

Or on startup:
  bootstrap_all_configured_chains()
"""

import logging
from time import time
from web3 import Web3

from envs import AGENT_STAKE_USDC, TRY_SET_CHALLENGE_PERIOD_ZERO
from core.chains import list_chains, get_chain
from core.contracts import (
    get_w3, get_contracts, get_contract, admin_signer, agent_signers,
    send_tx, role_hash,
)

log = logging.getLogger("creditgraph.bootstrap")

USDC_DECIMALS = 6
AGENT_ROLE_UNDERWRITER = 2
AGENT_GAS_TOPUP_WEI = Web3.to_wei(0.15, "ether")   # 100x — covers approve + register on Monad with margin
MIN_AGENT_GAS_WEI = Web3.to_wei(0.1, "ether")     # above this, skip top-up


def _usdc_units(amount: float) -> int:
    return int(amount * (10 ** USDC_DECIMALS))


def _has_role(chain_key: str, role_name: str, account: str) -> bool:
    try:
        ac = get_contract(chain_key, "AccessController")
        return ac.functions.hasRole(role_hash(role_name), Web3.to_checksum_address(account)).call()
    except Exception as e:
        log.warning(f"[{chain_key}] hasRole check failed for {role_name}/{account}: {e}")
        return False


def _ensure_usdc(chain_key: str, target_address: str, units: int, target_signer=None) -> None:
    """
    Ensures target has `units` of USDC. Strategies tried in order:
      1. If target already has enough, done.
      2. admin.mintTo(target, needed) — works on permissionless MockUSDC
      3. target.faucet(needed) — target self-mints if contract supports it
      4. admin.transfer(target, needed) — admin sends from their own balance
    If all fail, logs warning and continues.
    """
    usdc = get_contract(chain_key, "USDC")
    target_cs = Web3.to_checksum_address(target_address)
    bal = usdc.functions.balanceOf(target_cs).call()
    if bal >= units:
        return
    needed = units - bal
    log.info(f"[{chain_key}] USDC top-up: {target_address} needs {needed / 1e6} USDC")

    # Path 1: admin mints to target (permissionless MockUSDC)
    try:
        fn = usdc.functions.mintTo(target_cs, needed)
        send_tx(chain_key, fn, admin_signer)
        log.info(f"[{chain_key}] mintTo: minted {needed / 1e6} USDC to {target_address}")
        return
    except Exception as e:
        log.info(f"[{chain_key}] mintTo unavailable, trying faucet()")

    # Path 2: target calls faucet() on itself
    if target_signer is not None:
        try:
            fn = usdc.functions.faucet(needed)
            send_tx(chain_key, fn, target_signer)
            log.info(f"[{chain_key}] faucet(): {target_address} self-minted {needed / 1e6} USDC")
            return
        except Exception as e:
            log.info(f"[{chain_key}] faucet() unavailable, trying admin transfer")

    # Path 3: admin transfers from their own USDC balance
    admin_bal = usdc.functions.balanceOf(admin_signer.address).call()
    if admin_bal < needed:
        log.warning(
            f"[{chain_key}] Admin has {admin_bal / 1e6} USDC but needs to send {needed / 1e6} to "
            f"{target_address}. Top up admin at the Circle faucet and restart."
        )
        return
    try:
        fn = usdc.functions.transfer(target_cs, needed)
        send_tx(chain_key, fn, admin_signer)
        log.info(f"[{chain_key}] admin.transfer: sent {needed / 1e6} USDC to {target_address}")
    except Exception as e:
        log.warning(f"[{chain_key}] All USDC top-up paths failed: {e}")


def _topup_agent_gas(chain_key: str, agent_address: str) -> None:
    """Admin sends a tiny amount of native currency to an agent that has none."""
    w3 = get_w3(chain_key)
    chain_id = get_chain(chain_key)["chain_id"]
    agent_address = Web3.to_checksum_address(agent_address)

    bal = w3.eth.get_balance(agent_address)
    if bal >= MIN_AGENT_GAS_WEI:
        log.info(f"[{chain_key}] Agent {agent_address} has {bal / 1e18:.6f} native, skip top-up")
        return

    admin_bal = w3.eth.get_balance(admin_signer.address)
    if admin_bal < AGENT_GAS_TOPUP_WEI * 2:
        log.warning(
            f"[{chain_key}] Admin low on native ({admin_bal / 1e18:.6f}) — can't top up {agent_address}"
        )
        return

    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.to_wei(0.1, "gwei"))
        priority = w3.to_wei(0.1, "gwei")
        max_fee = int(base_fee * 2) + priority

        try:
            estimated = w3.eth.estimate_gas({
                "from": admin_signer.address,
                "to": agent_address,
                "value": AGENT_GAS_TOPUP_WEI,
            })
            gas_limit = int(estimated * 1.5) + 10_000
        except Exception:
            gas_limit = 100_000

        tx = {
            "from": admin_signer.address,
            "to": agent_address,
            "value": AGENT_GAS_TOPUP_WEI,
            "nonce": w3.eth.get_transaction_count(admin_signer.address, "pending"),
            "chainId": chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        }
        signed = admin_signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            log.info(f"[{chain_key}] Topped up {agent_address[:8]}... ({AGENT_GAS_TOPUP_WEI / 1e18})")
            time.sleep(2)  # wait for balance to update before next step
        else:
            log.warning(f"[{chain_key}] Top-up tx reverted: {tx_hash.hex()}")
    except Exception as e:
        log.warning(f"[{chain_key}] Gas top-up failed for {agent_address}: {e}")


def _read_or_set_agent_min_stake(chain_key: str, desired_units: int) -> int:
    reg = get_contract(chain_key, "AgentRegistry")
    try:
        current = reg.functions.minStake(AGENT_ROLE_UNDERWRITER).call()
    except Exception:
        current = 0

    if current == desired_units:
        return current

    if _has_role(chain_key, "CONFIG_ROLE", admin_signer.address):
        try:
            fn = reg.functions.setMinStake(AGENT_ROLE_UNDERWRITER, desired_units)
            send_tx(chain_key, fn, admin_signer)
            log.info(f"[{chain_key}] Set Underwriter minStake to {desired_units / 1e6} USDC")
            return desired_units
        except Exception as e:
            log.warning(f"[{chain_key}] setMinStake failed: {e}")
            return current

    log.warning(
        f"[{chain_key}] Admin lacks CONFIG_ROLE — using on-chain minStake of "
        f"{current / 1e6} USDC (wanted {desired_units / 1e6})"
    )
    return current


def _register_and_stake_agent(chain_key: str, agent_signer, stake_units: int) -> None:
    reg = get_contract(chain_key, "AgentRegistry")
    usdc = get_contract(chain_key, "USDC")

    try:
        if reg.functions.isAuthorized(agent_signer.address, AGENT_ROLE_UNDERWRITER).call():
            log.info(f"[{chain_key}] Agent {agent_signer.address} already Underwriter")
            return
    except Exception as e:
        log.warning(f"[{chain_key}] isAuthorized check failed for {agent_signer.address}: {e}")

    _topup_agent_gas(chain_key, agent_signer.address)
    _ensure_usdc(chain_key, agent_signer.address, stake_units, target_signer=agent_signer)

    w3 = get_w3(chain_key)
    eth_bal = w3.eth.get_balance(agent_signer.address)
    if eth_bal == 0:
        log.warning(f"[{chain_key}] Agent {agent_signer.address} has 0 native. Skipping registration.")
        return

    try:
        approve_fn = usdc.functions.approve(reg.address, stake_units)
        send_tx(chain_key, approve_fn, agent_signer)
        register_fn = reg.functions.register(AGENT_ROLE_UNDERWRITER, stake_units)
        send_tx(chain_key, register_fn, agent_signer)
        log.info(f"[{chain_key}] Agent {agent_signer.address} registered + staked {stake_units / 1e6} USDC")
    except Exception as e:
        log.warning(f"[{chain_key}] Agent register failed for {agent_signer.address}: {e}")


def _try_zero_challenge_period(chain_key: str) -> None:
    if not TRY_SET_CHALLENGE_PERIOD_ZERO:
        return
    oracle = get_contract(chain_key, "ScoringOracle")
    try:
        current = oracle.functions.challengePeriod().call()
    except Exception:
        return
    if current == 0:
        log.info(f"[{chain_key}] challengePeriod already 0")
        return
    if not _has_role(chain_key, "CONFIG_ROLE", admin_signer.address):
        log.info(f"[{chain_key}] Admin lacks CONFIG_ROLE — leaving challengePeriod at {current}s")
        return
    try:
        send_tx(chain_key, oracle.functions.setChallengePeriod(0), admin_signer)
        log.info(f"[{chain_key}] Set challengePeriod to 0")
    except Exception as e:
        log.warning(f"[{chain_key}] setChallengePeriod(0) failed: {e}")


def _log_role_health(chain_key: str) -> None:
    log.info(f"--- [{chain_key}] Role health check ---")
    addr = admin_signer.address
    for role in ["CONFIG_ROLE", "MINTER_ROLE", "SCORE_UPDATER_ROLE", "BRIDGE_ROLE",
                 "POOL_MANAGER_ROLE", "SLASHER_ROLE", "PAUSER_ROLE"]:
        has = _has_role(chain_key, role, addr)
        log.info(f"  [{chain_key}] admin {addr[:8]}... {role}: {'YES' if has else 'no'}")


def run_bootstrap(chain_key: str) -> dict:
    """Bootstrap one chain. Idempotent, safe to re-run."""
    status = {
        "chain_key": chain_key,
        "connected": False,
        "admin_address": admin_signer.address,
        "agent_addresses": [a.address for a in agent_signers],
        "admin_native": 0,
        "agents_registered": [False, False, False],
        "challenge_period": None,
        "errors": [],
    }

    try:
        w3 = get_w3(chain_key)
        chain = get_chain(chain_key)
        status["connected"] = w3.is_connected() and w3.eth.chain_id == chain["chain_id"]
        if not status["connected"]:
            status["errors"].append(f"RPC not connected or wrong chain id")
            return status

        status["admin_native"] = w3.eth.get_balance(admin_signer.address) / 1e18
        if status["admin_native"] < 0.005:
            log.warning(f"[{chain_key}] Admin has only {status['admin_native']} {chain['native_symbol']}")

        desired_stake_units = _usdc_units(AGENT_STAKE_USDC)
        stake_units = _read_or_set_agent_min_stake(chain_key, desired_stake_units)

        for i, agent in enumerate(agent_signers):
            try:
                _register_and_stake_agent(chain_key, agent, stake_units)
            except Exception as e:
                log.warning(f"[{chain_key}] Agent {i+1} bootstrap error: {e}")

        reg = get_contract(chain_key, "AgentRegistry")
        for i, agent in enumerate(agent_signers):
            try:
                status["agents_registered"][i] = reg.functions.isAuthorized(
                    agent.address, AGENT_ROLE_UNDERWRITER
                ).call()
            except Exception:
                pass

        _try_zero_challenge_period(chain_key)
        try:
            status["challenge_period"] = get_contract(chain_key, "ScoringOracle").functions.challengePeriod().call()
        except Exception:
            pass

        _log_role_health(chain_key)
    except Exception as e:
        log.error(f"[{chain_key}] Bootstrap error: {e}")
        status["errors"].append(str(e))

    return status


def bootstrap_all_configured_chains() -> dict[str, dict]:
    """Runs bootstrap on every chain that's configured. For app startup."""
    results = {}
    for chain in list_chains():
        key = chain["key"]
        log.info(f"=== Bootstrapping {key} ===")
        results[key] = run_bootstrap(key)
    return results