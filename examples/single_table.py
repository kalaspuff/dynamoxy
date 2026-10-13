"""Single-table design: customers and their orders in one table, with a status index.

Run: uv run python examples/single_table.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

import dynamoxy
from dynamoxy import Attr, Document, GlobalIndex, Model, field, partition_key, sort_key


class OrderStatus(StrEnum):
    NEW = "new"
    PAID = "paid"
    SHIPPED = "shipped"


class LineItem(Document):
    sku: Attr[str]
    quantity: Attr[int] = field(ge=1)
    price: Attr[Decimal] = field(ge=0)


class Customer(Model, table="shop", entity="Customer"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="PROFILE")
    customer_id: Attr[str]
    name: Attr[str]


class Order(Model, table="shop", entity="Order"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="ORDER#{order_id}")
    customer_id: Attr[str]
    order_id: Attr[str]
    placed_at: Attr[datetime]
    status: Attr[OrderStatus] = field(default=OrderStatus.NEW)
    lines: Attr[list[LineItem]] = field(default_factory=list)
    # Derived keys of a generic index, recomputed on every write (also by update()).
    gsi1pk: Attr[str] = field(template="STATUS#{status}")
    gsi1sk: Attr[str] = field(template="{placed_at}")

    by_status = GlobalIndex("gsi1", partition_key=gsi1pk, sort_key=gsi1sk)

    @property
    def total(self) -> Decimal:
        return sum((line.price * line.quantity for line in self.lines), Decimal(0))


async def main(client: dynamoxy.Client | None = None) -> list[str]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Customer, Order)  # one table, created once
        customers, orders = db.repository(Customer), db.repository(Order)

        await customers.create(Customer(customer_id="c1", name="Ada"))
        for day, order_id in enumerate(["o1", "o2", "o3"], start=1):
            await orders.create(
                Order(
                    customer_id="c1",
                    order_id=order_id,
                    placed_at=datetime(2026, 5, day, 12, tzinfo=UTC),
                    lines=[LineItem(sku=f"sku-{order_id}", quantity=day, price=Decimal("9.90"))],
                )
            )

        # Keys come from the templates; the query only returns orders (sk begins with "ORDER#").
        placed = await orders.query(customer_id="c1").all()
        print("orders of c1:", [(order.order_id, str(order.total)) for order in placed])

        # Updating the status recomputes the index key, so the order moves to another partition.
        await orders.update(Order.key(customer_id="c1", order_id="o2"), Order.status.set(OrderStatus.PAID))
        paid = await orders.query(index=Order.by_status, status=OrderStatus.PAID).all()
        print("paid orders:", [order.order_id for order in paid])

        # A collection decodes every entity type of a partition.
        everything = await db.collection(Customer, Order).query(customer_id="c1").all()
        kinds = [type(item).__name__ for item in everything]
        print("partition CUSTOMER#c1 holds:", kinds)
        return kinds


if __name__ == "__main__":
    asyncio.run(main())
