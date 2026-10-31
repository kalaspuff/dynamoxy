"""Verifying and repairing unique guard items (``verify_unique()``, ``repair_unique()``) on every backend."""

from __future__ import annotations

import asyncio

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key, testing
from tests.integration.test_unique import Current, Legacy, guards


class Member(Model, table="members", entity="Member"):
    """A unique value in a single table, next to items of another entity."""

    org_id: Attr[str] = partition_key()
    member_id: Attr[str] = sort_key()
    email: Attr[str] = field(unique=True)


class Note(Model, table="members", entity="Note"):
    org_id: Attr[str] = partition_key()
    member_id: Attr[str] = sort_key()
    email: Attr[str]


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[Current]:
    await client.create_tables(Legacy)
    return client.model(Current)


async def store_legacy(client: dynamoxy.Client, **emails: str) -> None:
    for user_id, email in emails.items():
        await client.model(Legacy).create(Legacy(user_id=user_id, email=email))


async def test_missing_guards_of_items_stored_before_the_field_became_unique(
    client: dynamoxy.Client, users: dynamoxy.Repository[Current]
) -> None:
    await store_legacy(client, u1="ada@example.com", u2="bob@example.com")
    await users.create(Current(user_id="u3", email="cy@example.com"))
    report = await users.verify_unique()
    assert (report.model, report.items, report.guards, report.ok) == (Current, 3, 1, False)
    assert sorted((m.field, m.value, m.key["user_id"]) for m in report.missing) == [
        ("email", "ada@example.com", "u1"),
        ("email", "bob@example.com", "u2"),
    ]
    assert (report.orphaned, report.duplicates, report.invalid) == ((), (), ())
    planned = await users.repair_unique(report, dry_run=True)
    assert planned.dry_run
    assert len(planned.created) == 2
    assert len(await guards(users)) == 1, "a dry run writes nothing"
    repair = await users.repair_unique()
    assert (len(repair.created), repair.deleted, repair.reassigned, repair.skipped) == (2, (), (), ())
    assert (await users.verify_unique(segments=2)).ok
    again = await users.repair_unique()
    assert (again.created, again.deleted, again.reassigned, again.skipped) == ((), (), (), ()), "idempotent"
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await users.create(Current(user_id="u4", email="ada@example.com"))
    await users.update(Current.key("u1"), Current.email.set("ada@new.example.com"))
    await users.create(Current(user_id="u4", email="ada@example.com"))
    assert (await users.verify_unique()).ok


async def test_duplicates_are_reported_and_left_alone(
    client: dynamoxy.Client, users: dynamoxy.Repository[Current]
) -> None:
    await store_legacy(client, u1="ada@example.com", u2="ada@example.com", u3="bob@example.com")
    await users.create(Current(user_id="u4", email="cy@example.com"))
    await store_legacy(client, u5="cy@example.com")
    report = await users.verify_unique()
    duplicates = sorted(report.duplicates, key=lambda d: d.value)
    assert [(d.field, d.value, sorted(k["user_id"] for k in d.keys), d.owner) for d in duplicates] == [
        ("email", "ada@example.com", ["u1", "u2"], None),
        ("email", "cy@example.com", ["u4", "u5"], {"user_id": "u4"}),
    ]
    assert [m.key["user_id"] for m in report.missing] == ["u3"]
    with pytest.raises(ValueError, match=r"2 unique values are held by several items .* nothing was written"):
        await users.repair_unique(report)
    assert len(await guards(users)) == 1
    repair = await users.repair_unique(report, skip_duplicates=True)
    assert [m.key["user_id"] for m in repair.created] == ["u3"]
    assert repair.duplicates == report.duplicates
    assert await guards(users) == ["_unique#Current#email#bob@example.com", "_unique#Current#email#cy@example.com"]


async def test_orphaned_guards_are_deleted_or_given_to_the_item_holding_the_value(
    client: dynamoxy.Client, users: dynamoxy.Repository[Current]
) -> None:
    await users.create(Current(user_id="gone", email="gone@example.com"))
    await users.create(Current(user_id="moved", email="old@example.com"))
    await users.create(Current(user_id="taken", email="ada@example.com"))
    await store_legacy(client, holder="ada@example.com")
    table = users.table
    await table.delete({"user_id": "gone"})  # by another tool: its guard stays
    await table.delete({"user_id": "taken"})  # the guard of ada stays, and "holder" holds ada
    await table.update({"user_id": "moved"}, dynamoxy.F("email").set("new@example.com"))  # the guard stays
    await table.put({"user_id": "_unique#Current#nickname#ada", "_type": "_unique", "_attribute": "nickname"})
    report = await users.verify_unique()
    orphaned = {o.key["user_id"]: o for o in report.orphaned}
    assert {name: (o.field, o.reason, o.owner, o.holder) for name, o in orphaned.items()} == {
        "_unique#Current#email#gone@example.com": ("email", "owner missing", {"user_id": "gone"}, None),
        "_unique#Current#email#old@example.com": ("email", "owner changed", {"user_id": "moved"}, None),
        "_unique#Current#email#ada@example.com": (
            "email",
            "owner missing",
            {"user_id": "taken"},
            {"user_id": "holder"},
        ),
        "_unique#Current#nickname#ada": (None, "not unique", None, None),
    }
    assert [(m.value, m.key) for m in report.missing] == [("new@example.com", {"user_id": "moved"})]
    repair = await users.repair_unique(report)
    assert (len(repair.created), len(repair.deleted), len(repair.reassigned), repair.skipped) == (1, 3, 1, ())
    assert await guards(users) == ["_unique#Current#email#ada@example.com", "_unique#Current#email#new@example.com"]
    guard = await table.get({"user_id": "_unique#Current#email#ada@example.com"})
    assert guard is not None
    assert guard["_owner"] == {"user_id": "holder"}
    assert (await users.verify_unique()).ok
    assert await users.delete("holder") is not None, "the holder owns its guard now"


