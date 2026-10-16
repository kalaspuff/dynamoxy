"""Interop with boto3 against the emulator: both see the same items in the same shape."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import Binary, TypeDeserializer, TypeSerializer

import dynamoxy
from tests.conftest import emulator_client, unique_name
from tests.models import Customer, LineItem, Order, OrderStatus, User

pytestmark = pytest.mark.e2e


@pytest.fixture
def boto(emulator_endpoint: str) -> Any:
    return boto3.client(
        "dynamodb",
        endpoint_url=emulator_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
async def client(emulator_endpoint: str) -> AsyncIterator[dynamoxy.Client]:
    prefix = f"{unique_name('interop')}-"
    async with emulator_client(emulator_endpoint, table_prefix=prefix) as client:
        await client.create_tables(User, Customer, Order)
        yield client
        for table in await client.list_tables():
            if table.startswith(prefix):
                await client.table(table).delete_table(wait=False, missing_ok=True)


def plain(item: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {name: unwrap(deserializer.deserialize(value)) for name, value in item.items()}


def unwrap(value: Any) -> Any:
    """boto3 values as dynamoxy returns them (bytes, int for whole numbers)."""
    if isinstance(value, Binary):
        return bytes(value)  # pyrefly: ignore[bad-argument-type] - boto3 Binary has __bytes__
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, list):
        return [unwrap(element) for element in value]
    if isinstance(value, dict):
        return {key: unwrap(element) for key, element in value.items()}
    if isinstance(value, set):
        return {unwrap(element) for element in value}
    return value


async def test_boto3_reads_what_dynamoxy_writes(client: dynamoxy.Client, boto: Any) -> None:
    order = Order(
        customer_id="c1",
        order_id="o1",
        placed_at=datetime(2026, 3, 1, 12, 30, tzinfo=UTC),
        total=Decimal("99.95"),
        status=OrderStatus.PAID,
        lines=[LineItem(sku="a", quantity=2, price=Decimal("49.975"))],
    )
    await client.repository(Order).create(order)
    response = boto.get_item(
        TableName=client.table_name("app"), Key={"pk": {"S": "CUSTOMER#c1"}, "sk": {"S": "ORDER#o1"}}
    )
    stored = response["Item"]
    assert stored["total"] == {"N": "99.95"}
    assert stored["gsi1pk"] == {"S": "STATUS#paid"}
    assert stored["_type"] == {"S": "Order"}
    assert set(stored.pop("_wid")) == {"S"}, "the id of the write (not a field)"
    assert stored == dynamoxy.to_wire(dynamoxy.schema_of(Order).encode(order))


async def test_dynamoxy_reads_what_boto3_writes(client: dynamoxy.Client, boto: Any) -> None:
    serializer = TypeSerializer()
    item = {
        "user_id": "u1",
        "email": "ada@example.com",
        "name": "Ada",
        "age": 36,
        "visits": Decimal(3),
        "tags": {"admin", "ops"},
        "status": "suspended",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "version": 4,
        "extra": {"nested": [1, "two", b"\x03", None, True]},
    }
    boto.put_item(
        TableName=client.table_name("users"), Item={key: serializer.serialize(value) for key, value in item.items()}
    )
    user = await client.repository(User).get_or_raise("u1")
    assert (user.age, user.visits, user.tags, user.version) == (36, 3, {"admin", "ops"}, 4)
    assert user.status.value == "suspended"
    assert user.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    raw = await client.table("users").get({"user_id": "u1"})
    assert raw == plain({key: serializer.serialize(value) for key, value in item.items()})


async def test_updates_from_both_sides(client: dynamoxy.Client, boto: Any) -> None:
    users = client.repository(User)
    user = await users.create(User(user_id="u2", email="b@example.com", name="Bob"))
    boto.update_item(
        TableName=client.table_name("users"),
        Key={"user_id": {"S": "u2"}},
        UpdateExpression="ADD visits :one, #v :one SET #n = :name",
        ExpressionAttributeNames={"#v": "version", "#n": "name"},
        ExpressionAttributeValues={":one": {"N": "1"}, ":name": {"S": "Robert"}},
    )
    with pytest.raises(dynamoxy.VersionConflictError):
        await users.update(user, User.visits.add(1))
    await users.refresh(user)
    assert (user.name, user.visits, user.version) == ("Robert", 1, 2)
    await users.update(user, User.tags.add({"x"}))
    stored = boto.get_item(TableName=client.table_name("users"), Key={"user_id": {"S": "u2"}})["Item"]
    assert stored["tags"] == {"SS": ["x"]}
    assert stored["version"] == {"N": "3"}


async def test_transaction_cancellations_look_the_same(client: dynamoxy.Client, boto: Any) -> None:
    await client.repository(Customer).create(Customer(customer_id="c9", name="Ada"))
    table = client.table_name("app")
    put = {
        "Put": {
            "TableName": table,
            "Item": {"pk": {"S": "CUSTOMER#c9"}, "sk": {"S": "PROFILE"}},
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }
    with pytest.raises(boto.exceptions.TransactionCanceledException) as boto_error:
        boto.transact_write_items(TransactItems=[put])
    with pytest.raises(dynamoxy.TransactionCanceledError) as ours:
        async with client.transaction() as tx:
            tx.create(Customer(customer_id="c9", name="Other"))
    boto_codes = [reason["Code"] for reason in boto_error.value.response["CancellationReasons"]]
    assert boto_codes == [reason.code for reason in ours.value.cancellation_reasons] == ["ConditionalCheckFailed"]
