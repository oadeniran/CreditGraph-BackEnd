"""
Local sanity check: builds an EIP-712 payload, signs it with all 3 agents,
recovers the signers, and checks the sort order matches what ScoringOracle
will enforce on-chain. Burns zero gas.
"""

import secrets
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from core.contracts import w3, get, agent_signers
from services.underwriter import _build_typed_data, _sign_with_each_agent


def main():
    print("--- EIP-712 signing dry run ---\n")

    token_id = 1
    score = 720
    tier = 3
    reason_hash = Web3.keccak(text="example-reason")
    nonce = secrets.token_bytes(32)

    typed = _build_typed_data(token_id, score, tier, reason_hash, nonce)
    sigs = _sign_with_each_agent(typed)

    signable = encode_typed_data(full_message=typed)

    print("Agent addresses (unsorted):")
    for a in agent_signers:
        print(f"  {a.address}")

    print("\nRecovered signers from signatures (should be ascending):")
    recovered = []
    last_int = 0
    for sig in sigs:
        addr = Account.recover_message(signable, signature=sig)
        as_int = int(addr.lower(), 16)
        assert as_int > last_int, f"Signature order broken at {addr}"
        last_int = as_int
        recovered.append(addr)
        print(f"  {addr}")

    # Quorum check
    reg = get("AgentRegistry")
    print("\nAuthorization status:")
    auth_count = 0
    for addr in recovered:
        is_auth = reg.functions.isAuthorized(addr, 2).call()
        print(f"  {addr}: {is_auth}")
        if is_auth:
            auth_count += 1

    threshold = get("ScoringOracle").functions.quorumThreshold().call()
    print(f"\nQuorum threshold: {threshold}, authorized signers: {auth_count}")
    if auth_count >= threshold:
        print("✓ Quorum satisfied. submitScore would be accepted on-chain.")
    else:
        print("✗ Quorum NOT satisfied. Run bootstrap to register agents first.")

    print("\nDomain separator (from contract):")
    print(f"  {get('ScoringOracle').functions.domainSeparator().call().hex()}")


if __name__ == "__main__":
    main()