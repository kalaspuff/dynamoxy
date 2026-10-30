"""Pydantic models as tables: pydantic validates, dynamoxy maps (pip install 'dynamoxy[pydantic]').

Run: uv run python examples/pydantic_models.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

import dynamoxy


class Address(BaseModel):  # plain pydantic models nest as maps
    street: str
    city: str
    country: str = "SE"


class Customer(dynamoxy.PydanticModel, table="crm-customers"):
    customer_id: Annotated[str, dynamoxy.PartitionKey()]
    email: Annotated[str, dynamoxy.Unique()]
    name: str = Field(min_length=1)
    credit: Decimal = Decimal(0)
    address: Address | None = None
    created_at: Annotated[datetime | None, dynamoxy.CreatedAt()] = None
    version: Annotated[int | None, dynamoxy.Version()] = None

    by_email = dynamoxy.GlobalIndex(partition_key="email")


async def main(client: dynamoxy.Client | None = None) -> Customer:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Customer)
        customers = db.model(Customer)
        ada = await customers.create(
            Customer(
                customer_id="c1", email="ada@example.com", name="Ada", address=Address(street="Main 1", city="Lund")
            )
        )
        print(f"created {ada.customer_id} version={ada.version} created_at={ada.created_at:%Y-%m-%d}")

        # Class attributes are expression paths; dynamoxy.attr() types them for type checkers.
        await customers.update(ada, dynamoxy.attr(Customer.credit).add(Decimal("12.50")))
        rich = await customers.scan().filter(dynamoxy.attr(Customer.credit) > 10).all()
        print("customers with credit:", [customer.name for customer in rich])

        found = await customers.query(index=Customer.by_email, email="ada@example.com").one()
        assert found.address is not None
        print(f"{found.name} lives in {found.address.city}; credit {found.credit}")
        return found


if __name__ == "__main__":
    asyncio.run(main())