async def test_fixes_skip_data_that_changed_since_the_scan(
    client: dynamoxy.Client, users: dynamoxy.Repository[Current]
) -> None:
    await store_legacy(client, u1="ada@example.com", u2="bob@example.com")
    await users.create(Current(user_id="u3", email="cy@example.com"))
    await users.table.delete({"user_id": "u3"})
    report = await users.verify_unique()
    assert (len(report.missing), len(report.orphaned)) == (2, 1)
    # Meanwhile: u1 changes its value, another item takes bob, and u3 is stored again with cy.
    await users.table.update({"user_id": "u1"}, dynamoxy.F("email").set("ada@new.example.com"))
    await users.create(Current(user_id="u4", email="bob@example.com"))
    await users.table.put({"user_id": "u3", "email": "cy@example.com"})
    repair = await users.repair_unique(report)
    assert (repair.created, repair.deleted, repair.reassigned) == ((), (), ())
    assert len(repair.skipped) == 3
    assert await guards(users) == ["_unique#Current#email#bob@example.com", "_unique#Current#email#cy@example.com"]
    guard = await users.table.get({"user_id": "_unique#Current#email#bob@example.com"})
    assert guard is not None
    assert guard["_owner"] == {"user_id": "u4"}, "the value another writer took stays its own"


async def test_single_tables_verify_only_their_model(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    members = client.model(Member)
    await members.create(Member(org_id="o1", member_id="m1", email="ada@example.com"))
    await client.model(Note).create(Note(org_id="o1", member_id="n1", email="ada@example.com"))
    await members.table.put({"org_id": "o1", "member_id": "m2", "_type": "Member", "email": "bob@example.com"})
    report = await members.verify_unique()
    assert (report.items, report.guards, report.duplicates, report.orphaned) == (2, 1, (), ())
    assert [m.key for m in report.missing] == [{"org_id": "o1", "member_id": "m2"}]
    await members.repair_unique()
    assert (await members.verify_unique()).ok


async def test_models_without_unique_attributes_are_refused(client: dynamoxy.Client) -> None:
    with pytest.raises(TypeError, match="has no unique attributes"):
        await client.model(Legacy).verify_unique()
    with pytest.raises(TypeError, match="has no unique attributes"):
        await client.model(Legacy).repair_unique()
    with pytest.raises(ValueError, match="the report is of Legacy"):
        await client.model(Current).repair_unique(dynamoxy.UniqueReport(model=Legacy, items=0, guards=0))


async def test_service_errors_of_fixes_are_raised_as_they_are() -> None:
    async with testing.memory_client(retry=dynamoxy.RetryPolicy.none()) as client:
        await client.create_tables(Legacy)
        await store_legacy(client, u1="ada@example.com", u2="bob@example.com")
        users = client.model(Current)
        testing.inject_fault(client, operation="TransactWriteItems", error="throttling", times=1)
        with pytest.raises(dynamoxy.ThrottlingError):  # not an ExceptionGroup
            await users.repair_unique()
        await users.repair_unique()  # repairs what is left
        assert len(await guards(users)) == 2
        assert (await users.verify_unique()).ok


class Contact(Model, table="contacts"):
    """Two unique values per item: their fixes share the item."""

    contact_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    phone: Attr[str] = field(unique=True)


class LegacyContact(Model, table="contacts"):
    contact_id: Attr[str] = partition_key()
    email: Attr[str]
    phone: Attr[str]


async def test_fixes_that_share_an_item_never_run_at_once(client: dynamoxy.Client) -> None:
    # DynamoDB cancels one of two concurrent transactions on one item (TransactionConflict).
    await client.create_tables(LegacyContact)
    for index in range(5):
        await client.model(LegacyContact).create(
            LegacyContact(contact_id=f"c{index}", email=f"c{index}@example.com", phone=f"+46{index}")
        )
    in_flight: list[set[str]] = []
    overlaps: list[str] = []
    concurrent = 0

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        nonlocal concurrent
        if request.operation != "TransactWriteItems":
            return await call_next(request)
        operations = request.params["transact_items"]
        keys = {
            operation["key"]["contact_id"] if "key" in operation else operation["item"]["contact_id"]
            for operation in operations
        }
        overlaps.extend(key for busy in in_flight for key in keys & busy)
        in_flight.append(keys)
        concurrent = max(concurrent, len(in_flight))
        try:
            await asyncio.sleep(0)  # lets the other fixes start
            return await call_next(request)
        finally:
            in_flight.remove(keys)

    client.use(record)
    repair = await client.model(Contact).repair_unique()
    assert len(repair.created) == 10
    assert concurrent > 1, "fixes of different items still run at once"
    assert overlaps == []
    assert (await client.model(Contact).verify_unique()).ok
