"""Table management and dict-level item operations on every backend."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

import pytest

import dynamoxy
from dynamoxy import Attr, F, GlobalIndex, Model, field, partition_key, sort_key, ttl
from tests.helpers import record_consistent_reads
from tests.models import Customer, Event, Order, User


class SessionV1(Model, table="sessions"):
    session_id: Attr[str] = partition_key()
    user: Attr[str] = field(default="")
    team: Attr[str] = field(default="")
    old_expiry: Attr[datetime | None] = ttl(default=None)

    by_user = GlobalIndex(partition_key=user)


class SessionV2(Model, table="sessions"):
    """The same table, with another key for an index of the same name."""

    session_id: Attr[str] = partition_key()
    user: Attr[str] = field(default="")
    team: Attr[str] = field(default="")

    by_user = GlobalIndex(partition_key=team)


class SessionV3(Model, table="sessions"):
    """The same table, with another TTL attribute."""

    session_id: Attr[str] = partition_key()
    user: Attr[str] = field(default="")
    expires_at: Attr[datetime | None] = ttl(default=None)

    by_user = GlobalIndex(partition_key=user)


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


async def test_ensure_tables_refuses_indexes_and_ttl_that_differ(client: dynamoxy.Client) -> None:
    await client.create_tables(SessionV1)
    assert await client.ensure_tables(SessionV1) == []
    with pytest.raises(TypeError, match="index 'by_user' with the key"):
        await client.ensure_tables(SessionV2)
    with pytest.raises(TypeError, match="Time to Live on 'old_expiry', the models expect 'expires_at'"):
        await client.ensure_tables(SessionV3)


class Reading(Model, table="readings"):
    sensor: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    station: Attr[str] = field(default="")

    by_station = GlobalIndex(partition_key=station, sort_key=seq)


async def test_ensure_tables_refuses_keys_of_other_types(client: dynamoxy.Client) -> None:
    await client.table("readings").create(partition_key=("sensor", "N"), sort_key=("seq", "S"))
    with pytest.raises(
        TypeError, match=r"has the key sensor \(N\), seq \(S\), the models expect sensor \(S\), seq \(N\)"
    ):
        await client.ensure_tables(Reading)
    await client.table("readings").delete_table()
    await client.table("readings").create(partition_key=("sensor", "S"), sort_key=("seq", "N"))
    index = dynamoxy.IndexDefinition(
        name="by_station", kind="global", partition_key=("station", "N"), sort_key=("seq", "N")
    )
    await client.table("readings").add_index(index)
    with pytest.raises(TypeError, match=r"index 'by_station' with the key station \(N\), seq \(N\), the models expect"):
        await client.ensure_tables(Reading)


@pytest.mark.parametrize("call", ["create_tables", "ensure_tables"])
async def test_tables_created_by_another_writer_meanwhile_are_existing_tables(
    client: dynamoxy.Client, call: str
) -> None:
    # Another replica creates the table between the existence check and CreateTable.
    async def race(request: dynamoxy.Request, call_next: Any) -> Any:
        try:
            return await call_next(request)
        except dynamoxy.ResourceNotFoundError:
            if request.operation == "DescribeTable":
                remove()
                await client.create_tables(Event)
            raise

    remove = client.use(race)
    if call == "create_tables":
        assert await client.create_tables(Event, exist_ok=True) == []
        with pytest.raises(dynamoxy.ResourceInUseError):
            await client.create_tables(Event)
    else:
        assert await client.ensure_tables(Event) == []
    description = await client.table("events").describe()
    assert description.status == "ACTIVE"


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


async def test_dict_level_consistent_reads(client: dynamoxy.Client) -> None:
    await client.create_tables(Event)
    table = client.table("events")
    await table.put({"stream": "s", "seq": 1, "kind": "k"})
    sent = record_consistent_reads(client)
    assert len(await table.query(F("stream") == "s", consistent=True).all()) == 1
    assert len(await table.query(F("stream") == "s", index="by_kind_local", consistent=True).all()) == 1
    assert len(await table.scan(consistent=True).all()) == 1
    assert len(await table.query(F("stream") == "s").all()) == 1
    assert sent == [("Query", True), ("Query", True), ("Scan", True), ("Query", False)]
    # Tables do not know their indexes: DynamoDB refuses consistent reads of a global one.
    with pytest.raises(dynamoxy.ValidationError, match="Consistent reads are not supported on global secondary"):
        await table.query(F("kind") == "k", index="by_kind", consistent=True).all()
    with pytest.raises(dynamoxy.ValidationError, match="Consistent reads are not supported on global secondary"):
        await table.scan(index="by_kind", consistent=True).all()


async def test_writes_return_item_collection_metrics(client: dynamoxy.Client, backend: str) -> None:
    # Tables with a local index have item collections; Event has one (by_kind_local).
    await client.create_tables(Event)
    name = client.table_name("events")

    def collections(metrics: list[dict[str, Any]]) -> list[Any]:
        """The keys of item collection metrics (their size estimates differ between backends)."""
        assert all(len(entry["size_estimate_range_gb"]) == 2 for entry in metrics)
        return [entry["item_collection_key"] for entry in metrics]

    put = await client.call(
        "put_item",
        "PutItem",
        table=name,
        table_name=name,
        item={"stream": "s", "seq": 1, "kind": "k"},
        return_item_collection_metrics=True,
    )
    assert collections([put["item_collection_metrics"]]) == [{"stream": "s"}]
    writes = [("put", {"stream": "t", "seq": 1, "kind": "k"}), ("delete", {"stream": "s", "seq": 1})]
    batch = await client.call(
        "batch_write_item", "BatchWriteItem", request_items={name: writes}, return_item_collection_metrics=True
    )
    transaction = await client.call(
        "transact_write_items",
        "TransactWriteItems",
        transact_items=[{"kind": "put", "table_name": name, "item": {"stream": "u", "seq": 1, "kind": "k"}}],
        return_item_collection_metrics=True,
    )
    unasked = await client.call("batch_write_item", "BatchWriteItem", request_items={name: writes[:1]})
    assert unasked["item_collection_metrics"] == {}
    if backend == "memory":  # MiniStack returns metrics for single-item writes only
        assert {table: collections(metrics) for table, metrics in batch["item_collection_metrics"].items()} == {
            name: [{"stream": "t"}, {"stream": "s"}]
        }
        assert {table: collections(metrics) for table, metrics in transaction["item_collection_metrics"].items()} == {
            name: [{"stream": "u"}]
        }


class Visit(Model, table="visits"):
    visit_id: Attr[str] = partition_key()
    user: Attr[str] = field(default="")
    page: Attr[str] = field(default="")
    expires_at: Attr[datetime | None] = ttl(default=None)

    by_user = GlobalIndex(partition_key=user)
    by_page = GlobalIndex(partition_key=page)


def record_operations(client: dynamoxy.Client, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operations of the client's calls, and ``wait`` where a table waiter ran."""
    operations: list[str] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        operations.append(request.operation)
        return await call_next(request)

    client.use(record)
    wait_until_active = dynamoxy.table.Table.wait_until_active

    async def recorded_wait(table: dynamoxy.table.Table, **options: Any) -> dynamoxy.TableDescription:
        operations.append("wait")
        return await wait_until_active(table, **options)

    monkeypatch.setattr(dynamoxy.table.Table, "wait_until_active", recorded_wait)
    return operations


