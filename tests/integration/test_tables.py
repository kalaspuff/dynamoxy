"""Table management and dict-level item operations on every backend."""

from __future__ import annotations

import pytest

import dynamoxy
from dynamoxy import F
from tests.models import Customer, Event, Order, User


async def test_create_describe_list_and_delete(client: dynamoxy.Client) -> None:
    created = await client.create_tables(User, Event, Customer, Order)
    assert created == [client.table_name("users"), client.table_name("events"), client.table_name("app")]
    users = await client.table("users").describe()
    assert (users.status, users.partition_key, users.sort_key) == ("ACTIVE", "user_id", None)
    assert users.billing_mode == "PAY_PER_REQUEST"
    assert {name: index.kind for name, index in users.indexes.items()} == {"by_email": "global", "by_status": "global"}
    assert users.indexes["by_status"].projection == "KEYS_ONLY"
    events = await client.table("events").describe()
    assert events.sort_key == "seq"
    local = events.indexes["by_kind_local"]
    assert (local.kind, local.partition_key, local.sort_key) == ("local", "stream", "kind")
    assert (local.projection, local.projected_attributes) == ("INCLUDE", ("payload",))
    assert (await client.table("events").time_to_live()).attribute == "expires_at"
    assert set(created) <= set(await client.list_tables())
    with pytest.raises(dynamoxy.ResourceInUseError):
        await client.create_tables(User)
    assert await client.create_tables(User, exist_ok=True) == []
    await client.delete_tables(User, Event)
    assert not await client.table("users").exists()
    with pytest.raises(dynamoxy.ResourceNotFoundError):
        await client.table("events").describe()
    with pytest.raises(dynamoxy.ResourceNotFoundError):
        await client.table("events").delete_table()
    await client.delete_tables(User)  # missing tables are fine here


async def test_ensure_tables_adds_what_is_missing(client: dynamoxy.Client) -> None:
    await client.table("users").create(partition_key=("user_id", "S"))
    assert await client.ensure_tables(User, Event) == [client.table_name("users"), client.table_name("events")]
    description = await client.table("users").describe()
    assert sorted(description.indexes) == ["by_email", "by_status"]
    assert all(index.status == "ACTIVE" for index in description.indexes.values())
    assert await client.ensure_tables(User, Event) == [], "nothing left to change"
    await client.table("app").create(partition_key=("pk", "S"))
    with pytest.raises(TypeError, match="a table's key cannot change"):
        await client.ensure_tables(Customer)


async def test_indexes_can_be_added_and_removed(client: dynamoxy.Client) -> None:
    table = client.table("plain")
    await table.create(partition_key=("pk", "S"), sort_key=("sk", "N"))
    await table.put({"pk": "a", "sk": 1, "kind": "x"})
    await table.put({"pk": "b", "sk": 2, "kind": "x"})
    index = dynamoxy.IndexDefinition(name="by_kind", kind="global", partition_key=("kind", "S"), sort_key=("sk", "N"))
    description = await table.add_index(index)
    assert description.indexes["by_kind"].status == "ACTIVE"
    rows = await table.query(F("kind") == "x", index="by_kind").all()
    assert [row["pk"] for row in rows] == ["a", "b"], "new indexes are backfilled"
    description = await table.delete_index("by_kind")
    assert "by_kind" not in description.indexes
    with pytest.raises(ValueError, match="only global"):
        await table.add_index(dynamoxy.IndexDefinition(name="l", kind="local", partition_key=("pk", "S")))


async def test_time_to_live(client: dynamoxy.Client) -> None:
    await client.create_tables(Event)
    table = client.table("events")
    ttl = await table.time_to_live()
    assert ttl.enabled
    assert ttl.attribute == "expires_at"
    assert await table.enable_time_to_live("expires_at") == ttl, "enabling it again is a no-op"
    with pytest.raises(dynamoxy.ValidationError, match="disable it before"):
        await table.enable_time_to_live("other")
    assert not (await table.disable_time_to_live()).enabled
    assert not (await table.time_to_live()).enabled
    assert not (await table.disable_time_to_live()).enabled


async def test_tags(client: dynamoxy.Client) -> None:
    await client.create_tables(User, tags={"team": "core", "env": "test"})
    table = client.table("users")
    assert await table.tags() == {"team": "core", "env": "test"}
    await table.tag({"env": "prod", "cost": "42"})
    await table.untag("team")
    assert await table.tags() == {"env": "prod", "cost": "42"}


