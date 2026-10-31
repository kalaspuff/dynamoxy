"""Transactions and transactional reads on every backend."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key
from tests.models import Article, Customer, Event, Order, Player, User

ANNOUNCED: list[str] = []


class Wallet(Model, table="wallets"):
    """Versioned, without timestamps: a transaction planned again sends the same request."""

    wallet_id: Attr[str] = partition_key()
    balance: Attr[int] = field(default=0)
    version: Attr[str] = dynamoxy.version()


class Note(Model, table="notes"):
    """A model whose after_save hook fails for one instance."""

    note_id: Attr[str] = partition_key()
    version: Attr[str] = dynamoxy.version()

    @dynamoxy.after_save
    def announce(self) -> None:
        ANNOUNCED.append(self.note_id)
        if self.note_id == "fails":
            raise RuntimeError("the hook failed")


@pytest.fixture
async def client_with_tables(client: dynamoxy.Client) -> dynamoxy.Client:
    await client.create_tables(User, Event, Customer, Order)
    return client


def order(order_id: str, total: str = "10") -> Order:
    return Order(customer_id="c1", order_id=order_id, placed_at=datetime(2026, 1, 1, tzinfo=UTC), total=Decimal(total))


async def test_a_transaction_planned_again_with_its_token_is_the_same_request(
    client_with_tables: dynamoxy.Client,
) -> None:
    # The version tokens of a transaction with an idempotency token derive from it: planned again
    # (an application's retry), it sends the same request, which DynamoDB applies once.
    client = client_with_tables
    await client.create_tables(Wallet)
    wallets = client.model(Wallet)
    await wallets.create(Wallet(wallet_id="w1"))
    token = "transfer-0001"
    for _ in range(2):
        async with client.transaction(token=token) as tx:
            tx.update(Wallet.key("w1"), Wallet.balance.add(10))
    stored = await wallets.get_or_raise("w1")
    assert stored.balance == 10, "applied once"
    async with client.transaction(token="transfer-0002") as tx:
        tx.update(Wallet.key("w1"), Wallet.balance.add(10))
    assert (await wallets.get_or_raise("w1")).version != stored.version, "another token, another version"
    # Models with timestamps differ between plans anyway (see the transactions guide).


async def test_commit_applies_everything(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    user = await client.model(User).create(User(user_id="u1", email="a@example.com", name="Ada"))
    async with client.transaction() as tx:
        tx.create(Customer(customer_id="c1", name="Ada"))
        tx.create(order("o1"))
        tx.update(user, User.visits.add(5), User.tags.add({"vip"}))
        tx.put_item("events", {"stream": "s", "seq": 1, "kind": "raw"})
    assert len(tx) == 4
    assert await client.model(Customer).get(customer_id="c1") is not None
    assert (user.visits, user.tags) == (5, {"vip"}), "instances are refreshed"
    assert user.version == (await client.model(User).get_or_raise("u1")).version
    assert user.is_loaded
    assert (await client.model(User).get_or_raise("u1")).visits == 5
    assert await client.table("events").get({"stream": "s", "seq": 1}) == {"stream": "s", "seq": 1, "kind": "raw"}


async def test_instances_are_refreshed_with_list_elements_set_in_index_order(
    client: dynamoxy.Client, backend: str
) -> None:
    # A transaction returns no item, so the update is evaluated locally, like DynamoDB: list
    # elements are set in order of their index, and those past the end append in that order.
    await client.create_tables(Player)
    players = client.model(Player)
    player = await players.create(Player(player_id="p1", name="Ada", history=["x", "y", "z"]))
    async with client.transaction() as tx:
        tx.update(player, Player.history[4].set("b"), Player.history[3].set("a"))
    assert player.history == ["x", "y", "z", "a", "b"]
    if backend == "memory":  # MiniStack sets them in the order of the actions and stores ["x", "y", "z", "a"]
        assert (await players.get_or_raise("p1")).history == player.history


async def test_canceled_transactions_explain_every_operation(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    await client.model(Order).create(order("o1"))
    await client.model(Order).create(order("o3"))
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:  # noqa: PT012 - the block commits
        async with client.transaction() as tx:
            tx.create(order("o2"))
            tx.create(order("o1"))
            tx.check(Order, Order.key(customer_id="c1", order_id="o3"), Order.total > 100)
    error = caught.value
    reasons = dynamoxy.cancellation_reasons(error)
    assert error.cancellation_reasons == reasons
    assert [reason.code for reason in reasons] == ["None", "ConditionalCheckFailed", "ConditionalCheckFailed"]
    assert [reason.operation for reason in reasons] == ["create Order", "create Order", "check Order"]
    assert isinstance(reasons[1].error, dynamoxy.ItemAlreadyExistsError)
    assert reasons[1].item is not None
    assert reasons[1].item["order_id"] == "o1"
    assert reasons[0].failed is False
    assert [type(specific) for specific in error.errors] == [dynamoxy.ItemAlreadyExistsError]
    assert await client.model(Order).get(customer_id="c1", order_id="o2") is None, "nothing was written"


async def test_unique_and_version_failures_in_transactions(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    users = client.model(User)
    ada = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
    stale = await users.get_or_raise("u1")
    await users.update(ada, User.visits.add(1))
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:  # noqa: PT012 - the block commits
        async with client.transaction() as tx:
            tx.create(User(user_id="u2", email="ada@example.com", name="Bob"))
            tx.save(stale)
    errors = [reason.error for reason in dynamoxy.cancellation_reasons(caught.value) if reason.error is not None]
    assert {type(error) for error in errors} == {dynamoxy.UniqueConstraintError, dynamoxy.VersionConflictError}


async def test_delete_and_dict_level_operations(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    events = client.model(Event)
    for seq, kind in enumerate("abc", start=1):
        await events.put(Event(stream="s", seq=seq, kind=kind))
    async with client.transaction() as tx:
        tx.delete(Event.key("s", 1), model=Event)
        tx.update_item("events", {"stream": "s", "seq": 2}, dynamoxy.F("kind").set("c"))
        tx.check_item("events", {"stream": "s", "seq": 3}, dynamoxy.F("kind") == "c")
        tx.delete_item("events", {"stream": "s", "seq": 4})
    assert await events.get("s", 1) is None
    assert (await events.get_or_raise("s", 2)).kind == "c"


async def test_updates_by_key_of_inherited_fields(client: dynamoxy.Client) -> None:
    # Article.seen_at is declared by the abstract Timestamped; the update is one of an Article.
    await client.create_tables(Article)
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    key = Article.key("a1", moment)
    await client.model(Article).create(Article(article_id="a1", published_at=moment))
    async with client.transaction() as tx:
        tx.update(key, Article.seen_at.set(moment))
    raw = await client.table("articles").get(key)
    assert raw is not None
    assert raw["seen_at"] == "2026-01-01T00:00:00.000000Z", "in Article's datetime_format"


async def test_tokens_make_retries_idempotent(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    events = client.model(Event)
    await events.put(Event(stream="t", seq=1, kind="k", payload={}))
    token = uuid.uuid4().hex  # tokens are account-wide: unique per logical operation, at most 36 characters
    for _ in range(2):
        transaction = client.transaction(token=token)
        transaction.update(Event.key("t", 1), Event.payload["n"].set_if_not_exists(0), model=Event)
        await transaction.commit()
    with pytest.raises(dynamoxy.IdempotentParameterMismatchError):  # noqa: PT012 - the block commits
        transaction = client.transaction(token=token)
        transaction.put_item("events", {"stream": "t", "seq": 2, "kind": "other"})
        await transaction.commit()


async def test_transactions_validate_before_writing(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    transaction = client.transaction()
    for index in range(101):
        transaction.put_item("events", {"stream": "big", "seq": index, "kind": "k"})
    with pytest.raises(dynamoxy.ValidationError, match="DynamoDB allows 100"):
        await transaction.commit()
    with pytest.raises(RuntimeError, match="committed already"):
        await transaction.commit()
    with pytest.raises(TypeError, match="pass model="):
        client.transaction().delete({"stream": "x", "seq": 1})
    with pytest.raises(ValueError, match="1 to 36 characters"):
        client.transaction(token="x" * 37)
    with pytest.raises(ValueError, match="1 to 36 characters"):
        await client.execute_transaction([], token="")


async def test_aborted_blocks_do_not_commit(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    with pytest.raises(RuntimeError, match="abort"):  # noqa: PT012 - the block commits
        async with client.transaction() as tx:
            tx.put_item("events", {"stream": "x", "seq": 1, "kind": "k"})
            raise RuntimeError("abort")
    assert await client.table("events").get({"stream": "x", "seq": 1}) is None


async def test_transact_get_reads_a_snapshot(client_with_tables: dynamoxy.Client) -> None:
    client = client_with_tables
    await client.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    await client.model(Order).create(order("o1", "42"))
    customer, found, missing = await client.transact_get(
        dynamoxy.Get(Customer, customer_id="c1"),
        dynamoxy.Get(Order, customer_id="c1", order_id="o1"),
        dynamoxy.Get(Order, customer_id="c1", order_id="nope"),
    )
    assert isinstance(customer, Customer)
    assert customer.name == "Ada"
    assert isinstance(found, Order)
    assert found.total == Decimal(42)
    assert missing is None
    assert await client.transact_get() == []


async def test_failing_hooks_after_a_commit_leave_every_instance_final(client: dynamoxy.Client) -> None:
    await client.create_tables(Note)
    ANNOUNCED.clear()
    failing, other = Note(note_id="fails"), Note(note_id="other")
    tx = client.transaction().create(failing).create(other)
    with pytest.raises(RuntimeError, match="the hook failed"):
        await tx.commit()
    assert ANNOUNCED == ["fails", "other"], "every hook ran"
    assert None not in (failing.version, other.version)
    assert other.is_loaded
    committed = other.version
    await other.save()
    assert other.version not in (None, committed)


class Cart(Model, table="carts"):
    """Unversioned: a transaction's update is refreshed from the item as last read."""

    cart_id: Attr[str] = partition_key()
    items: Attr[list[int] | None] = field(default=None)


