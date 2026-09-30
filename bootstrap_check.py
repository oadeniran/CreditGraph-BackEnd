"""
Verifies the bootstrap will work end-to-end. Prints:
  - ETH balances of admin and all agents
  - USDC balances
  - whether admin holds the relevant on-chain roles
  - whether each agent is already registered as Underwriter
  - what bootstrap is about to do (without actually doing it yet)

Then optionally runs the real bootstrap with --run.
"""

import sys
from web3 import Web3
from core.contracts import w3, contracts, admin_signer, agent_signers, role_hash, is_connected
from envs import AGENT_STAKE_USDC

USDC_DECIMALS = 6
AGENT_ROLE_UNDERWRITER = 2


def print_balances(label, addresses):
    usdc = contracts["USDC"]
    reg = contracts["AgentRegistry"]
    print(f"\n--- {label} ---")
    for addr in addresses:
        addr = Web3.to_checksum_address(addr)
        eth = w3.eth.get_balance(addr) / 1e18
        try:
            usdc_bal = usdc.functions.balanceOf(addr).call() / (10 ** USDC_DECIMALS)
        except Exception:
            usdc_bal = 0.0
        try:
            is_auth = reg.functions.isAuthorized(addr, AGENT_ROLE_UNDERWRITER).call()
        except Exception:
            is_auth = False
        print(f"  {addr}")
        print(f"    ETH:  {eth:.6f}")
        print(f"    USDC: {usdc_bal:.2f}")
        print(f"    registered Underwriter: {is_auth}")


def print_admin_roles():
    ac = contracts["AccessController"]
    print(f"\n--- Admin role check ({admin_signer.address}) ---")
    for role in ["DEFAULT_ADMIN_ROLE", "CONFIG_ROLE", "MINTER_ROLE", "SCORE_UPDATER_ROLE",
                 "BRIDGE_ROLE", "POOL_MANAGER_ROLE", "SLASHER_ROLE", "PAUSER_ROLE"]:
        # DEFAULT_ADMIN_ROLE is bytes32(0), not keccak
        if role == "DEFAULT_ADMIN_ROLE":
            role_id = b"\x00" * 32
        else:
            role_id = role_hash(role)
        try:
            has = ac.functions.hasRole(role_id, admin_signer.address).call()
        except Exception as e:
            has = f"err: {e}"
        print(f"  {role}: {has}")


def estimate_costs():
    print("\n--- Estimated costs ---")
    needed_per_agent = 0.0005  # for register + approve
    needed_admin_for_topups = 0.0005 * 3 + 0.001  # 3 transfers + buffer
    print(f"  Per agent ETH needed:        {needed_per_agent}")
    print(f"  Admin ETH needed for top-ups: {needed_admin_for_topups}")
    print(f"  Per agent USDC stake:         {AGENT_STAKE_USDC}")
    print(f"  Total agent USDC (3 agents):  {AGENT_STAKE_USDC * 3}")


def maybe_run_bootstrap():
    if "--run" not in sys.argv:
        print("\n(Dry run. Pass --run to actually execute bootstrap.)")
        return
    print("\n--- Running bootstrap ---")
    from core.bootstrap import run_bootstrap
    status = run_bootstrap()
    print("\n--- Bootstrap status ---")
    for k, v in status.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    print(f"Connected: {is_connected()}")
    print(f"Chain ID:  {w3.eth.chain_id}")

    print_balances("Admin", [admin_signer.address])
    print_balances("Agents", [a.address for a in agent_signers])
    print_admin_roles()
    estimate_costs()
    maybe_run_bootstrap()

    print("\n--- Post-run balances ---")
    print_balances("Admin", [admin_signer.address])
    print_balances("Agents", [a.address for a in agent_signers])