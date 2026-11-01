"""dynamoxy.streams on items as dynamoxy stores them, on every backend.

The backends keep no streams, so the records are built from the difference between scans of
the tables before and after each step, in the shape DynamoDB Streams sends them
(NEW_AND_OLD_IMAGES): every attribute dynamoxy writes, guard items included, comes through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import dynamoxy
from dynamoxy import streams
from dynamoxy.schema import schema_of
from tests.models import Customer, Order, User

type Snapshot = dict[tuple[str, str], dict[str, Any]]


async def snapshot(client: dynamoxy.Client, *models: type) -> Snapshot:
    """Every item of the models' tables by table and key."""
    items: Snapshot = {}
    for model in models:
        schema = schema_of(model)
        table = client.model(model).table
        for item in await table.scan(consistent=True).all():
            items[table.physical_name, repr(sorted(schema.key_values(item).items()))] = item
    return items


def records(
    before: Snapshot, after: Snapshot, client: dynamoxy.Client, models: tuple[type, ...]
) -> list[dict[str, Any]]:
    """The stream records of the writes between two snapshots."""
    key_names = {client.model(model).table.physical_name: schema_of(model)._key_attributes for model in models}
    result: list[dict[str, Any]] = []
    for table, key in sorted(before.keys() | after.keys()):
        old, new = before.get((table, key)), after.get((table, key))
        if old == new:
            continue
        item = new if new is not None else old
        assert item is not None
        data: dict[str, Any] = {
            "ApproximateCreationDateTime": datetime.now(UTC).timestamp(),
            "Keys": dynamoxy.to_wire({name: item[name] for name in key_names[table]}, base64_binary=True),
            "SequenceNumber": str(len(result) + 1),
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        }
        if new is not None:
            data["NewImage"] = dynamoxy.to_wire(new, base64_binary=True)
        if old is not None:
            data["OldImage"] = dynamoxy.to_wire(old, base64_binary=True)
        result.append(
            {
                "eventID": f"e{len(result) + 1}",
                "eventName": "INSERT" if old is None else "REMOVE" if new is None else "MODIFY",
                "eventSource": "aws:dynamodb",
                "dynamodb": data,
                "eventSourceARN": f"arn:aws:dynamodb:us-east-1:123456789012:table/{table}/stream/2026-11-01",
            }
        )
    return result


@pytest.fixture
async def tables(client: dynamoxy.Client) -> tuple[type, ...]:
    await client.ensure_tables(User, Customer, Order)
    return (User, Customer, Order)


async def test_changes_of_stored_items(client: dynamoxy.Client, tables: tuple[type, ...]) -> None:
    start = await snapshot(client, *tables)
    user = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
    await Customer.create(client, customer_id="c1", name="Ada")
    order = await Order.create(client, customer_id="c1", order_id="o1", placed_at=datetime.now(UTC), total=Decimal(5))
    created = await snapshot(client, *tables)
    user.email = "ada@example.org"
    await user.save()
    await order.delete()
    changed = await snapshot(client, *tables)

    event = {"Records": records(start, created, client, tables) + records(created, changed, client, tables)}
    assert len(event["Records"]) == 4 + 4, "an item and a guard for the user, and each change of the guards"
    changes = list(streams.changes(event, *tables, client=client))
    assert [(change.event_name, change.model) for change in changes] == [
        ("INSERT", Order),
        ("INSERT", Customer),
        ("INSERT", User),
        ("REMOVE", Order),
        ("MODIFY", User),
    ], "the guard items are skipped"
    modified = changes[4]
    assert modified.new is not None
    assert modified.old is not None
    assert (modified.old.email, modified.new.email) == ("ada@example.com", "ada@example.org")
    assert modified.new == await User.get(client, "u1")

    # The instances are bound to the client's repositories: the new image writes back, and the old
    # one, a version behind, is refused.
    modified.new.visits += 1
    await modified.new.save()
    assert (await client.model(User).get_or_raise("u1", consistent=True)).visits == 1
    modified.old.visits += 1
    with pytest.raises(dynamoxy.VersionConflictError):
        await modified.old.save()


async def test_unbound_changes_of_one_entity(client: dynamoxy.Client, tables: tuple[type, ...]) -> None:
    start = await snapshot(client, *tables)
    await Customer.create(client, customer_id="c2", name="Bob")
    await Order.create(client, customer_id="c2", order_id="o2", placed_at=datetime.now(UTC), total=Decimal(7))
    event = records(start, await snapshot(client, *tables), client, tables)
    [change] = streams.changes(event, Order)
    assert change.new is not None
    assert (change.event_name, change.new.order_id, change.new.total) == ("INSERT", "o2", Decimal(7))
    assert change.table == client.table_name("app")
