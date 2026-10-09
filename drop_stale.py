# tools/drop_stale_indexes.py
import asyncio
from core.database import identities_collection, loans_collection, attestations_collection, channels_collection

async def main():
    stale = [
        (identities_collection, "wallet_address_1"),
        (identities_collection, "token_id_1"),
        (loans_collection, "loan_id_onchain_1"),
        (attestations_collection, "attestation_id_onchain_1"),
        (channels_collection, "channel_id_1"),
    ]
    for coll, name in stale:
        try:
            await coll.drop_index(name)
            print(f"✓ dropped {coll.name}.{name}")
        except Exception as e:
            print(f"— skipped {coll.name}.{name}: {type(e).__name__}")

    # List remaining indexes to confirm
    print("\nRemaining identities indexes:")
    async for idx in identities_collection.list_indexes():
        print(f"  {idx.get('name')}: {dict(idx.get('key', {}))}")

if __name__ == "__main__":
    asyncio.run(main())