"""
Verifies and runs bootstrap for a specific chain.

  python -m tools.bootstrap_check                       # dry run, default chain
  python -m tools.bootstrap_check --chain monad_testnet # dry run on monad
  python -m tools.bootstrap_check --run                 # execute on default
  python -m tools.bootstrap_check --chain monad_testnet --run
"""

import sys
from web3 import Web3

from core.contracts import get_w3, get_contracts, admin_signer, agent_signers, role_hash
from core.chains import DEFAULT_CHAIN_KEY, is_configured, get_chain
from envs import AGENT_STAKE_USDC

USDC_DECIMALS = 6
AGENT_ROLE_UNDERWRITER = 2


def parse_chain_arg() -> str:
    if "--chain" in sys.argv:
        idx = sys.argv.index("--chain")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return DEFAULT_CHAIN_KEY


def print_balances(chain_key, label, addresses):
    contracts = get_contracts(chain_key)
    w3 = get_w3(chain_key)
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
        native = get_chain(chain_key).get("native_symbol", "ETH")
        print(f"  {addr}")
        print(f"    {native}:  {eth:.6f}")
        print(f"    USDC:     {usdc_bal:.2f}")
        print(f"    registered Underwriter: {is_auth}")


def print_admin_roles(chain_key):
    contracts = get_contracts(chain_key)
    ac = contracts["AccessController"]
    print(f"\n--- Admin role check on {chain_key} ({admin_signer.address}) ---")
    for role in ["DEFAULT_ADMIN_ROLE", "CONFIG_ROLE", "MINTER_ROLE", "SCORE_UPDATER_ROLE",
                 "BRIDGE_ROLE", "POOL_MANAGER_ROLE", "SLASHER_ROLE", "PAUSER_ROLE"]:
        if role == "DEFAULT_ADMIN_ROLE":
            role_id = b"\x00" * 32
        else:
            role_id = role_hash(role)
        try:
            has = ac.functions.hasRole(role_id, admin_signer.address).call()
        except Exception as e:
            has = f"err: {e}"
        print(f"  {role}: {has}")


def main():
    chain_key = parse_chain_arg()
    print(f"Target chain: {chain_key}")

    if not is_configured(chain_key):
        print(f"❌ Chain {chain_key} is not fully configured in .env. Fill in RPC + addresses.")
        sys.exit(1)

    w3 = get_w3(chain_key)
    print(f"Connected: {w3.is_connected()}")
    print(f"Chain ID:  {w3.eth.chain_id}")

    print_balances(chain_key, "Admin", [admin_signer.address])
    print_balances(chain_key, "Agents", [a.address for a in agent_signers])
    print_admin_roles(chain_key)

    print(f"\n--- Estimated costs ---")
    print(f"  Per agent gas needed: 0.0005 native")
    print(f"  Admin gas for top-ups: 0.0025 native")
    print(f"  Per agent USDC stake: {AGENT_STAKE_USDC}")
    print(f"  Total agent USDC:     {AGENT_STAKE_USDC * 3}")

    if "--run" not in sys.argv:
        print("\n(Dry run. Pass --run to actually execute bootstrap.)")
        return

    print(f"\n--- Running bootstrap on {chain_key} ---")
    from core.bootstrap import run_bootstrap
    status = run_bootstrap(chain_key)
    print("\n--- Bootstrap status ---")
    for k, v in status.items():
        print(f"  {k}: {v}")

    print("\n--- Post-run balances ---")
    print_balances(chain_key, "Admin", [admin_signer.address])
    print_balances(chain_key, "Agents", [a.address for a in agent_signers])


if __name__ == "__main__":
    main()