"""Tables without models: dict-level items, PartiQL and DynamoDB's typed JSON.

Useful for migrations, admin tools and tables owned by other services.

Run: uv run python examples/raw_tables.py
"""

from __future__ import annotations

import asyncio

import dynamoxy
from dynamoxy import F


async def main(client: dynamoxy.Client | None = None) -> list[dict[str, object]]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        table = db.table("inventory")
        await table.create(partition_key=("warehouse", "S"), sort_key=("sku", "S"), ttl_attribute="expires_at")

        await table.batch_write(
            puts=[
                {"warehouse": "lund", "sku": f"sku-{index}", "stock": index * 10, "tags": {"new"}} for index in range(5)
            ]
        )
        await table.update({"warehouse": "lund", "sku": "sku-1"}, F("stock").add(-3), condition=F("stock") >= 3)
        low = await table.query(F("warehouse") == "lund").filter(F("stock") < 20).all()
        print("low stock:", [(item["sku"], item["stock"]) for item in low])

        rows = await db.execute(f'SELECT sku, stock FROM "{table.physical_name}" WHERE warehouse = ?', "lund")
        print("PartiQL rows:", len(rows))

        # Items in DynamoDB's typed JSON (boto3's low-level client, Streams, exports) and back.
        wire = dynamoxy.to_wire(low[0])
        print("typed JSON:", wire)
        assert dynamoxy.from_wire(wire) == low[0]
        return low


if __name__ == "__main__":
    asyncio.run(main())