async def test_commits_refresh_instances_whose_item_changed_since_the_read(client: dynamoxy.Client) -> None:
    await client.create_tables(Cart)
    carts = client.model(Cart)
    await carts.create(Cart(cart_id="c1"))
    loaded = await carts.get_or_raise("c1")
    await carts.update(Cart.key("c1"), Cart.items.set([1]))  # another writer
    # The append applies to the stored item, not to the one read (which has no list): the
    # commit succeeded, and the instance is refreshed from the item as stored now.
    async with client.transaction() as tx:
        tx.update(loaded, Cart.items.append([2]))
    assert loaded.items == [1, 2]
    assert loaded.is_loaded
    assert (await carts.get_or_raise("c1")).items == [1, 2]


async def test_commits_that_cannot_refresh_an_instance_do_not_fail(client: dynamoxy.Client) -> None:
    await client.create_tables(Cart)
    carts = client.model(Cart)
    await carts.create(Cart(cart_id="c1"))
    loaded = await carts.get_or_raise("c1")
    await carts.update(Cart.key("c1"), Cart.items.set([1]))

    async def delete_after_the_commit(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        response = await call_next(request)
        if request.operation == "TransactWriteItems":
            remove()
            await carts.delete("c1")
        return response

    remove = client.use(delete_after_the_commit)
    await client.transaction().update(loaded, Cart.items.append([2])).commit()  # applied: no error
    assert not loaded.is_loaded, "the item is gone: the instance forgets the stored copy"


async def test_commits_whose_refresh_read_fails_do_not_fail(client: dynamoxy.Client) -> None:
    await client.create_tables(Cart)
    carts = client.model(Cart)
    await carts.create(Cart(cart_id="c1"))
    loaded = await carts.get_or_raise("c1")
    await carts.update(Cart.key("c1"), Cart.items.set([1]))
    committed = False

    async def fail_reads_after_the_commit(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        nonlocal committed
        if committed and request.operation == "GetItem":
            raise dynamoxy.TransportError("the connection dropped")
        response = await call_next(request)
        committed = committed or request.operation == "TransactWriteItems"
        return response

    remove = client.use(fail_reads_after_the_commit)
    await client.transaction().update(loaded, Cart.items.append([2])).commit()  # applied: no error
    remove()
    assert not loaded.is_loaded, "the item could not be read: the instance forgets the stored copy"
    assert (await carts.get_or_raise("c1")).items == [1, 2]
