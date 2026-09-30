"""
MongoDB layer. Note: chain is canonical, Mongo is a cache + history index.
Collections:
  users          -> wallet -> token_id  (cached)
  loans          -> indexed loan events
  attests        -> indexed attestation events
  scores         -> indexed score finalizations
  identities     -> indexed identity mints
  graduations    -> tier promotions
  defaults       -> default events
  channels       -> x402 channels (indexed)
  receipts       -> x402 settlement receipts
  indexer_state  -> {key: 'last_block', value: int}
"""

from motor.motor_asyncio import AsyncIOMotorClient
from envs import MONGO_URI

client = AsyncIOMotorClient(MONGO_URI)
db = client.creditgraph_db

# Existing
users_collection = db.users
scores_collection = db.scores
loans_collection = db.loans
attestations_collection = db.attests

# New
identities_collection = db.identities
graduations_collection = db.graduations
defaults_collection = db.defaults
channels_collection = db.channels
receipts_collection = db.receipts
indexer_state_collection = db.indexer_state


async def ensure_indexes():
    """Create unique + lookup indexes. Idempotent."""
    await users_collection.create_index("wallet_address", unique=True)
    await loans_collection.create_index("loan_id_onchain", unique=True, sparse=True)
    await loans_collection.create_index([("wallet_address", 1), ("originated_at", -1)])
    await attestations_collection.create_index("attestation_id_onchain", unique=True, sparse=True)
    await attestations_collection.create_index("attester_address")
    await attestations_collection.create_index("subject_address")
    await scores_collection.create_index([("token_id", 1), ("updated_at", -1)])
    await identities_collection.create_index("wallet_address", unique=True)
    await identities_collection.create_index("token_id", unique=True)
    await channels_collection.create_index("channel_id", unique=True)
    await indexer_state_collection.create_index("key", unique=True)