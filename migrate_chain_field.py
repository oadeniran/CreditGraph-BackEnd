"""
One-off migration:
  1. Delete orphan docs with null/missing on-chain primary keys (legacy data
     from pre-indexer era that can't be reconciled with any on-chain record)
  2. Backfill `chain: "arbitrum_sepolia"` on remaining existing docs
  3. Drop old single-field unique indexes
  4. Rebuild compound indexes

Usage: python -m tools.migrate_chain_field
"""

import asyncio
from core.database import (
    ensure_indexes,
    loans_collection, attestations_collection, scores_collection,
    identities_collection, channels_collection,
    graduations_collection, defaults_collection, indexer_state_collection,
)

DEFAULT_BACKFILL_CHAIN = "arbitrum_sepolia"

# Collections and their on-chain primary-key field (if any).
# Docs with null/missing values here are orphans and will be deleted.
PK_FIELDS = {
    "loans": "loan_id_onchain",
    "attests": "attestation_id_onchain",
    "channels": "channel_id",
    "identities": "token_id",
}

COLLECTIONS = [
    identities_collection,
    loans_collection,
    attestations_collection,
    scores_collection,
    channels_collection,
    graduations_collection,
    defaults_collection,
    indexer_state_collection,
]


async def main():
    print("Step 1 — Deleting orphan docs with null/missing primary keys...\n")
    for coll in COLLECTIONS:
        pk = PK_FIELDS.get(coll.name)
        if not pk:
            continue
        orphan_filter = {"$or": [{pk: None}, {pk: {"$exists": False}}]}
        count = await coll.count_documents(orphan_filter)
        if count:
            result = await coll.delete_many(orphan_filter)
            print(f"  {coll.name:30s} deleted {result.deleted_count} orphans")
        else:
            print(f"  {coll.name:30s} no orphans")

    print(f"\nStep 2 — Backfilling 'chain' field with '{DEFAULT_BACKFILL_CHAIN}'...\n")
    for coll in COLLECTIONS:
        result = await coll.update_many(
            {"chain": {"$exists": False}},
            {"$set": {"chain": DEFAULT_BACKFILL_CHAIN}},
        )
        print(f"  {coll.name:30s} updated: {result.modified_count}")

    print("\nStep 3 — Dropping old single-field indexes (if present)...\n")
    for coll, old_index in [
        (loans_collection, "loan_id_onchain_1"),
        (attestations_collection, "attestation_id_onchain_1"),
        (identities_collection, "token_id_1"),
        (identities_collection, "wallet_address_1"),      # ← ADD THIS
        (channels_collection, "channel_id_1"),
        (indexer_state_collection, "key_1"),
    ]:
        try:
            await coll.drop_index(old_index)
            print(f"  dropped {coll.name}.{old_index}")
        except Exception as e:
            # OperationFailure if index doesn't exist — fine
            print(f"  skipped {coll.name}.{old_index}: {type(e).__name__}")

    print("\nStep 4 — Rebuilding compound indexes...\n")
    await ensure_indexes()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())