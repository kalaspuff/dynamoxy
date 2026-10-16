"""Repository behaviour on every backend (in-memory and the emulator)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import dynamoxy
from dynamoxy import Attr, F, GlobalIndex, Model, Projection, field, partition_key, sort_key, updated_at, version
from tests.conftest import ClientFactory
from tests.helpers import at, record_consistent_reads, wait_for
from tests.models import Customer, Event, Order, OrderStatus, Player, Status, User


class Ticket(Model, table="tickets"):
    """Templates that use automatic fields, an immutable field and a timedelta counter."""

    ticket_id: Attr[str] = partition_key()
    state: Attr[str] = field(default="open")
    title: Attr[str] = field(default="")
    country: Attr[str] = field(default="SE", immutable=True)
    spent: Attr[timedelta] = field(default=timedelta(0))
    updated_at: Attr[datetime] = updated_at()
    version: Attr[int] = version()
    recent: Attr[str] = field(template="{updated_at}")
    state_recent: Attr[str] = field(template="{state}#{updated_at}")
    revision: Attr[str] = field(template="v{version}")


class Counter(Model, table="counters"):
    """Every field has a default: entries of a KEYS_ONLY index would decode without an error."""

    counter_id: Attr[str] = partition_key()
    group: Attr[str] = field(default="a")
    count: Attr[int] = field(default=0)
    note: Attr[str | None] = field(default=None)

    by_group = GlobalIndex(partition_key=group, projection=Projection.keys_only())


class Device(Model, table="devices"):
    device_id: Attr[uuid.UUID] = partition_key()
    label: Attr[str] = field(default="")


class Measurement(Model, table="measurements"):
    sensor: Attr[str] = partition_key()
    value: Attr[float] = sort_key()


def make_user(user_id: str = "u1", **changes: Any) -> User:
    values: dict[str, Any] = {"user_id": user_id, "email": f"{user_id}@example.com", "name": user_id.upper()}
    values.update(changes)
    return User(**values)


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[User]:
    await client.create_tables(User)
    return client.repository(User)


@pytest.fixture
async def events(client: dynamoxy.Client) -> dynamoxy.Repository[Event]:
    await client.create_tables(Event)
    return client.repository(Event)


async def test_create_get_and_automatic_fields(users: dynamoxy.Repository[User]) -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    user = await users.create(make_user())
    assert user.version == 1
    assert user.created_at is not None
    assert user.created_at >= before
    assert user.updated_at == user.created_at
    assert user.is_loaded
    fetched = await users.get("u1")
    assert fetched == user
    assert fetched is not None
    assert fetched.is_loaded
    assert await users.get("missing") is None
    assert await users.exists("u1")
    assert not await users.exists(user_id="missing")
    with pytest.raises(dynamoxy.ItemNotFoundError, match="does not exist"):
        await users.get_or_raise("missing")


async def test_create_refuses_existing_keys(users: dynamoxy.Repository[User]) -> None:
    await users.create(make_user())
    with pytest.raises(dynamoxy.ItemAlreadyExistsError) as caught:
        await users.create(make_user(email="other@example.com"))
    assert caught.value.key == {"user_id": "u1"}
    assert isinstance(caught.value, dynamoxy.ConditionalCheckFailedError)
    assert caught.value.retryable is False


async def test_put_replaces_and_save_checks_versions(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    stale = await users.get_or_raise("u1")
    user.name = "Ada"
    await users.save(user)
    assert user.version == 2
    stale.name = "Stale"
    with pytest.raises(dynamoxy.VersionConflictError) as caught:
        await users.save(stale)
    assert caught.value.expected_version == 1
    assert (await users.get_or_raise("u1")).name == "Ada"
    await users.refresh(stale)
    assert stale.name == "Ada"
    assert stale.version == 2
    stale.name = "Fresh"
    await users.save(stale)
    replacement = make_user(name="Replaced")
    await users.put(replacement)
    assert (await users.get_or_raise("u1")).name == "Replaced"


async def test_save_of_a_new_versioned_item_requires_absence(users: dynamoxy.Repository[User]) -> None:
    await users.save(make_user())
    with pytest.raises(dynamoxy.VersionConflictError):
        await users.save(make_user(email="second@example.com"))


async def test_copies_save_like_new_instances(users: dynamoxy.Repository[User]) -> None:
    original = make_user("u1")
    # A fixed moment: the copy's own timestamp is later even where the clock is coarse (Windows'
    # clock advances in steps of about 15 ms on Python 3.12).
    original.created_at = at(1)
    await users.create(original)
    await users.update(original, User.visits.add(1))
    clone = original.copy(user_id="u2", email="u2@example.com")
    assert (clone.version, clone.created_at, clone.updated_at, clone.is_loaded) == (None, None, None, False)
    assert clone.visits == 1, "the other values are copied"
    await users.save(clone)  # a new item: version 1 and timestamps of its own
    assert clone.version == 1
    assert clone.created_at is not None
    assert clone.created_at > at(1), "not the original's"
    assert (await users.get_or_raise("u1")).version == 2, "the original is unchanged"
    with pytest.raises(dynamoxy.VersionConflictError):
        await users.save(original.copy(name="Same key"))  # like a new instance: the key must be free
    kept = original.copy(user_id="u3", email="u3@example.com", created_at=original.created_at)
    await users.create(kept)
    assert kept.created_at == at(1), "automatic fields given as changes are kept"


async def test_update_actions(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user(tags={"a"}))
    created = user.created_at
    returned = await users.update(
        user,
        User.visits.add(2),
        User.tags.add({"b", "c"}),
        User.name.set("Ada"),
        User.age.set(36),
    )
    assert returned is user
    assert (user.visits, user.tags, user.name, user.age, user.version) == (2, {"a", "b", "c"}, "Ada", 36, 2)
    assert user.created_at == created
    assert user.updated_at is not None
    assert user.updated_at >= created
    await users.update(user, User.tags.delete({"a", "b"}), User.age.remove(), User.visits.set(User.visits + 10))
    stored = await users.get_or_raise("u1")
    assert (stored.tags, stored.age, stored.visits, stored.version) == ({"c"}, None, 12, 3)
    by_key = await users.update(User.key("u1"), User.visits.decrement(2))
    assert by_key.visits == 10
    assert by_key.version == 4


async def test_update_requires_the_item_unless_upsert(events: dynamoxy.Repository[Event]) -> None:
    with pytest.raises(dynamoxy.ItemNotFoundError):
        await events.update(Event.key("s", 1), Event.kind.set("x"))
    created = await events.update(Event.key("s", 1), Event.kind.set("x"), upsert=True)
    assert (created.stream, created.seq, created.kind) == ("s", 1, "x")
    assert created.payload == {}, "untouched fields get their defaults"
    with pytest.raises(ValueError, match=r"Event\.kind\.set_if_not_exists"):
        await events.update(Event.key("s", 1), Event.payload["n"].set(1), upsert=True)
    again = await events.update(
        Event.key("s", 1), Event.payload["n"].set(1), Event.kind.set_if_not_exists("y"), upsert=True
    )
    assert (again.kind, again.payload) == ("x", {"n": 1}), "existing items keep their values"


async def test_upserts_create_complete_items(client: dynamoxy.Client) -> None:
    await client.create_tables(User, Customer, Order)
    users = client.repository(User)
    with dynamoxy.collect() as collected, pytest.raises(ValueError, match=r"required field\(s\) email, name"):
        await users.update(User.key("u1"), User.visits.add(1), upsert=True)
    with pytest.raises(ValueError, match="must set it in the actions"):
        await users.update(make_user("u2"), User.visits.add(1), upsert=True)
    assert collected.calls == 0, "refused before any I/O"
    created = await users.update(
        User.key("u3"), User.email.set("u3@example.com"), User.name.set("Ann"), User.visits.add(2), upsert=True
    )
    assert (created.visits, created.status, created.tags, created.version) == (2, Status.ACTIVE, set(), 1)
    assert created.created_at is not None
    assert created.created_at == created.updated_at
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await users.update(User.key("u4"), User.email.set("u3@example.com"), User.name.set("Bo"), upsert=True)
    loaded = await users.get_or_raise("u3")
    await users.update(loaded, User.visits.add(1), upsert=True)  # loaded: its guard exists already
    assert loaded.visits == 3
    orders = client.repository(Order)
    placed = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="pass the key as field values"):
        await orders.update(Order.key(customer_id="c1", order_id="o1"), Order.total.set(10), upsert=True)
    upserted = await orders.update(
        {"customer_id": "c1", "order_id": "o1"}, Order.placed_at.set(placed), Order.total.set(10), upsert=True
    )
    assert (upserted.pk, upserted.sk, upserted.status) == ("CUSTOMER#c1", "ORDER#o1", OrderStatus.NEW)
    found = await orders.query(customer_id="c1").all()
    assert [order.order_id for order in found] == ["o1"], "the entity type is written"
    assert (found[0].gsi1pk, found[0].gsi1sk) == ("STATUS#new", placed.isoformat(timespec="microseconds"))
    by_status = await orders.query(index=Order.by_status, status=OrderStatus.NEW).all()
    assert [order.order_id for order in by_status] == ["o1"], "derived index keys are written"
    await orders.update(
        {"customer_id": "c1", "order_id": "o1"},
        Order.status.set(OrderStatus.PAID),
        Order.placed_at.set_if_not_exists(placed),
        Order.total.set_if_not_exists(0),
        upsert=True,
    )
    paid = await orders.get_or_raise(customer_id="c1", order_id="o1")
    assert (paid.status, paid.gsi1pk, paid.total) == (OrderStatus.PAID, "STATUS#paid", 10)


async def test_derived_attributes_follow_arithmetic_updates(client: dynamoxy.Client) -> None:
    await client.create_tables(Player)
    players = client.repository(Player)
    ada = await players.create(Player(player_id="p1", name="Ada"))
    await players.create(Player(player_id="p2", name="Bo", score=5))
    await players.update(ada, Player.score.add(10))  # a loaded instance: computed from the stored item
    assert ada.rank == "SCORE#00000010"
    await players.update(Player.key("p2"), Player.score.set(Player.score + 20))  # by key: read first
    async with client.transaction() as tx:
        tx.update(ada, Player.score.increment(3))
    assert ada.rank == "SCORE#00000013"
    created = await players.update(
        Player.key("p3"), Player.name.set_if_not_exists("Cy"), Player.score.add(7), upsert=True
    )
    assert created.rank == "SCORE#00000007"
    ranked = await players.query(index=Player.by_rank, board="global").descending().all()
    assert [(player.player_id, player.rank) for player in ranked] == [
        ("p2", "SCORE#00000025"),
        ("p1", "SCORE#00000013"),
        ("p3", "SCORE#00000007"),
    ]


async def test_update_conditions_and_version_conflicts(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await users.update(user, User.visits.add(1), condition=User.status == Status.SUSPENDED)
    assert not isinstance(caught.value, dynamoxy.VersionConflictError)
    stale = await users.get_or_raise("u1")
    await users.update(user, User.visits.add(1))
    with pytest.raises(dynamoxy.VersionConflictError):
        await users.update(stale, User.visits.add(1))
    await users.update(stale.copy(), User.visits.add(1)) if False else None  # copies are not loaded


async def test_update_rejects_keys_and_derived_attributes(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    with pytest.raises(ValueError, match="key or derived"):
        await users.update(user, User.user_id.set("u2"))
    with pytest.raises(ValueError, match="at least one action"):
        await users.update(user)


async def test_delete_returns_the_item_and_checks_versions(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    stale = await users.get_or_raise("u1")
    await users.update(user, User.visits.add(1))
    with pytest.raises(dynamoxy.VersionConflictError):
        await users.delete(stale)
    deleted = await users.delete(user)
    assert deleted is not None
    assert deleted.visits == 1
    assert not user.is_loaded
    assert await users.delete("u1") is None
    assert await users.get("u1") is None


async def test_delete_with_conditions(events: dynamoxy.Repository[Event]) -> None:
    await events.put(Event(stream="s", seq=1, kind="keep"))
    with pytest.raises(dynamoxy.ConditionalCheckFailedError):
        await events.delete(("s", 1), condition=Event.kind == "drop")
    deleted = await events.delete(("s", 1), condition=Event.kind == "keep")
    assert deleted is not None
    assert deleted.kind == "keep"


async def test_batches(events: dynamoxy.Repository[Event]) -> None:
    items = [Event(stream="s", seq=index, kind="even" if index % 2 == 0 else "odd") for index in range(130)]
    await events.batch_put(items)
    keys = [Event.key("s", index) for index in range(0, 130, 3)] + [Event.key("s", 999)]
    found = await events.batch_get(keys)
    assert [event.seq for event in found] == list(range(0, 130, 3)), "order follows the keys; missing are skipped"
    assert await events.count(stream="s") == 130
    await events.batch_delete([("s", index) for index in range(100)])
    assert await events.count(stream="s") == 30


async def test_batch_writer_deduplicates_and_flushes(
    client: dynamoxy.Client, events: dynamoxy.Repository[Event]
) -> None:
    async with client.batch_writer(flush_size=10) as batch:
        for index in range(25):
            await batch.put(Event(stream="w", seq=index % 20, kind=f"v{index}"))
        await batch.delete(Event.key("w", 0), model=Event)
    assert await events.count(stream="w") == 19
    latest = await events.get("w", 1)
    assert latest is not None
    assert latest.kind in ("v1", "v21")


async def test_query_order_paging_and_cursors(events: dynamoxy.Repository[Event]) -> None:
    await events.batch_put(
        [Event(stream="q", seq=index, kind="even" if index % 2 == 0 else "odd") for index in range(20)]
    )
    query = events.query(stream="q")
    assert [event.seq async for event in query] == list(range(20))
    assert [event.seq for event in await query.descending().limit(3).all()] == [19, 18, 17]
    first_page = await query.page_size(7).page()
    assert [event.seq for event in first_page] == list(range(7))
    assert first_page.cursor is not None
    assert first_page.has_more
    second_page = await query.page_size(7).after(first_page.cursor).page()
    assert [event.seq for event in second_page] == list(range(7, 14))
    pages = [page async for page in query.page_size(8).pages()]
    assert sum(len(page) for page in pages) == 20
    odd = query.filter(Event.kind == "odd")
    assert await odd.count() == 10
    assert [event.seq for event in await odd.limit(4).all()] == [1, 3, 5, 7]
    assert (await query.where(Event.seq.between(5, 8)).all())[-1].seq == 8
    assert (await query.where(Event.seq >= 18).first()).seq == 18  # type: ignore[union-attr]
    assert await query.where(Event.seq > 100).first() is None
    assert not await query.where(Event.seq > 100).exists()
    assert (await query.where(Event.seq == 4).one()).seq == 4
    with pytest.raises(ValueError, match="more than one"):
        await query.one()
    with pytest.raises(dynamoxy.ItemNotFoundError):
        await query.where(Event.seq > 100).one()
    with pytest.raises(ValueError, match="invalid cursor"):
        await query.after("not-a-cursor").page()


async def test_limits_bound_requests_and_cursors_continue_after_the_last_item(
    events: dynamoxy.Repository[Event],
) -> None:
    await events.batch_put(
        [Event(stream="c", seq=index, kind="even" if index % 2 == 0 else "odd") for index in range(40)]
    )
    even = events.query(stream="c").filter(Event.kind == "even")
    page = await even.limit(5).page()
    assert page.scanned_count == 5, "page() evaluates at most `limit` items, like page_size"
    assert [event.seq for event in page] == [0, 2, 4]
    assert page.cursor is not None
    # An API handing out five items and the cursor of their page at a time sees every item once:
    # the page cut at the limit continues right after its last item.
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(20):
        query = even.limit(5).after(cursor) if cursor else even.limit(5)
        pages = [page async for page in query.pages()]
        seen += [event.seq for page in pages for event in page.items]
        cursor = pages[-1].cursor
        if cursor is None:
            break
    assert seen == list(range(0, 40, 2))
    rows = await even.limit(3).project(Event.kind).all()
    assert rows == [{"kind": "even"}] * 3, "key attributes read for cursors stay out of projections"


async def test_queries_take_one_sort_key_condition(events: dynamoxy.Repository[Event]) -> None:
    query = events.query(stream="q")
    with pytest.raises(ValueError, match="one sort key condition"):
        query.where(Event.seq >= 1).where(Event.seq <= 5)
    with pytest.raises(ValueError, match="one sort key condition"):
        query.where(Event.seq >= 1, Event.seq <= 5)
    with pytest.raises(ValueError, match="one sort key condition"):
        events.query(stream="q", seq=1).where(Event.seq >= 1)
    assert await query.where(Event.seq.between(1, 5)).all() == []


async def test_parallel_scans_that_stop_early_finish(events: dynamoxy.Repository[Event]) -> None:
    await events.batch_put([Event(stream=f"s{index % 7}", seq=index, kind="k") for index in range(300)])
    scan = events.scan().segments(4)

    def workers() -> list[asyncio.Task[Any]]:
        return [task for task in asyncio.all_tasks() if task.get_coro().__qualname__.endswith("worker")]  # type: ignore[union-attr]

    # Every segment fills the queue while the scan stops after one item: nothing may block.
    assert len(await asyncio.wait_for(scan.limit(1).all(), timeout=10)) == 1
    assert len(await asyncio.wait_for(scan.limit(5).all(), timeout=10)) == 5
    assert await asyncio.wait_for(scan.first(), timeout=10) is not None
    assert not workers(), "first() stops its segments"
    async for _ in scan:
        break
    await wait_for(lambda: not workers())
    assert len(await scan.all()) == 300
    with pytest.raises(TypeError, match="every segment from its start"):
        await events.scan().after({"stream": "s1", "seq": 1}).segments(2).all()


async def test_projection_returns_dicts(events: dynamoxy.Repository[Event]) -> None:
    await events.put(Event(stream="p", seq=1, kind="k", payload={"x": 1, "y": 2}))
    rows = await events.query(stream="p").project(Event.kind, F("payload")["x"]).all()
    assert rows == [{"kind": "k", "payload": {"x": 1}}]


async def test_global_and_local_indexes(events: dynamoxy.Repository[Event]) -> None:
    await events.batch_put(
        [Event(stream=f"s{index % 3}", seq=index, kind="a" if index < 5 else "b") for index in range(10)]
    )
    kind_a = await events.query(index=Event.by_kind, kind="a").all()
    assert sorted(event.seq for event in kind_a) == [0, 1, 2, 3, 4]
    local = await events.query(index="by_kind_local", stream="s0").where(Event.kind == "b").all()
    assert sorted(event.seq for event in local) == [6, 9]
    with pytest.raises(ValueError, match="has no index 'nope'"):
        events.query(index="nope", kind="a")


async def test_keys_only_index_and_hydration(users: dynamoxy.Repository[User]) -> None:
    for index in range(3):
        await users.create(make_user(f"u{index}"))
    query = users.query(index=User.by_status, status=Status.ACTIVE)
    keys = await query.project(User.user_id).all()
    assert sorted(row["user_id"] for row in keys) == ["u0", "u1", "u2"]
    with pytest.raises(ValueError, match=r"add \.hydrate\(\)"):
        await query.all()
    assert await query.exists(), "exists() decodes nothing"
    hydrated = await query.hydrate().all()
    assert sorted(user.name for user in hydrated) == ["U0", "U1", "U2"]


async def test_hydration_reads_as_consistently_as_the_query(
    client: dynamoxy.Client, events: dynamoxy.Repository[Event]
) -> None:
    await events.batch_put([Event(stream="h", seq=index, kind="k") for index in range(2)])
    sent = record_consistent_reads(client)
    hydrated = events.query(index=Event.by_kind_local, stream="h").hydrate()
    assert len(await hydrated.all()) == 2
    assert len(await hydrated.consistent().all()) == 2
    assert sent == [("Query", False), ("BatchGetItem", False), ("Query", True), ("BatchGetItem", True)]


async def test_scans_filters_and_segments(events: dynamoxy.Repository[Event]) -> None:
    await events.batch_put([Event(stream=f"s{index}", seq=index, kind="k") for index in range(40)])
    assert len(await events.scan().all()) == 40
    assert await events.scan().count() == 40
    assert len(await events.scan().segments(4).all()) == 40
    assert await events.scan().segments(3).count() == 40
    assert len(await events.scan().segments(4).limit(5).all()) == 5
    segment_items = [await events.scan().segment(index, 4).all() for index in range(4)]
    assert sum(len(items) for items in segment_items) == 40
    filtered = await events.scan().filter(Event.seq >= 30).all()
    assert sorted(event.seq for event in filtered) == list(range(30, 40))
    assert await events.count() == 40


async def test_consistent_queries_and_scans(client: dynamoxy.Client, events: dynamoxy.Repository[Event]) -> None:
    await events.batch_put([Event(stream="c", seq=index, kind="k") for index in range(3)])
    sent = record_consistent_reads(client)
    eventual = await events.query(stream="c").page()
    consistent = await events.query(stream="c", consistent=True).page()
    assert [event.seq for event in consistent] == [0, 1, 2]
    assert eventual.consumed_capacity is not None
    assert consistent.consumed_capacity is not None
    assert consistent.consumed_capacity.capacity_units == 2 * eventual.consumed_capacity.capacity_units
    assert len(await events.scan(consistent=True).all()) == 3
    assert await events.count(consistent=True) == 3
    assert await events.count(stream="c", consistent=True) == 3
    local = await events.query(index=Event.by_kind_local, stream="c", consistent=True).all()
    assert sorted(event.seq for event in local) == [0, 1, 2]
    assert sent == [
        ("Query", False),
        ("Query", True),
        ("Scan", True),
        ("Scan", True),  # count() without key values
        ("Query", True),  # count() of a partition
        ("Query", True),  # local indexes support consistent reads
    ]


async def test_consistent_reads_of_global_indexes_fail_before_any_io(client: dynamoxy.Client) -> None:
    events = client.repository(Event)
    refused = r"^Event index 'by_kind' is a global secondary index, which does not support consistent reads: remove"
    with dynamoxy.collect() as usage:
        with pytest.raises(ValueError, match=refused):
            events.query(index=Event.by_kind, kind="k", consistent=True)
        with pytest.raises(ValueError, match=refused):
            events.query(index=Event.by_kind, kind="k").consistent()
        with pytest.raises(ValueError, match=refused):
            events.scan(index="by_kind", consistent=True)
        with pytest.raises(ValueError, match=refused):
            await events.count(index=Event.by_kind, kind="k", consistent=True)
        events.query(index=Event.by_kind, kind="k").consistent(False)
    assert usage.calls == 0


async def test_ttl_attribute_is_epoch_seconds(client: dynamoxy.Client, events: dynamoxy.Repository[Event]) -> None:
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    await events.put(Event(stream="t", seq=1, kind="k", expires_at=moment))
    raw = await client.table("events").get(Event.key("t", 1))
    assert raw is not None
    assert raw["expires_at"] == int(moment.timestamp())
    ttl = await client.table("events").time_to_live()
    assert ttl.enabled
    assert ttl.attribute == "expires_at"


class Reading(dynamoxy.Model, table="readings", datetime_format="iso_z"):
    """Timestamps stored as RFC 3339 text with a ``Z`` suffix."""

    sensor_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
    taken_at: dynamoxy.Attr[datetime] = dynamoxy.sort_key()
    value: dynamoxy.Attr[int]
    created_at: dynamoxy.Attr[datetime] = dynamoxy.created_at()
    updated_at: dynamoxy.Attr[datetime] = dynamoxy.updated_at()


async def test_timestamps_with_a_z_suffix(client: dynamoxy.Client) -> None:
    await client.create_tables(Reading)
    readings = client.repository(Reading)
    for hour in range(4):
        await readings.create(Reading(sensor_id="s1", taken_at=at(1, hour), value=hour))
    raw = await client.table("readings").get(Reading.key("s1", at(1, 2)))
    assert raw is not None
    assert raw["taken_at"] == "2026-01-01T02:00:00.000000Z"
    assert raw["created_at"] == raw["updated_at"]
    assert raw["created_at"].endswith("Z")
    window = await readings.query(sensor_id="s1").where(Reading.taken_at.between(at(1, 1), at(1, 2))).all()
    assert [reading.value for reading in window] == [1, 2]
    await readings.update(window[0], Reading.value.add(10))
    raw = await client.table("readings").get(Reading.key("s1", at(1, 1)))
    assert raw is not None
    assert raw["value"] == 11
    assert raw["updated_at"].endswith("Z")
    assert raw["updated_at"] >= raw["created_at"]
    # Items written by other services or before a format change are read in either style...
    written_elsewhere = {"sensor_id": "s2", "taken_at": "2026-01-01T00:00:00+00:00", "value": 1}
    await client.table("readings").put({**written_elsewhere, "created_at": "2026-01-01T00:00:00.5Z"})
    legacy = await readings.query(sensor_id="s2").one()
    assert legacy.taken_at == at(1)
    assert legacy.created_at == at(1) + timedelta(milliseconds=500)
    # ...but keys compare stored text: this key is "...00.000000Z", the stored one "...00+00:00".
    assert await readings.get("s2", at(1)) is None


async def test_validation_fails_before_any_io(client_factory: ClientFactory) -> None:
    client = client_factory()
    users = client.repository(User)
    user = make_user()
    user.__dict__["email"] = "broken"  # bypass assignment validation to simulate corrupted state
    with dynamoxy.collect() as usage, pytest.raises(dynamoxy.ValidationError, match="email"):
        await users.put(user)
    assert usage.calls == 0


async def test_model_and_client_hooks(client: dynamoxy.Client) -> None:
    calls: list[str] = []

    class Hooked(dynamoxy.Model, table="hooked"):
        pk: dynamoxy.Attr[str] = dynamoxy.partition_key()
        email: dynamoxy.Attr[str]

        @dynamoxy.before_save
        def lower(self) -> None:
            calls.append("model.before_save")
            self.email = self.email.lower()

        @dynamoxy.after_load
        def loaded(self) -> None:
            calls.append("model.after_load")

        @dynamoxy.before_delete
        async def deleting(self) -> None:
            calls.append("model.before_delete")

    @client.hook("before_save", Hooked)
    async def audit(item: Hooked, context: dynamoxy.HookContext) -> None:
        calls.append(f"client.before_save:{context.operation}")

    client.hooks.add("after_delete", lambda item, context: calls.append("client.after_delete"))
    await client.create_tables(Hooked)
    repository = client.repository(Hooked)
    await repository.create(Hooked(pk="h", email="ADA@EXAMPLE.COM"))
    stored = await repository.get_or_raise("h")
    assert stored.email == "ada@example.com"
    await repository.delete(stored)
    assert calls == [
        "model.before_save",
        "client.before_save:create",
        "model.after_load",
        "model.before_delete",
        "client.after_delete",
        "model.after_load",  # delete() returns the deleted item, decoded from DynamoDB
    ]


async def test_hooks_can_abort_writes(client: dynamoxy.Client) -> None:
    class Guarded(dynamoxy.Model, table="guarded"):
        pk: dynamoxy.Attr[str] = dynamoxy.partition_key()

        @dynamoxy.before_save
        def refuse(self) -> None:
            raise PermissionError("read-only")

    await client.create_tables(Guarded)
    repository = client.repository(Guarded)
    with pytest.raises(PermissionError):
        await repository.put(Guarded(pk="x"))
    assert await repository.get("x") is None


async def test_update_hooks_run_for_updates_of_instances(client: dynamoxy.Client) -> None:
    calls: list[str] = []

    class Counted(dynamoxy.Model, table="counted"):
        pk: dynamoxy.Attr[str] = dynamoxy.partition_key()
        visits: dynamoxy.Attr[int] = dynamoxy.field(default=0)

        @dynamoxy.before_update
        def checking(self) -> None:
            calls.append(f"before_update {self.visits}")
            if self.pk == "frozen":
                raise PermissionError("frozen")

        @dynamoxy.after_update
        async def updated(self) -> None:
            calls.append(f"after_update {self.visits}")

    @client.hook("after_update", Counted)
    def audit(item: Counted, context: dynamoxy.HookContext) -> None:
        calls.append(f"client after_update ({context.operation})")

    await client.create_tables(Counted)
    counted = client.repository(Counted)
    item = await counted.create(Counted(pk="c"))
    await counted.update(item, Counted.visits.add(1))
    async with client.transaction() as tx:
        tx.update(item, Counted.visits.add(2))
    await counted.update(Counted.key("c"), Counted.visits.add(1))  # by key: no instance, so no hooks
    assert calls == [
        "before_update 0",
        "after_update 1",
        "client after_update (update)",
        "before_update 1",
        "after_update 3",
        "client after_update (update)",
    ]
    frozen = await counted.create(Counted(pk="frozen"))
    with dynamoxy.collect() as usage, pytest.raises(PermissionError):
        await counted.update(frozen, Counted.visits.add(1))
    assert usage.calls == 0, "refused before any I/O"
    assert (await counted.get_or_raise("frozen")).visits == 0


async def test_repository_subclasses(client: dynamoxy.Client) -> None:
    class UserRepository(dynamoxy.Repository[User]):
        async def by_email(self, email: str) -> User | None:
            return await self.query(index=User.by_email, email=email).first()

    await client.create_tables(User)
    repository = UserRepository(client)
    await repository.create(make_user("u7"))
    found = await repository.by_email("u7@example.com")
    assert found is not None
    assert found.user_id == "u7"
    with pytest.raises(TypeError, match="does not know its model"):
        dynamoxy.Repository(client)


async def test_updates_validate_values_they_set(client: dynamoxy.Client) -> None:
    await client.create_tables(User)
    users = client.repository(User)
    user = await users.create(make_user("u1"))
    with dynamoxy.collect() as collected, pytest.raises(dynamoxy.ValidationError, match=r"User\.age: .*0") as caught:
        await users.update(user, User.age.set(-1))
    assert [error.path for error in caught.value.errors] == ["age"]
    with pytest.raises(dynamoxy.ValidationError, match="is required and cannot be removed"):
        await users.update(user, User.name.remove())
    with pytest.raises(dynamoxy.ValidationError, match="email"):
        await users.update(user, User.email.set("not an email"))
    assert collected.calls == 0, "nothing was sent"
    await users.update(
        user, User.age.remove(), User.visits.add(-5)
    )  # optional fields can go; arithmetic is not checked
    assert (user.age, user.visits) == (None, -5)


async def test_set_none_removes_attributes(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user(age=30))
    await users.update(user, User.age.set(None))
    assert user.age is None
    stored = await users.table.get({"user_id": "u1"})
    assert stored is not None
    assert "age" not in stored, "None is not stored, as with put() and save()"
    assert [found.user_id for found in await users.scan().filter(User.age == None).all()] == ["u1"]  # noqa: E711
    # A NULL that another writer stored reads as None, and compares as None.
    await users.table.put({**stored, "user_id": "u2", "email": "u2@example.com", "age": None})
    assert (await users.get_or_raise("u2")).age is None
    missing = await users.scan(consistent=True).filter(User.age == None).all()  # noqa: E711
    assert sorted(found.user_id for found in missing) == ["u1", "u2"]
    assert await users.scan(consistent=True).filter(User.age != None).count() == 0  # noqa: E711
    with pytest.raises(dynamoxy.ValidationError, match="is required and cannot be removed"):
        await users.update(user, User.name.set(None))  # type: ignore[call-overload]


async def test_templates_follow_automatic_fields(client: dynamoxy.Client) -> None:
    await client.create_tables(Ticket)
    tickets = client.repository(Ticket)
    ticket = await tickets.create(Ticket(ticket_id="t1"))

    async def check() -> None:
        stored = await tickets.table.get({"ticket_id": "t1"}, consistent=True)
        assert stored is not None
        assert stored["recent"] == stored["updated_at"]
        assert stored["state_recent"] == f"{stored['state']}#{stored['updated_at']}"
        assert stored["revision"] == f"v{stored['version']}"

    await check()
    await tickets.update(ticket, Ticket.title.set("hello"))
    assert ticket.revision == "v2"
    await check()
    await ticket.update(Ticket.state.set("closed"))
    await check()
    await tickets.update(Ticket.key("t1"), Ticket.title.set("by key"))  # reads the version it cannot know
    await check()
    assert (await tickets.get_or_raise("t1")).revision == "v4"


async def test_loaded_instances_keep_immutable_values(client: dynamoxy.Client) -> None:
    await client.create_tables(Ticket)
    tickets = client.repository(Ticket)
    ticket = await tickets.create(Ticket(ticket_id="t1"))
    ticket.country = "NO"
    with pytest.raises(ValueError, match=r"Ticket\.country is immutable"):
        await ticket.save()
    with pytest.raises(ValueError, match=r"Ticket\.country is immutable"):
        await tickets.put(ticket)
    assert (await tickets.get_or_raise("t1")).country == "SE"
    ticket.country = "SE"
    ticket.title = "fine"
    await ticket.save()
    assert ticket.version == 2
    # An instance that was not loaded replaces the item, as put() does.
    await tickets.put(Ticket(ticket_id="t1", country="DK"))
    assert (await tickets.get_or_raise("t1")).country == "DK"


async def test_keys_given_as_field_values(client: dynamoxy.Client) -> None:
    await client.create_tables(Device)
    devices = client.repository(Device)
    device_id = uuid.uuid4()
    await devices.create(Device(device_id=device_id))
    updated = await devices.update({"device_id": device_id}, Device.label.set("x"))
    assert updated.label == "x"
    assert (await devices.get_or_raise(device_id)).label == "x"
    assert await devices.update({"device_id": str(device_id)}, Device.label.set("y")) is not None, "or stored"
    deleted = await devices.delete({"device_id": device_id})
    assert deleted is not None
    assert deleted.label == "y"


async def test_float_keys_in_batch_reads(client: dynamoxy.Client) -> None:
    await client.create_tables(Measurement)
    measurements = client.repository(Measurement)
    values = [0.1, 0.3, 2.5, 1e16]
    await measurements.batch_put([Measurement(sensor="s", value=value) for value in values])
    found = await measurements.batch_get([("s", value) for value in values])
    assert [measurement.value for measurement in found] == values, "0.1 is read back as Decimal('0.1')"


async def test_index_reads_never_make_up_values(client: dynamoxy.Client, backend: str) -> None:
    await client.create_tables(Counter, Event)
    counters = client.repository(Counter)
    await counters.create(Counter(counter_id="c1", count=42, note="important"))
    by_group = counters.query(index=Counter.by_group, group="a")
    with pytest.raises(ValueError, match="does not hold every attribute of Counter"):
        await by_group.first()  # it would be Counter(count=0, note=None), and save() would store that
    assert await by_group.exists()
    assert [(item.count, item.note) for item in await by_group.hydrate().all()] == [(42, "important")]
    assert await by_group.project("counter_id").all() == [{"counter_id": "c1"}]
    events = client.repository(Event)
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    await events.put(Event(stream="x", seq=1, kind="k", payload={"a": 1}, expires_at=expires))
    local = await events.query(index="by_kind_local", stream="x").all()
    if backend == "memory":  # MiniStack ignores Select=ALL_ATTRIBUTES on local indexes; DynamoDB reads the table
        assert [event.expires_at for event in local] == [expires], "a local index reads the other attributes"
