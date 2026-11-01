"""A DynamoDB Streams handler: keep a customer's order totals up to date from the table's stream.

In AWS Lambda, subscribe the function to the table's stream (``NEW_AND_OLD_IMAGES``, with
``ReportBatchItemFailures``), create the client once per execution environment and run the
handler for each batch::

    client = dynamoxy.Client()


    def handler(event, context):
        return asyncio.run(handle(event, client))

The in-memory DynamoDB keeps no stream, so ``main()`` builds the records the stream would send.

Run: uv run python examples/stream_handler.py
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key, streams


class Customer(Model, table="storefront", entity="Customer"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="PROFILE")
    customer_id: Attr[str]
    email: Attr[str] = field(unique=True)  # also stores a guard item in the table, which the stream carries
    orders: Attr[int] = field(default=0)
    spent: Attr[Decimal] = field(default=Decimal(0))


class Order(Model, table="storefront", entity="Order"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="ORDER#{order_id}")
    customer_id: Attr[str]
    order_id: Attr[str]
    total: Attr[Decimal] = field(ge=0)


async def handle(event: dict[str, Any], client: dynamoxy.Client) -> dict[str, list[dict[str, str]]]:
    """Counts each customer's orders as they are placed and removed.

    Records of customers and of guard items are skipped. When a record fails, Lambda retries the
    batch from it: the changes before it are not processed again. Lambda may deliver a record
    more than once; a handler whose effects must happen once records what it did (see "Idempotent
    consumers" in the usage guide).
    """
    customers = client.model(Customer)
    current: str | None = None
    try:
        for change in streams.changes(event, Order, client=client):
            current = change.sequence_number
            if change.event_name == "INSERT" and change.new is not None:
                order, sign = change.new, 1
            elif change.event_name == "REMOVE" and change.old is not None:
                order, sign = change.old, -1
            else:
                continue
            await customers.update(
                {"customer_id": order.customer_id},
                Customer.orders.add(sign),
                Customer.spent.add(sign * order.total),
            )
    except dynamoxy.StreamRecordError as error:  # an image that does not fit its model
        return {"batchItemFailures": [{"itemIdentifier": error.sequence_number or ""}]}
    except dynamoxy.DynamoxyError:  # the update failed: retry from the record being processed
        return {"batchItemFailures": [{"itemIdentifier": current or ""}]}
    return {"batchItemFailures": []}


def stream_record(client: dynamoxy.Client, event_name: str, sequence: int, item: dict[str, Any]) -> dict[str, Any]:
    """A record as DynamoDB Streams sends it to Lambda: the item in DynamoDB's typed JSON."""
    image = "OldImage" if event_name == "REMOVE" else "NewImage"
    table = client.table_name("storefront")
    return {
        "eventID": f"event-{sequence}",
        "eventName": event_name,
        "eventSource": "aws:dynamodb",
        "eventSourceARN": f"arn:aws:dynamodb:eu-north-1:123456789012:table/{table}/stream/2026-11-01T00:00:00.000",
        "dynamodb": {
            "Keys": dynamoxy.to_wire({"pk": item["pk"], "sk": item["sk"]}),
            image: dynamoxy.to_wire(item, base64_binary=True),
            "SequenceNumber": str(sequence),
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
    }


async def main(client: dynamoxy.Client | None = None) -> tuple[int, Decimal]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Customer, Order)
        await Customer.create(db, customer_id="c1", email="ada@example.com")
        first = await Order.create(db, customer_id="c1", order_id="o1", total=Decimal(30))
        await Order.create(db, customer_id="c1", order_id="o2", total=Decimal("12.5"))
        # The customer, the guard item of their email and two orders, each inserted.
        items = await db.model(Customer).table.scan(consistent=True).all()
        records = [stream_record(db, "INSERT", sequence, item) for sequence, item in enumerate(items, 1)]
        await first.delete()
        records.append(stream_record(db, "REMOVE", len(records) + 1, first.to_item()))

        result = await handle({"Records": records}, db)
        print("batch item failures:", result["batchItemFailures"])
        customer = await db.model(Customer).get_or_raise(customer_id="c1", consistent=True)
        print(f"{customer.customer_id}: {customer.orders} order(s), {customer.spent} spent")
        return customer.orders, customer.spent


if __name__ == "__main__":
    asyncio.run(main())