async def test_steps_without_waiting_wait_for_the_step_before(
    client: dynamoxy.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DynamoDB changes a table one step at a time, and Time to Live needs an active table: with
    # wait=False, the second index was added while the first was being created, and Time to Live
    # enabled on a table that was still being created.
    operations = record_operations(client, monkeypatch)
    await client.ensure_tables(Visit, wait=False)
    assert operations == ["DescribeTable", "CreateTable", "wait", "DescribeTimeToLive", "UpdateTimeToLive"]
    await client.table("visits").delete_table()
    await client.table("visits").create(partition_key=("visit_id", "S"))
    operations.clear()
    await client.ensure_tables(Visit, wait=False)
    changes = [operation for operation in operations if operation in ("wait", "UpdateTable", "UpdateTimeToLive")]
    assert changes == ["UpdateTimeToLive", "UpdateTable", "wait", "UpdateTable"], "the last step is not waited for"
    assert await client.ensure_tables(Visit, wait=False) == []


async def test_switching_to_provisioned_billing_gives_global_indexes_capacity(client: dynamoxy.Client) -> None:
    await client.create_tables(Visit)
    table = client.table("visits")
    sent: list[dict[str, Any]] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        if request.operation == "UpdateTable":
            sent.append(dict(request.params))
        return await call_next(request)

    client.use(record)
    description = await table.update_settings(billing=dynamoxy.Billing.provisioned(10, 5))
    assert (description.billing_mode, description.read_capacity, description.write_capacity) == ("PROVISIONED", 10, 5)
    updates = sorted(sent[0]["global_secondary_index_updates"], key=lambda update: update["update"]["index_name"])
    assert updates == [
        {"update": {"index_name": name, "provisioned_throughput": (10, 5)}} for name in ("by_page", "by_user")
    ]
    await table.update_settings(billing=dynamoxy.Billing.provisioned(20, 5))  # already provisioned: the table only
    assert "global_secondary_index_updates" not in sent[1]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("table", ["wait", "UpdateTimeToLive", "UpdateTable", "wait", "UpdateTable"]),
        ("index", ["UpdateTimeToLive", "wait", "UpdateTable", "wait", "UpdateTable"]),
    ],
)
async def test_steps_wait_for_a_change_an_earlier_call_left_in_progress(
    client: dynamoxy.Client, monkeypatch: pytest.MonkeyPatch, change: str, expected: list[str]
) -> None:
    # A rerun after a waiter gave up: the table is still being updated, or an index it added is
    # still backfilling (the table is active then, so Time to Live does not wait for it).
    await client.table("visits").create(partition_key=("visit_id", "S"))
    operations = record_operations(client, monkeypatch)
    describe = dynamoxy.table.Table.describe
    calls = 0

    async def still_updating(table: dynamoxy.table.Table) -> dynamoxy.TableDescription:
        nonlocal calls
        calls += 1
        description = await describe(table)
        if calls != 2:  # the description sync_with starts from (exists() reads the first)
            return description
        if change == "table":
            return dataclasses.replace(description, status="UPDATING")
        backfilling = dynamoxy.IndexDescription("old", "global", "o", None, "ALL", status="CREATING", backfilling=True)
        return dataclasses.replace(description, indexes={**description.indexes, "old": backfilling})

    monkeypatch.setattr(dynamoxy.table.Table, "describe", still_updating)
    await client.ensure_tables(Visit, wait=False)
    changes = [operation for operation in operations if operation in ("wait", "UpdateTable", "UpdateTimeToLive")]
    assert changes == expected


