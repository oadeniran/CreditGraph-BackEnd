# from core.contracts import w3, contracts, admin_signer, agent_signers, is_connected
# print("connected:", is_connected())
# print("admin:", admin_signer.address)
# print("agents:", [a.address for a in agent_signers])
# print("contracts loaded:", list(contracts.keys()))

# from core.contracts import contracts, admin_signer
# oracle = contracts["ScoringOracle"]
# reg = contracts["AgentRegistry"]

# print("challenge_period:", oracle.functions.challengePeriod().call())
# print("min_stake:", reg.functions.minStake(2).call() / 1e6, "USDC")
# print("quorum:", oracle.functions.quorumThreshold().call())

import asyncio
from web3 import Web3
from core.contracts import contracts, admin_signer, send_tx
from services.underwriter import underwrite
from services import chain_reader

# Pick any address you control or any random one
TEST_WALLET = "0xdb25eccf39Be5C744919C597A68D70d0C1DBb5dC"  # change to your wallet for a real test

# Step 1: Mint identity
metadata = Web3.keccak(text=f"test:{TEST_WALLET}")
result = send_tx(
    contracts["CreditIdentity"].functions.mint(TEST_WALLET, metadata),
    admin_signer
)
print("mint tx:", result["tx_hash"])
print("token_id:", chain_reader.token_id_of(TEST_WALLET))

# Step 2: Underwrite (quorum signs + submits + finalizes since period=0)
res = asyncio.run(underwrite(TEST_WALLET))
print("\nUnderwriting result:")
for k, v in res.items():
    print(f"  {k}: {v}")

# Step 3: Read the score back from ScoreRegistry
print("\nFinal score on-chain:", chain_reader.get_score(chain_reader.token_id_of(TEST_WALLET)))