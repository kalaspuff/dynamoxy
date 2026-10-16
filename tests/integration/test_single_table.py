"""Single-table design: key templates, entity types, derived index keys and collections."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import dynamoxy
from tests.helpers import record_consistent_reads
from tests.models import Customer, LineItem, Order, OrderStatus


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
    await app.repository(Customer).create(Customer(customer_id="c1", name="Ada"))
    orders = app.repository(Order)
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
    profile = await app.repository(Customer).query(customer_id="c1").all()
    assert [item.name for item in profile] == ["Ada"]


async def test_derived_index_keys_follow_updates(app: dynamoxy.Client) -> None:
    orders = app.repository(Order)
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
    await app.repository(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.repository(Order).create(order("o1", 1))
    await app.repository(Order).create(order("o2", 2))
    view = app.collection(Customer, Order)
    items = await view.query(customer_id="c1").all()
    assert sorted(type(item).__name__ for item in items) == ["Customer", "Order", "Order"]
    by_raw_key = await view.query(pk="CUSTOMER#c1").all()
    assert len(by_raw_key) == 3
    everything = await view.scan().all()
    assert len(everything) == 3
    with pytest.raises(TypeError, match="share one table"):
        app.collection(Customer, dynamoxy.Model)  # type: ignore[arg-type]


async def test_collections_read_consistently(app: dynamoxy.Client) -> None:
    await app.repository(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.repository(Order).create(order("o1", 1))
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
    await app.repository(Customer).create(Customer(customer_id="c1", name="Ada"))
    await app.repository(Order).create(order("o1", 1))
    assert await app.repository(Order).count() == 1
    assert await app.repository(Customer).count() == 1
    raw = await app.table("app").scan().all()
    assert sorted(item["_type"] for item in raw) == ["Customer", "Order"]


async def test_where_replaces_the_prefix_of_templated_sort_keys(app: dynamoxy.Client) -> None:
    await app.repository(Customer).create(Customer(customer_id="c1", name="Ada"))
    orders = app.repository(Order)
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
    orders = app.repository(Order)
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
