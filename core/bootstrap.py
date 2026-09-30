"""
One-shot startup tasks:
  - sanity check chain connection
  - ensure admin has USDC + ETH (logs warnings if not)
  - ensure 3 agent signers have USDC, are registered + staked as Underwriters
  - try to set ScoringOracle.challengePeriod = 0 (best-effort)
  - log role health (who has CONFIG_ROLE, MINTER_ROLE, SCORE_UPDATER_ROLE, etc.)
"""

import logging
from web3 import Web3

from envs import AGENT_STAKE_USDC, TRY_SET_CHALLENGE_PERIOD_ZERO, CHAIN_ID
from core.contracts import (
    w3, contracts, admin_signer, agent_signers,
    send_tx, role_hash, get, is_connected,
)

log = logging.getLogger("creditgraph.bootstrap")

USDC_DECIMALS = 6
AGENT_ROLE_UNDERWRITER = 2  # DataTypes.AgentRole.Underwriter

AGENT_GAS_TOPUP_WEI = w3.to_wei(0.0005, "ether")
MIN_AGENT_GAS_WEI = w3.to_wei(0.0001, "ether")


def _topup_agent_gas(agent_address: str) -> None:
    """Admin sends a tiny amount of ETH to an agent that has zero gas."""
    agent_address = Web3.to_checksum_address(agent_address)
    bal = w3.eth.get_balance(agent_address)
    if bal >= MIN_AGENT_GAS_WEI:
        log.info(f"Agent {agent_address} already has {bal / 1e18:.6f} ETH, skipping top-up")
        return

    admin_bal = w3.eth.get_balance(admin_signer.address)
    if admin_bal < AGENT_GAS_TOPUP_WEI * 2:
        log.warning(
            f"Admin too low on ETH ({admin_bal / 1e18:.6f}) to top up agent {agent_address}"
        )
        return

    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.to_wei(0.1, "gwei"))
        priority = w3.to_wei(0.1, "gwei")
        max_fee = int(base_fee * 2) + priority

        # Let the node estimate the gas needed; fall back to 100k if it can't
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
            "chainId": CHAIN_ID,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        }
        signed = admin_signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            log.info(
                f"Topped up agent {agent_address[:8]}... with {AGENT_GAS_TOPUP_WEI / 1e18} ETH "
                f"(tx: {tx_hash.hex()}, gas used: {receipt.gasUsed})"
            )
        else:
            log.warning(f"Top-up tx reverted: {tx_hash.hex()}")
    except Exception as e:
        log.warning(f"Gas top-up failed for {agent_address}: {e}")


def _usdc_units(amount: float) -> int:
    return int(amount * (10 ** USDC_DECIMALS))


def _has_role(role_name: str, account: str) -> bool:
    try:
        ac = get("AccessController")
        return ac.functions.hasRole(role_hash(role_name), Web3.to_checksum_address(account)).call()
    except Exception as e:
        log.warning(f"hasRole check failed for {role_name} / {account}: {e}")
        return False


def _ensure_usdc(target_address: str, units: int) -> None:
    """Top up an address with USDC via MockUSDC.faucet (called from that address)."""
    usdc = get("USDC")
    bal = usdc.functions.balanceOf(Web3.to_checksum_address(target_address)).call()
    if bal >= units:
        return
    needed = units - bal
    log.info(f"USDC top-up: {target_address} needs {needed / 1e6} USDC")
    # faucet() mints to msg.sender. We need the target to call faucet themselves.
    # We can't do that for arbitrary addresses unless we hold their key, so this
    # is only used for keys we own (admin + agents). MockUSDC also exposes
    # mintTo(address,uint256) which is open — use that from admin to be safe.
    try:
        fn = usdc.functions.mintTo(Web3.to_checksum_address(target_address), needed)
        send_tx(fn, admin_signer)
        log.info(f"Minted {needed / 1e6} USDC to {target_address}")
    except Exception as e:
        log.warning(f"mintTo failed (likely insufficient ETH for admin), retrying with faucet path: {e}")


def _read_or_set_agent_min_stake(desired_units: int) -> int:
       """
       Returns the actual on-chain minStake to use.
       If admin has CONFIG_ROLE and current != desired, tries to set it.
       Otherwise returns whatever is on-chain so we don't try to register
       with less than the contract requires.
       """
       reg = get("AgentRegistry")
       try:
           current = reg.functions.minStake(AGENT_ROLE_UNDERWRITER).call()
       except Exception:
           current = 0

       if current == desired_units:
           return current

       if _has_role("CONFIG_ROLE", admin_signer.address):
           try:
               fn = reg.functions.setMinStake(AGENT_ROLE_UNDERWRITER, desired_units)
               send_tx(fn, admin_signer)
               log.info(f"Set Underwriter minStake to {desired_units / 1e6} USDC")
               return desired_units
           except Exception as e:
               log.warning(f"setMinStake failed: {e}")
               return current

       log.warning(
           f"Admin lacks CONFIG_ROLE — using on-chain minStake of {current / 1e6} USDC "
           f"(your .env wanted {desired_units / 1e6})"
       )
       return current