async def test_settings(client: dynamoxy.Client) -> None:
    await client.create_tables(Event, billing=dynamoxy.Billing.provisioned(5, 5))
    table = client.table("events")
    description = await table.describe()
    assert (description.billing_mode, description.read_capacity, description.write_capacity) == ("PROVISIONED", 5, 5)
    description = await table.update_settings(billing=dynamoxy.Billing.on_demand(), stream="NEW_AND_OLD_IMAGES")
    assert description.billing_mode == "PAY_PER_REQUEST"
    assert description.stream_view_type == "NEW_AND_OLD_IMAGES"
    assert description.stream_arn
    description = await table.update_settings(deletion_protection=True)
    assert description.deletion_protection
    with pytest.raises(dynamoxy.ValidationError, match=r"deletion protection|protected against deletion"):
        await table.delete_table()
    await table.update_settings(deletion_protection=False, stream=False)
    await table.delete_table()


async def test_backups_and_restores(client: dynamoxy.Client) -> None:
    await client.create_tables(Event)
    table = client.table("events")
    await table.put({"stream": "s", "seq": 1, "kind": "before"})
    backup = await table.create_backup("nightly")
    assert backup.name == "nightly"
    assert backup.arn
    assert [listed.arn for listed in await table.backups()] == [backup.arn]
    await table.put({"stream": "s", "seq": 2, "kind": "after"})
    restored = client.table("events-restored")
    await restored.restore_backup(backup.arn)
    assert [row["kind"] for row in await restored.scan().all()] == ["before"]
    with pytest.raises(dynamoxy.DynamoxyError):
        await restored.restore_backup(backup.arn)
    await table.delete_backup(backup.arn)
    assert await table.backups() == []


async def test_point_in_time_recovery(client: dynamoxy.Client, backend: str) -> None:
    await client.create_tables(Event)
    table = client.table("events")
    assert not (await table.continuous_backups()).point_in_time_recovery
    state = await table.enable_point_in_time_recovery(recovery_period_days=7)
    assert state.point_in_time_recovery
    if backend == "memory":  # MiniStack reports neither the period nor the restorable window
        assert state.recovery_period_days == 7
        assert state.earliest_restorable is not None
        assert state.latest_restorable is not None
    await table.put({"stream": "s", "seq": 1, "kind": "k"})
    await table.restore_to("events-copy")
    assert len(await client.table("events-copy").scan().all()) == 1
    assert not (await table.disable_point_in_time_recovery()).point_in_time_recovery


async def test_dict_level_items(client: dynamoxy.Client) -> None:
    table = client.table("plain")
    await table.create(partition_key=("pk", "S"), sort_key=("sk", "N"))
    assert await table.put({"pk": "a", "sk": 1, "n": 1}, return_old=True) is None
    old = await table.put({"pk": "a", "sk": 1, "n": 2}, return_old=True)
    assert old == {"pk": "a", "sk": 1, "n": 1}
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await table.put({"pk": "a", "sk": 1}, condition=F("pk").not_exists())
    assert caught.value.item == {"pk": "a", "sk": 1, "n": 2}
    assert await table.update({"pk": "a", "sk": 1}, F("n").add(3)) == {"pk": "a", "sk": 1, "n": 5}
    assert await table.update({"pk": "a", "sk": 1}, F("n").set(F("n") - 1), return_values="UPDATED_OLD") == {"n": 5}
    assert await table.get({"pk": "a", "sk": 1}, projection=["n"], consistent=True) == {"n": 4}
    await table.batch_write(puts=[{"pk": "b", "sk": index} for index in range(30)], deletes=[{"pk": "a", "sk": 1}])
    keys = [{"pk": "b", "sk": index} for index in range(30)] + [{"pk": "missing", "sk": 0}]
    assert len(await table.batch_get(keys)) == 30
    page = await table.query(F("pk") == "b", F("sk").between(10, 19)).limit(4).page()
    assert [row["sk"] for row in page.items] == [10, 11, 12, 13]
    assert page.cursor is not None
    assert await table.delete({"pk": "b", "sk": 0}, return_old=True) == {"pk": "b", "sk": 0}
    assert await table.delete({"pk": "b", "sk": 0}, return_old=True) is None
    assert await table.query(F("pk") == "b").count() == 29
    with pytest.raises(ValueError, match="needs a key condition"):
        table.query()
