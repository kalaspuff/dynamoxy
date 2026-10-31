"""Single-table design: key templates, entity types, derived index keys and collections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import dynamoxy
from dynamoxy import Attr, GlobalIndex, IndexDefinition, Model, Projection, field, partition_key, sort_key
from tests.helpers import record_consistent_reads
from tests.models import Customer, LineItem, Order, OrderStatus


class Payment(Model, table="ledger", entity="Payment"):
    """Two entities sharing an index that holds only some attributes."""

    pk: Attr[str] = partition_key(template="ACCOUNT#{account_id}")
    sk: Attr[str] = sort_key(template="PAYMENT#{payment_id}")
    account_id: Attr[str]
    payment_id: Attr[str]
    state: Attr[str] = field(default="open")
    amount: Attr[int] = field(default=0)
    gsi1pk: Attr[str] = field(template="STATE#{state}")

    by_state = GlobalIndex("gsi1", partition_key=gsi1pk, projection=Projection.keys_only())


class Price(Model, table="catalog", entity="Price"):
    """Key templates with a Decimal and a formatted datetime."""

    pk: Attr[str] = partition_key(template="SKU#{sku}")
    sk: Attr[str] = sort_key(template="PRICE#{amount}#{valid_from:%Y-%m-%d}")
    sku: Attr[str]
    amount: Attr[Decimal]
    valid_from: Attr[datetime]


class Refund(Model, table="ledger", entity="Refund"):
    pk: Attr[str] = partition_key(template="ACCOUNT#{account_id}")
    sk: Attr[str] = sort_key(template="REFUND#{refund_id}")
    account_id: Attr[str]
    refund_id: Attr[str]
    state: Attr[str] = field(default="open")
    gsi1pk: Attr[str] = field(template="STATE#{state}")

    by_state = GlobalIndex("gsi1", partition_key=gsi1pk, projection=Projection.include("state"))


class Profile(Model, table="people", entity="Profile"):
    """Two entities sharing a partition key whose validator normalizes values."""

    person: Attr[str] = partition_key(validators=(str.lower,))
    sk: Attr[str] = sort_key(template="PROFILE")


class Note(Model, table="people", entity="Note"):
    person: Attr[str] = partition_key(validators=(str.lower,))
    sk: Attr[str] = sort_key(template="NOTE#{note_id}")
    note_id: Attr[str]


@pytest.fixture
async def app(client: dynamoxy.Client) -> dynamoxy.Client:
    created = await client.create_tables(Customer, Order)
    assert len(created) == 1, "models sharing a table create it once"
    return client


def order(order_id: str, day: int, status: OrderStatus = OrderStatus.NEW, customer_id: str = "c1") -> Order:
    return Order(
        customer_id=customer_id,
        order_id=order_id,
        placed_at=datetime(2026, 1, day, tzinfo=UTC),
        total=Decimal(day),
        status=status,
        lines=[LineItem(sku=f"sku-{order_id}", quantity=1, price=Decimal(day))],
    )


async def test_templates_build_keys_and_queries_select_one_entity(app: dynamoxy.Client) -> None:
    await app.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    orders = app.model(Order)
    for index in range(1, 4):
        await orders.create(order(f"o{index}", index))
    await orders.create(order("x", 9, customer_id="c2"))
    stored = await orders.get(customer_id="c1", order_id="o2")
    assert stored is not None
    assert stored.pk == "CUSTOMER#c1"
    assert stored.sk == "ORDER#o2"
    assert stored.lines[0].sku == "sku-o2"
    in_partition = await orders.query(customer_id="c1").all()
    assert [item.order_id for item in in_partition] == ["o1", "o2", "o3"], "the profile item is not an order"
    one = await orders.query(customer_id="c1", order_id="o3").all()
    assert [item.order_id for item in one] == ["o3"]
    profile = await app.model(Customer).query(customer_id="c1").all()
    assert [item.name for item in profile] == ["Ada"]


async def test_derived_index_keys_follow_updates(app: dynamoxy.Client) -> None:
    orders = app.model(Order)
    first = await orders.create(order("o1", 1))
    await orders.create(order("o2", 2, OrderStatus.PAID))
    paid = await orders.query(index=Order.by_status, status=OrderStatus.PAID).all()
    assert [item.order_id for item in paid] == ["o2"]
    await orders.update(first, Order.status.set(OrderStatus.PAID))
    assert first.gsi1pk == "STATUS#paid"
    paid = await orders.query(index="gsi1", gsi1pk="STATUS#paid").all()
    assert sorted(item.order_id for item in paid) == ["o1", "o2"]
    # An update by key reads the stored item to rebuild derived keys that need other fields.
    await orders.update(Order.key(customer_id="c1", order_id="o2"), Order.status.set(OrderStatus.SHIPPED))
    shipped = await orders.query(index=Order.by_status, status=OrderStatus.SHIPPED).all()
    assert [item.order_id for item in shipped] == ["o2"]
    newest_first = await orders.query(index=Order.by_status, status=OrderStatus.PAID).descending().all()
    assert [item.order_id for item in newest_first] == ["o1"]


async def test_collections_decode_mixed_partitions(app: dynamoxy.Client) -> None:
    await app.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.model(Order).create(order("o1", 1))
    await app.model(Order).create(order("o2", 2))
    view = app.collection(Customer, Order)
    items = await view.query(customer_id="c1").all()
    assert sorted(type(item).__name__ for item in items) == ["Customer", "Order", "Order"]
    by_raw_key = await view.query(pk="CUSTOMER#c1").all()
    assert len(by_raw_key) == 3
    everything = await view.scan().all()
    assert len(everything) == 3
    with pytest.raises(TypeError, match="share one table"):
        app.collection(Customer, dynamoxy.Model)  # type: ignore[arg-type]


async def test_collections_validate_key_values(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile, Note)
    await client.model(Profile).create(Profile(person="Ada"))
    await client.model(Note).create(Note(person="ADA", note_id="n1"))
    items = await client.collection(Profile, Note).query(person="Ada").all()
    assert sorted(type(item).__name__ for item in items) == ["Note", "Profile"]


async def test_collections_read_consistently(app: dynamoxy.Client) -> None:
    await app.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.model(Order).create(order("o1", 1))
    view = app.collection(Customer, Order)
    sent = record_consistent_reads(app)
    assert len(await view.query(customer_id="c1", consistent=True).all()) == 2
    assert len(await view.scan(consistent=True).all()) == 2
    assert sent == [("Query", True), ("Scan", True)]
    refused = "index 'gsi1' is a global secondary index, which does not support consistent reads"
    with dynamoxy.collect() as usage:
        with pytest.raises(ValueError, match=refused):
            view.query(index="gsi1", status=OrderStatus.NEW, consistent=True)
        with pytest.raises(ValueError, match=refused):
            view.scan(index="gsi1", consistent=True)
    assert usage.calls == 0


async def test_entity_types_keep_models_apart(app: dynamoxy.Client) -> None:
    await app.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.model(Order).create(order("o1", 1))
    assert await app.model(Order).count() == 1
    assert await app.model(Customer).count() == 1
    raw = await app.table("app").scan().all()
    assert sorted(item["_type"] for item in raw) == ["Customer", "Order"]


async def test_where_replaces_the_prefix_of_templated_sort_keys(app: dynamoxy.Client) -> None:
    await app.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    orders = app.model(Order)
    for order_id, day in (("2025-01", 1), ("2025-02", 2), ("2026-01", 3)):
        await orders.create(order(order_id, day))
    query = orders.query(customer_id="c1")
    in_2025 = await query.where(Order.sk.begins_with("ORDER#2025")).all()
    assert [item.order_id for item in in_2025] == ["2025-01", "2025-02"]
    # "PROFILE" sorts after every "ORDER#...": the entity type keeps the customer out.
    later = await query.where(Order.sk >= "ORDER#2025-02").descending().all()
    assert [item.order_id for item in later] == ["2026-01", "2025-02"]
    window = await query.where(Order.sk.between("ORDER#2025-02", "ORDER#2026-12")).all()
    assert [item.order_id for item in window] == ["2025-02", "2026-01"]
    assert await query.where(Order.sk < "ORDER#2025-02").count() == 1


async def test_first_and_limits_read_what_they_return(app: dynamoxy.Client) -> None:
    orders = app.model(Order)
    await orders.batch_put([order(f"o{index:02}", 1 + index % 27) for index in range(30)])
    limits: list[int | None] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        if request.operation == "Query":
            limits.append(request.params.get("limit"))
        return await call_next(request)

    app.use(record)
    newest = await orders.query(customer_id="c1").descending().first()
    assert newest is not None
    assert newest.order_id == "o29"
    assert [item.order_id for item in await orders.query(customer_id="c1").limit(3).all()] == ["o00", "o01", "o02"]
    assert limits == [1, 3], "the entity filter dynamoxy adds does not make limits read whole pages"


class Memo(Model, table="memos"):
    """No entity type: where() on the templated sort key leaves its prefix to a client check."""

    pk: Attr[str] = partition_key(template="USER#{user_id}")
    sk: Attr[str] = sort_key(template="MEMO#{memo_id}")
    user_id: Attr[str]
    memo_id: Attr[str]


async def test_limits_grow_but_stay_bounded(client: dynamoxy.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pages whose items dynamoxy's own filters or checks removed make the next request ask for
    # more. Without a bound, about 16 such pages (16 MB of other items) asked for more than a
    # request can say, and the read raised OverflowError; a larger growth gets there at once.
    monkeypatch.setattr("dynamoxy.query._GROWTH", 2**32)
    await client.create_tables(Memo)
    await Memo.create(client, user_id="u1", memo_id="m1")
    await client.table("memos").put({"pk": "USER#u1", "sk": "ZZZ"})  # another item, after the memos
    limits: list[int | None] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        limits.append(request.params.get("limit"))
        return await call_next(request)

    client.use(record)
    memo = await Memo.query(client, user_id="u1").where(Memo.sk >= "MEMO#").descending().first()
    assert memo is not None
    assert memo.memo_id == "m1"
    assert limits == [1, 2**20], "the other item came first, then a bounded request"
    # Limits and page sizes larger than a request can say are bounded too.
    limits.clear()
    memos = Memo.query(client, user_id="u1")
    assert len((await memos.limit(2**33).page()).items) == 1
    assert len((await memos.page_size(2**33).page()).items) == 1
    assert [memo.memo_id for memo in await memos.limit(2**33).all()] == ["m1"]
    assert all(limit == 2**20 for limit in limits), limits


async def test_indexes_that_hold_some_attributes(client: dynamoxy.Client) -> None:
    await client.create_tables(Payment, Refund)
    gsi1 = (await client.table("ledger").describe()).indexes["gsi1"]
    assert gsi1.projection == "INCLUDE"
    assert set(gsi1.projected_attributes) == {"_type", "state"}, "the entity type is projected for its filter"
    payments, refunds = client.model(Payment), client.model(Refund)

    async def write_items() -> None:
        await payments.create(Payment(account_id="a1", payment_id="p1", amount=5))
        await refunds.create(Refund(account_id="a1", refund_id="r1"))

    await write_items()
    open_payments = payments.query(index=Payment.by_state, state="open")
    assert [item.payment_id for item in await open_payments.hydrate().all()] == ["p1"]
    assert [row["sk"] for row in await open_payments.project("sk").all()] == ["PAYMENT#p1"]
    assert [item.refund_id for item in await refunds.query(index=Refund.by_state, state="open").hydrate().all()] == [
        "r1"
    ]
    with pytest.raises(ValueError, match=r"add \.hydrate\(\)"):
        await open_payments.all()
    with pytest.raises(ValueError, match="does not hold 'amount'"):
        open_payments.filter(Payment.amount > 1)
    # A table whose index does not hold the entity type (created before dynamoxy projected it):
    # the entries pass the filter, and hydrated items are checked on the client.
    await client.delete_tables(Payment)
    await client.table("ledger").create(
        partition_key=("pk", "S"),
        sort_key=("sk", "S"),
        indexes=[IndexDefinition("gsi1", "global", ("gsi1pk", "S"), projection=Projection.keys_only())],
    )
    await write_items()
    assert [item.payment_id for item in await open_payments.hydrate().all()] == ["p1"]
    assert [item.refund_id for item in await refunds.query(index=Refund.by_state, state="open").hydrate().all()] == [
        "r1"
    ]


async def test_keys_of_loaded_instances_cannot_change(app: dynamoxy.Client) -> None:
    orders = app.model(Order)
    placed = await orders.create(order("o1", 1))
    placed.customer_id = "c2"
    with pytest.raises(ValueError, match="key of this instance changed"):
        await placed.save()
    with pytest.raises(ValueError, match="key of this instance changed"):
        await orders.put(placed)
    with pytest.raises(ValueError, match="key of this instance changed"):
        await orders.batch_put([placed])
    assert [(item.customer_id, item.order_id) for item in await orders.scan().all()] == [("c1", "o1")]
    await orders.create(placed.copy())  # a copy is a new item
    await orders.delete(Order.key(customer_id="c1", order_id="o1"))
    assert [item.customer_id for item in await orders.scan().all()] == ["c2"]


async def test_writes_of_a_loaded_instance_never_reach_another_item(app: dynamoxy.Client) -> None:
    orders = app.model(Order)
    mine = await orders.create(order("o1", 1))
    await orders.create(order("o1", 2, customer_id="c2"))  # another customer's order with the same id
    mine.customer_id = "c2"  # the instance now renders the other order's key
    with pytest.raises(ValueError, match="key of this instance changed"):
        await mine.update(Order.total.set(Decimal(0)))
    with pytest.raises(ValueError, match="key of this instance changed"):
        await mine.delete()
    with pytest.raises(ValueError, match="key of this instance changed"):
        await app.transaction().update(mine, Order.total.set(Decimal(0))).delete(mine).commit()
    async with app.batch_writer() as batch:
        with pytest.raises(ValueError, match="key of this instance changed"):
            await batch.delete(mine)
    stored = {(item.customer_id, item.order_id): item.total for item in await orders.scan(consistent=True).all()}
    assert stored == {("c1", "o1"): Decimal(1), ("c2", "o1"): Decimal(2)}, "both orders unchanged"
    # refresh() reloads the item the instance was loaded from, local changes included.
    await mine.refresh()
    assert (mine.customer_id, mine.total) == ("c1", Decimal(1))

    # A field a key template uses is part of the key: updates cannot change it either.
    customers = app.model(Customer)
    ada = await customers.create(Customer(customer_id="a", name="Ada"))
    for update in (
        ada.update(Customer.customer_id.set("b")),
        customers.update(Customer.key(customer_id="a"), Customer.customer_id.set("b")),
        customers.update(Customer.key(customer_id="a"), dynamoxy.F("customer_id").remove()),
    ):
        with pytest.raises(ValueError, match=r"Customer\.customer_id is part of the key"):
            await update
    with pytest.raises(ValueError, match=r"Customer\.customer_id is part of the key"):
        await app.transaction().update(ada, Customer.customer_id.set("b")).commit()
    assert [(item.pk, item.customer_id) for item in await customers.scan(consistent=True).all()] == [
        ("CUSTOMER#a", "a")
    ]


async def test_templates_render_the_stored_form(client: dynamoxy.Client) -> None:
    await client.create_tables(Price)
    prices = client.model(Price)
    local = timezone(timedelta(hours=2))
    price = await prices.create(
        Price(sku="a", amount=Decimal("1.50"), valid_from=datetime(2026, 1, 1, 0, 30, tzinfo=local))
    )
    assert price.sk == "PRICE#1.5#2025-12-31"
    loaded = await prices.get_or_raise(
        sku="a", amount=Decimal("1.5"), valid_from=datetime(2025, 12, 31, 22, 30, tzinfo=UTC)
    )
    await loaded.save()
    assert [item.sk for item in await prices.query(sku="a").all()] == ["PRICE#1.5#2025-12-31"], "the same item"


class Offer(Model, table="catalog", entity="Offer"):
    """A key template with a number of a union field."""

    pk: Attr[str] = partition_key(template="SKU#{sku}")
    sk: Attr[str] = sort_key(template="OFFER#{amount}")
    sku: Attr[str]
    amount: Attr[Decimal | str]


async def test_templates_render_numbers_of_unions_as_stored(client: dynamoxy.Client) -> None:
    await client.create_tables(Offer)
    offers = client.model(Offer)
    assert (await offers.create(Offer(sku="a", amount=Decimal("1.50")))).sk == "OFFER#1.5"
    [loaded] = await offers.query(sku="a").all()
    assert loaded.amount == Decimal("1.5")
    await loaded.save()
    assert [item.sk for item in await offers.query(sku="a").all()] == ["OFFER#1.5"], "the same item"


class Seat(Model, table="seats", entity="Seat"):
    """Sort keys that repeat a field of the partition key, in the table and in an index."""

    pk: Attr[str] = partition_key(template="ORG#{org_id}")
    sk: Attr[str] = sort_key(template="MEMBER#{org_id}#{user_id}")
    org_id: Attr[str]
    user_id: Attr[str]
    status: Attr[str] = field(default="active")
    placed_at: Attr[str] = field(default="")
    note: Attr[str] = field(default="")
    gsi1pk: Attr[str] = field(template="STATUS#{status}")
    gsi1sk: Attr[str] = field(template="{status}#{placed_at}")

    by_status = GlobalIndex("gsi1", partition_key=gsi1pk, sort_key=gsi1sk)


async def test_sort_keys_may_repeat_fields_of_the_partition_key(client: dynamoxy.Client) -> None:
    await client.create_tables(Seat)
    for org, user, placed in [("o1", "u1", "2026-01"), ("o1", "u2", "2026-02"), ("o2", "u1", "2026-01")]:
        await Seat.create(client, org_id=org, user_id=user, placed_at=placed, note="x")
    assert await Seat.get(client, org_id="o1", user_id="u1") is not None
    found = await Seat.query(client, org_id="o1", user_id="u1").all()
    assert [(seat.org_id, seat.user_id) for seat in found] == [("o1", "u1")]
    assert await Seat.query(client, org_id="o1", user_id="u1").count() == 1
    assert await Seat.query(client, org_id="o1").count() == 2, "the partition alone"
    by_status = Seat.query(client, index=Seat.by_status, status="active", placed_at="2026-01")
    assert sorted(seat.org_id for seat in await by_status.all()) == ["o1", "o2"]
    assert await Seat.query(client, index=Seat.by_status, status="active").count() == 3
    with pytest.raises(ValueError, match="unexpected arguments team"):
        Seat.query(client, org_id="o1", user_id="u1", team="t")


async def test_counts_of_projected_reads(client: dynamoxy.Client) -> None:
    await client.create_tables(Seat)
    for user in ("u1", "u2", "u3"):
        await Seat.create(client, org_id="o1", user_id=user, note="x")
    assert await Seat.query(client, org_id="o1").project(Seat.note).count() == 3
    assert await client.model(Seat).scan().project(Seat.note).count() == 3
    table = client.table("seats")
    assert await table.query(dynamoxy.F("pk") == "ORG#o1").project("note").count() == 3
    assert await table.scan().project("note").count() == 3
    page = await client.model(Seat).scan().limit(1).page()
    assert page.cursor is not None
    with pytest.raises(TypeError, match=r"counts every segment from its start"):
        await client.model(Seat).scan().after(page.cursor).segments(2).count()


class Place(dynamoxy.Document):
    city: Attr[str] = field(default="")


class PostalPlace(Place):
    postcode: Attr[str] = field(default="")


class Member(Model, table="members-and-admins", entity="Member"):
    member_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    place: Attr[Place | None] = field(default=None)


class Admin(Member, entity="Admin"):
    level: Attr[int] = field(default=0)


async def test_repositories_refuse_instances_of_subclasses(client: dynamoxy.Client) -> None:
    # A repository of Member stored an Admin as a Member: without level, and with _type "Member",
    # so that Admin queries no longer found it.
    await client.create_tables(Member)
    members, admins = client.model(Member), client.model(Admin)
    admin = await admins.create(Admin(member_id="a", level=9))
    refused = "expected a Member, got an instance of its subclass Admin"
    for write in (
        lambda: members.create(Admin(member_id="b", level=1)),
        lambda: members.put(admin),
        lambda: members.save(admin),
        lambda: members.update(admin, Member.name.set("x")),
        lambda: members.delete(admin),
        lambda: members.refresh(admin),
    ):
        with pytest.raises(TypeError, match=refused):
            await write()
    with pytest.raises(TypeError, match=refused):
        async with client.transaction() as tx:
            tx.delete(admin, model=Member)
    with pytest.raises(TypeError, match=refused):
        async with client.batch_writer() as batch:
            await batch.delete(admin, model=Member)
    [stored] = await members.table.scan(consistent=True).all()
    assert (stored["_type"], stored["level"]) == ("Admin", 9)
    assert await admins.query(member_id="a").all() == [admin]


async def test_reads_refuse_items_that_another_model_stores(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    members, admins = client.model(Member), client.model(Admin)
    await admins.create(Admin(member_id="a", level=9))
    # Read as a Member, its save() would have stored it as one.
    with pytest.raises(
        dynamoxy.ItemDecodeError, match=r"it stores another model, Admin \(_type is 'Admin'\)"
    ) as caught:
        await members.get("a")
    assert caught.value.key == {"member_id": "a"}
    with pytest.raises(dynamoxy.ItemDecodeError, match=r"read it with client\.model\(Admin\)"):
        await members.batch_get(["a"])
    assert await members.query(member_id="a").all() == []
    # Writes by key are not refused before they are made; the item they return is. An upsert
    # does not re-type the item either.
    for upsert in (False, True):
        with pytest.raises(dynamoxy.ItemDecodeError, match="the update was applied"):
            await members.update(Member.key("a"), Member.name.set("Ada"), upsert=upsert)
    stored = await admins.get_or_raise("a")
    assert (stored.name, stored.level) == ("Ada", 9)
    # An entity name no model declares (one renamed since) still decodes.
    await members.table.put({"member_id": "m", "name": "Old", "_type": "LegacyMember"})
    assert (await members.get_or_raise("m")).name == "Old"


async def test_documents_refuse_instances_of_subclasses(client: dynamoxy.Client) -> None:
    # Encoded as a Place, a PostalPlace lost its postcode.
    await client.create_tables(Member)
    members = client.model(Member)
    with pytest.raises(dynamoxy.ValidationError, match="expected Place, got PostalPlace, a subclass"):
        await members.create(Member(member_id="m", place=PostalPlace(city="Lund", postcode="22100")))
    member = await members.create(Member(member_id="m", place=Place(city="Lund")))
    with pytest.raises(dynamoxy.ValidationError, match="expected Place, got PostalPlace"):
        await members.update(member, Member.place.set(PostalPlace(city="Lund", postcode="22100")))
    assert (await members.get_or_raise("m")).place == Place(city="Lund")
