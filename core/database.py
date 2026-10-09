"""
MongoDB layer. Every doc carries a `chain` field; indexes are compound
on (chain, primary_key).
"""

from motor.motor_asyncio import AsyncIOMotorClient
from envs import MONGO_URI

client = AsyncIOMotorClient(MONGO_URI)
db = client.creditgraph_db

users_collection = db.users
scores_collection = db.scores
loans_collection = db.loans
attestations_collection = db.attests
identities_collection = db.identities
graduations_collection = db.graduations
defaults_collection = db.defaults
channels_collection = db.channels
receipts_collection = db.receipts
indexer_state_collection = db.indexer_state


async def ensure_indexes():
    """Create compound indexes keyed by (chain, primary_key). Idempotent."""
    await users_collection.create_index("wallet_address", unique=True)

    await loans_collection.create_index([("chain", 1), ("loan_id_onchain", 1)], unique=True, sparse=True)
    await loans_collection.create_index([("chain", 1), ("wallet_address", 1), ("originated_at", -1)])

    await attestations_collection.create_index(
        [("chain", 1), ("attestation_id_onchain", 1)], unique=True, sparse=True
    )
    await attestations_collection.create_index([("chain", 1), ("attester_address", 1)])
    await attestations_collection.create_index([("chain", 1), ("subject_address", 1)])

    await scores_collection.create_index([("chain", 1), ("token_id", 1), ("updated_at", -1)])

    await identities_collection.create_index([("chain", 1), ("wallet_address", 1)], unique=True)
    await identities_collection.create_index([("chain", 1), ("token_id", 1)], unique=True)

    await channels_collection.create_index([("chain", 1), ("channel_id", 1)], unique=True)

    await indexer_state_collection.create_index("key", unique=True)