def _register_and_stake_agent(agent_signer, stake_units: int) -> None:
    reg = get("AgentRegistry")
    usdc = get("USDC")

    # Already active as Underwriter?
    try:
        active = reg.functions.isAuthorized(agent_signer.address, AGENT_ROLE_UNDERWRITER).call()
        if active:
            log.info(f"Agent {agent_signer.address} already authorized as Underwriter")
            return
    except Exception as e:
        log.warning(f"isAuthorized check failed for {agent_signer.address}: {e}")
    
    _topup_agent_gas(agent_signer.address)

    # Fund agent with USDC (admin mints to them)
    _ensure_usdc(agent_signer.address, stake_units)

    # Agent must have ETH to pay gas. We can't fund them automatically — log it.
    eth_bal = w3.eth.get_balance(agent_signer.address)
    if eth_bal == 0:
        log.warning(
            f"Agent {agent_signer.address} has 0 ETH. Send ~0.001 ETH on Arbitrum Sepolia "
            f"so they can register + sign. Skipping registration."
        )
        return

    # Approve USDC to AgentRegistry
    try:
        approve_fn = usdc.functions.approve(reg.address, stake_units)
        send_tx(approve_fn, agent_signer)
        # Register as Underwriter
        register_fn = reg.functions.register(AGENT_ROLE_UNDERWRITER, stake_units)
        send_tx(register_fn, agent_signer)
        log.info(f"Agent {agent_signer.address} registered + staked {stake_units / 1e6} USDC")
    except Exception as e:
        log.warning(f"Agent register failed for {agent_signer.address}: {e}")


def _try_zero_challenge_period() -> None:
    if not TRY_SET_CHALLENGE_PERIOD_ZERO:
        return
    oracle = get("ScoringOracle")
    try:
        current = oracle.functions.challengePeriod().call()
    except Exception:
        return
    if current == 0:
        log.info("ScoringOracle.challengePeriod already 0")
        return
    if not _has_role("CONFIG_ROLE", admin_signer.address):
        log.info(
            f"Admin lacks CONFIG_ROLE — leaving challengePeriod at {current}s. "
            f"Demo will route through pending → finalize flow."
        )
        return
    try:
        send_tx(oracle.functions.setChallengePeriod(0), admin_signer)
        log.info("Set ScoringOracle.challengePeriod to 0")
    except Exception as e:
        log.warning(f"setChallengePeriod(0) failed: {e}")


def _log_role_health() -> None:
    log.info("--- Role health check ---")
    addr = admin_signer.address
    for role in ["CONFIG_ROLE", "MINTER_ROLE", "SCORE_UPDATER_ROLE", "BRIDGE_ROLE",
                 "POOL_MANAGER_ROLE", "SLASHER_ROLE", "PAUSER_ROLE"]:
        has = _has_role(role, addr)
        log.info(f"  admin {addr[:8]}... {role}: {'YES' if has else 'no'}")
    log.info("-------------------------")


def run_bootstrap() -> dict:
    """Run the bootstrap sequence. Returns a status dict for /api/health."""
    status = {
        "connected": False,
        "admin_address": admin_signer.address,
        "agent_addresses": [a.address for a in agent_signers],
        "admin_eth": 0,
        "agents_registered": [False, False, False],
        "challenge_period": None,
        "errors": [],
    }
    try:
        status["connected"] = is_connected()
        if not status["connected"]:
            status["errors"].append("RPC not connected or wrong chain")
            return status

        status["admin_eth"] = w3.eth.get_balance(admin_signer.address) / 1e18
        if status["admin_eth"] < 0.005:
            log.warning(
                f"Admin has only {status['admin_eth']} ETH on Arbitrum Sepolia. "
                f"Fund {admin_signer.address} to enable admin operations."
            )

        desired_stake_units = _usdc_units(AGENT_STAKE_USDC)
        stake_units = _read_or_set_agent_min_stake(desired_stake_units)

        for i, agent in enumerate(agent_signers):
            try:
                _register_and_stake_agent(agent, stake_units)
            except Exception as e:
                log.warning(f"Agent {i+1} bootstrap error: {e}")

        # Read current state
        reg = get("AgentRegistry")
        for i, agent in enumerate(agent_signers):
            try:
                status["agents_registered"][i] = reg.functions.isAuthorized(
                    agent.address, AGENT_ROLE_UNDERWRITER
                ).call()
            except Exception:
                pass

        _try_zero_challenge_period()
        try:
            status["challenge_period"] = get("ScoringOracle").functions.challengePeriod().call()
        except Exception:
            pass

        _log_role_health()
    except Exception as e:
        log.error(f"Bootstrap error: {e}")
        status["errors"].append(str(e))

    return status