async def test_indexes_added_to_a_provisioned_table_get_its_capacity(client: dynamoxy.Client) -> None:
    table = client.table("capacity")
    await table.create(partition_key=("pk", "S"), billing=dynamoxy.Billing.provisioned(3, 2))
    sent: list[dict[str, Any]] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        if request.operation == "UpdateTable":
            sent.append(dict(request.params))
        return await call_next(request)

    client.use(record)
    description = await table.add_index(dynamoxy.IndexDefinition("by_owner", "global", ("owner", "S")))
    assert description.indexes["by_owner"].status == "ACTIVE"
    [created] = sent[0]["global_secondary_index_updates"]
    assert created["create"]["provisioned_throughput"] == (3, 2), "DynamoDB refuses an index without capacity there"


async def test_tables_being_created_refuse_changes(client: dynamoxy.Client, backend: str) -> None:
    # DynamoDB refuses to change a table it is still creating (measured, docs/parity); the
    # in-memory backend keeps a table CREATING until a DescribeTable sees it. MiniStack and
    # LocalStack make tables active at once.
    table = client.table("creating")
    await table.create(partition_key=("pk", "S"), wait=False)
    if backend == "memory":
        with pytest.raises(dynamoxy.ResourceNotFoundError, match=r"Table: .*creating not found"):
            await table.enable_time_to_live("expires")
        with pytest.raises(dynamoxy.ResourceInUseError, match="Table is being created"):
            # With billing=, add_index() does not describe the table (which would wait for it here).
            await table.add_index(
                dynamoxy.IndexDefinition("by_owner", "global", ("owner", "S")), billing=dynamoxy.Billing()
            )
    await table.wait_until_active()
    await table.enable_time_to_live("expires")
    await table.add_index(dynamoxy.IndexDefinition("by_owner", "global", ("owner", "S")), wait=False)
    if backend == "memory":  # MiniStack creates the index at once
        with pytest.raises(dynamoxy.LimitExceededError, match="Only 1 online index"):
            await table.add_index(
                dynamoxy.IndexDefinition("by_name", "global", ("name", "S")), billing=dynamoxy.Billing()
            )
    await table.wait_until_active()
    await table.add_index(dynamoxy.IndexDefinition("by_name", "global", ("name", "S")))
