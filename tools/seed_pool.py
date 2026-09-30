"""
Seeds the LendingPool with USDC so borrows can actually fund.
Run once at the start of a demo session.

  python -m tools.seed_pool [amount_usdc]
"""

import sys
from core.contracts import contracts, admin_signer, send_tx

def main():
    amount_usdc = float(sys.argv[1]) if len(sys.argv) > 1 else 1000
    units = int(amount_usdc * 1e6)

    usdc = contracts["USDC"]
    pool = contracts["LendingPool"]
    admin = admin_signer.address

    print(f"Seeding LendingPool with ${amount_usdc} USDC from admin {admin}")

    # 1. Mint USDC to admin (MockUSDC.mintTo is permissionless)
    print("  [1/3] Minting USDC to admin...")
    send_tx(usdc.functions.mintTo(admin, units), admin_signer)

    # 2. Approve pool
    print("  [2/3] Approving pool to spend USDC...")
    send_tx(usdc.functions.approve(pool.address, units), admin_signer)

    # 3. Deposit
    print("  [3/3] Depositing into pool...")
    result = send_tx(pool.functions.deposit(units, admin), admin_signer)
    print(f"     ✓ tx: {result['tx_hash']}")

    total_assets = pool.functions.totalAssets().call() / 1e6
    admin_shares = pool.functions.balanceOf(admin).call() / 1e6
    print(f"\nPool now holds: ${total_assets} USDC")
    print(f"Admin holds:    {admin_shares} cgUSDC shares")


if __name__ == "__main__":
    main()