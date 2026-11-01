"""Persistence methods of models (``Model.create``) and instances (``save``, ``delete``, ...) on every backend."""

from __future__ import annotations

import copy
import pickle
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, assert_type, override

import pydantic
import pytest

import dynamoxy
from dynamoxy import Attr, AttributeName, F, PartitionKey, field, partition_key, testing
from tests.conftest import ClientFactory
from tests.models import Account, ContactRecord, Customer, Event, Order, OrderStatus, Player, User

if TYPE_CHECKING:
    from collections.abc import Mapping


class ProfileV1(dynamoxy.Model, table="profiles_v"):
    """An older version of a model: it does not know the field ``ProfileV2.nickname``."""

    profile_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    version: Attr[str] = dynamoxy.version()


class ProfileV2(dynamoxy.Model, table="profiles_v"):
    profile_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    nickname: Attr[str | None] = field(default=None)
    version: Attr[str] = dynamoxy.version()


class Street(dynamoxy.Document):
    """A document of an older version: a newer one stores ``Street.number`` too."""

    name: Attr[str] = field(default="")


class Postal(dynamoxy.Document):
    city: Attr[str] = field(default="")
    street: Attr[Street | None] = field(default=None)


class PostalModel(pydantic.BaseModel):
    city: str = ""


class Mailbox(dynamoxy.Model, table="mailboxes"):
    mailbox_id: Attr[str] = partition_key()
    postal: Attr[Postal | None] = field(default=None)
    history: Attr[list[Postal]] = field(default_factory=list)


class MailboxRecord(dynamoxy.PydanticModel, table="mailbox_records"):
    mailbox_id: Annotated[str, PartitionKey()]
    postal: PostalModel | None = None


@pytest.fixture
async def players(client: dynamoxy.Client) -> dynamoxy.Repository[Player]:
    await client.create_tables(Player)
    return client.model(Player)


async def test_save_writes_the_changes_of_a_loaded_instance(players: dynamoxy.Repository[Player]) -> None:
    await players.create(Player(player_id="p1", name="Ada"))
    player = await players.get_or_raise("p1")
    loaded_version = player.version
    player.name = "Ada Lovelace"
    player.score = 7
    assert await player.save() is player
    assert player.rank == "SCORE#00000007", "derived fields are maintained"
    assert player.version not in (None, loaded_version), "a new version token"
    assert await players.get_or_raise("p1") == player


async def test_instances_are_bound_once_a_repository_wrote_them(players: dynamoxy.Repository[Player]) -> None:
    player = Player(player_id="p1", name="Ada")
    with dynamoxy.collect() as usage, pytest.raises(RuntimeError, match=r"Player\.save\(\): .*repository"):
        await player.save()
    assert usage.calls == 0
    await players.save(player)  # creates it (saving a new instance of a versioned model requires absence)
    player.name = "Bo"
    await player.save()
    assert (await players.get_or_raise("p1")).name == "Bo"


async def test_models_create_items_from_field_values(client: dynamoxy.Client) -> None:
    await client.create_tables(User)
    with dynamoxy.collect() as usage:
        user = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
    assert_type(user, User)
    assert usage.calls == 1, "the item and its unique guard, in one transaction"
    assert user.is_loaded
    assert user.visits == 0
    assert re.fullmatch(r"[0-9a-f]{16}", user.version or ""), "a random version token"
    assert user.created_at is not None
    assert await client.model(User).get_or_raise("u1") == user
    created_version = user.version
    await user.increment(User.visits)  # bound to client.model(User)
    assert user.visits == 1
    assert user.version != created_version
    with pytest.raises(dynamoxy.ItemAlreadyExistsError):
        await User.create(client, user_id="u1", email="bo@example.com", name="Bo")
    with pytest.raises(dynamoxy.UniqueConstraintError) as caught:
        await User.create(client, user_id="u2", email="ada@example.com", name="Bo")
    assert caught.value.field == "email"
    assert await client.model(User).count() == 1


async def test_create_refuses_bad_arguments_before_any_io(client: dynamoxy.Client) -> None:
    class Base(dynamoxy.Model, abstract=True):
        pk: Attr[str] = partition_key()

    await client.create_tables(User)
    user = User(user_id="u1", email="ada@example.com", name="Ada")
    with dynamoxy.collect() as usage:
        with pytest.raises(dynamoxy.ValidationError, match=r"User is invalid: name"):
            await User.create(client, user_id="u1", email="ada@example.com", name="")
        with pytest.raises(TypeError, match="unexpected keyword argument 'nmae'"):
            await User.create(client, user_id="u1", email="ada@example.com", nmae="Ada")  # type: ignore[call-arg]
        with pytest.raises(TypeError, match=r"User\.version is set automatically"):
            await User.create(client, user_id="u1", email="ada@example.com", name="Ada", version=3)  # type: ignore[call-arg]
        with pytest.raises(TypeError, match=r"User\.create\(\) takes a dynamoxy\.Client first, got User: "):
            await User.create(user)  # type: ignore[arg-type,call-arg]
        with pytest.raises(
            TypeError,
            match=r"takes field values as keyword arguments, got 1 positional: .*`await instance\.create\(client\)`",
        ):
            await User.create(client, user)  # type: ignore[call-arg]
        with pytest.raises(TypeError, match="Base is not stored in a table"):
            await Base.create(client, pk="b1")
    assert usage.calls == 0
    assert await client.model(User).count() == 0


async def test_instances_create_themselves(client: dynamoxy.Client) -> None:
    await client.create_tables(User)
    ada = User(user_id="u1", email="ada@example.com", name="Ada")
    assert not ada.is_loaded
    with dynamoxy.collect() as usage:
        created = await ada.create(client)
    assert_type(created, User)
    assert created is ada, "the instance itself is written, stored and bound"
    assert usage.calls == 1, "the item and its unique guard, in one transaction"
    assert ada.is_loaded
    assert re.fullmatch(r"[0-9a-f]{16}", ada.version or "")
    assert ada.created_at is not None
    assert await client.model(User).get_or_raise("u1") == ada
    await ada.increment(User.visits)  # bound to client.model(User)
    assert (await client.model(User).get_or_raise("u1")).visits == 1
    with pytest.raises(dynamoxy.ItemAlreadyExistsError):
        await User(user_id="u1", email="bo@example.com", name="Bo").create(client)
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await User(user_id="u2", email="ada@example.com", name="Bo").create(client)
    with pytest.raises(dynamoxy.ItemAlreadyExistsError):
        await ada.create(client)  # create never replaces, also for a loaded instance
    assert await client.model(User).count() == 1


async def test_instance_create_refuses_bad_arguments_before_any_io(client: dynamoxy.Client) -> None:
    class Base(dynamoxy.Model, abstract=True):
        pk: Attr[str] = partition_key()

    await client.create_tables(User)
    user = User(user_id="u1", email="ada@example.com", name="Ada")
    with dynamoxy.collect() as usage:
        with pytest.raises(
            TypeError, match=r"User\.create\(\) of an instance takes a dynamoxy\.Client, got str: .*instance\.create"
        ):
            await user.create("db")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await user.create()  # type: ignore[call-overload]
        with pytest.raises(TypeError):
            await user.create(client, name="Bo")  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="Base is not stored in a table"):
            await Base(pk="b1").create(client)
    assert usage.calls == 0
    assert not user.is_loaded


async def test_pydantic_instances_create_themselves(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    account = Account(account_id="a1", owner="Ada", handle="ada")
    assert await account.create(client) is account
    assert account.version is not None
    assert account.is_loaded
    await account.increment(dynamoxy.attr(Account.balance), 5)
    assert (await Account.get(client, "a1")) == account
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await Account(account_id="a2", owner="Bo", handle="ada").create(client)


async def test_create_runs_save_hooks(client: dynamoxy.Client) -> None:
    calls: list[str] = []

    class Note(dynamoxy.Model, table="notes"):
        note_id: Attr[str] = partition_key()
        text: Attr[str]

        @dynamoxy.before_save
        def strip(self) -> None:
            calls.append(f"before_save {self.text!r}")
            self.text = self.text.strip()

        @dynamoxy.after_save
        def saved(self) -> None:
            calls.append("after_save")

    @client.hook("before_save", Note)
    def audit(note: Note, context: dynamoxy.HookContext) -> None:
        calls.append(f"client before_save ({context.operation})")

    await client.create_tables(Note)
    note = await Note.create(client, note_id="n1", text=" hi ")
    assert note.text == "hi"
    assert (await client.model(Note).get_or_raise("n1")).text == "hi"
    assert calls == ["before_save ' hi '", "client before_save (create)", "after_save"]


async def test_create_takes_the_client_first_so_a_field_may_be_named_client(client: dynamoxy.Client) -> None:
    class Invoice(dynamoxy.Model, table="invoices"):
        invoice_id: Attr[str] = partition_key()
        client: Attr[str]

    await client.create_tables(Invoice)
    invoice = await Invoice.create(client, invoice_id="i1", client="ACME")
    assert (await client.model(Invoice).get_or_raise("i1")).client == invoice.client == "ACME"


async def test_pydantic_models_create_items(client: dynamoxy.Client) -> None:
    await client.create_tables(Account, ContactRecord)
    account = await Account.create(client, account_id="a1", owner="Ada", handle="ada")
    assert_type(account, Account)
    assert account.version is not None
    assert account.is_loaded
    await account.increment(dynamoxy.attr(Account.balance), 5)
    assert (await client.model(Account).get_or_raise("a1")).balance == 5
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await Account.create(client, account_id="a2", owner="Bo", handle="ada")
    contact = await ContactRecord.create(client, contact_id="c1", email="ada@example.com", name=" Ada ")
    assert contact.name == "Ada", "pydantic validators ran"
    with dynamoxy.collect() as usage, pytest.raises(pydantic.ValidationError, match="name"):
        await ContactRecord.create(client, contact_id="c2", email="bo@example.com", name="")
    assert usage.calls == 0


async def test_models_read_items_by_key_and_by_partition(client: dynamoxy.Client) -> None:
    await client.create_tables(Order, User)
    placed_at = datetime(2026, 10, 30, tzinfo=UTC)
    orders = [
        await Order.create(client, customer_id="c1", order_id=f"o{n}", placed_at=placed_at, total=Decimal(n))
        for n in range(3)
    ]
    found = await Order.get(client, customer_id="c1", order_id="o1")
    assert_type(found, Order | None)
    assert found is not None
    assert found == orders[1]
    assert found.is_loaded
    assert await Order.get(client, customer_id="c1", order_id="o9") is None
    assert await Order.get(client, customer_id="c1", order_id="o2", consistent=True) == orders[2]
    query = Order.query(client, customer_id="c1")
    assert_type(query, dynamoxy.Query[Order])
    assert [order.order_id for order in await query.descending().all()] == ["o2", "o1", "o0"]
    assert await Order.query(client, customer_id="c1", consistent=True).count() == 3
    await found.update(Order.status.set(OrderStatus.PAID))  # bound to client.model(Order)
    paid = await Order.query(client, index=Order.by_status, status=OrderStatus.PAID).all()
    assert [order.order_id for order in paid] == ["o1"]

    ada = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
    assert await User.get(client, "u1") == ada
    by_email = await User.query(client, index=User.by_email, email="ada@example.com").first()
    assert by_email is not None
    assert by_email.user_id == "u1"
    by_email.name = "Ada Lovelace"
    await by_email.save()  # the version check of a loaded instance
    assert (await User.get(client, "u1", consistent=True)).name == "Ada Lovelace"  # type: ignore[union-attr]
    assert client.model(User) is client.model(User), "one repository per model and client"


async def test_reads_from_the_model_refuse_bad_arguments_before_any_io(client: dynamoxy.Client) -> None:
    class Base(dynamoxy.Model, abstract=True):
        pk: Attr[str] = partition_key()

    await client.create_tables(User)
    with dynamoxy.collect() as usage:
        with pytest.raises(
            TypeError, match=r"User\.get\(\) takes a dynamoxy\.Client first, got str: .*User\.get\(client"
        ):
            await User.get("u1")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"User\.query\(\) takes a dynamoxy\.Client first, got str"):
            User.query("u1")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=r"User: the key needs user_id=\.\.\."):
            await User.get(client)
        with pytest.raises(ValueError, match=r"User: the key needs user_id=\.\.\."):
            User.query(client)
        with pytest.raises(ValueError, match="User has no index 'by_nothing'"):
            User.query(client, index="by_nothing", email="ada@example.com")
        with pytest.raises(TypeError, match="Base is not stored in a table"):
            await Base.get(client, "b1")
        with pytest.raises(TypeError, match="Base is not stored in a table"):
            Base.query(client, pk="b1")
    assert usage.calls == 0


async def test_models_scan_their_items(client: dynamoxy.Client) -> None:
    await client.create_tables(Order, User, Account)
    placed_at = datetime(2026, 10, 30, tzinfo=UTC)
    for n in range(3):
        await Order.create(client, customer_id=f"c{n}", order_id="o1", placed_at=placed_at, total=Decimal(n))
    scan = Order.scan(client)
    assert_type(scan, dynamoxy.Scan[Order])
    orders = await scan.all()
    assert sorted(order.customer_id for order in orders) == ["c0", "c1", "c2"]
    assert all(order.is_loaded for order in orders)
    big = await Order.scan(client, consistent=True).filter(Order.total >= 1).all()
    assert sorted(order.customer_id for order in big) == ["c1", "c2"]
    await big[0].update(Order.status.set(OrderStatus.PAID))  # bound to client.model(Order)
    paid = await Order.scan(client, index=Order.by_status).all()
    assert [order.customer_id for order in paid if order.status == OrderStatus.PAID] == [big[0].customer_id]
    assert await Order.scan(client).count() == 3

    await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
    assert [user.user_id for user in await User.scan(client).all()] == ["u1"], "guard items are not users"
    await Account.create(client, account_id="a1", owner="Ada", handle="ada")
    accounts = await Account.scan(client).all()
    assert_type(accounts, list[Account])
    assert [account.account_id for account in accounts] == ["a1"]


async def test_scans_from_the_model_refuse_bad_arguments_before_any_io(client: dynamoxy.Client) -> None:
    class Base(dynamoxy.Model, abstract=True):
        pk: Attr[str] = partition_key()

    with dynamoxy.collect() as usage:
        with pytest.raises(
            TypeError, match=r"User\.scan\(\) takes a dynamoxy\.Client first, got str: `User\.scan\(client\)`"
        ):
            User.scan("u1")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="User has no index 'by_nothing'"):
            User.scan(client, index="by_nothing")
        with pytest.raises(TypeError, match="Base is not stored in a table"):
            Base.scan(client)
    assert usage.calls == 0


async def test_pydantic_models_read_items(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    account = await Account.create(client, account_id="a1", owner="Ada", handle="ada")
    found = await Account.get(client, "a1")
    assert_type(found, Account | None)
    assert found == account
    accounts = await Account.query(client, account_id="a1").all()
    assert_type(accounts, list[Account])
    assert accounts == [account]
    assert await Account.get(client, "a2") is None


async def test_model_api_names_can_still_be_stored_attributes(client: dynamoxy.Client) -> None:
    # `query` and `get` are model methods, so fields take other Python names; the stored names stay.
    class Search(dynamoxy.Model, table="searches"):
        search_id: Attr[str] = partition_key()
        search_query: Attr[str] = field(attribute_name="query")
        fetched: Attr[int] = field(default=0, attribute_name="get")

        by_query = dynamoxy.GlobalIndex(partition_key=search_query)

    class SearchRecord(dynamoxy.PydanticModel, table="search_records"):
        record_id: Annotated[str, PartitionKey()]
        search_query: Annotated[str, AttributeName("query")]

    await client.create_tables(Search, SearchRecord)
    search = await Search.create(client, search_id="s1", search_query="dynamodb")
    await search.increment(Search.fetched)
    stored = await client.table("searches").get({"search_id": "s1"})
    assert stored is not None
    assert {name: value for name, value in stored.items() if name != "_wid"} == {
        "search_id": "s1",
        "query": "dynamodb",
        "get": 1,
    }
    found = await Search.query(client, index=Search.by_query, search_query="dynamodb").first()
    assert found is not None
    assert (found.search_id, found.fetched) == ("s1", 1)
    filtered = await Search.query(client, search_id="s1").filter(Search.search_query.begins_with("dyn")).all()
    assert filtered == [found]
    await found.update(Search.search_query.set("rust"))
    assert (await Search.get(client, "s1")).search_query == "rust"  # type: ignore[union-attr]
    await SearchRecord.create(client, record_id="r1", search_query="dynamodb")
    assert (await client.table("search_records").get({"record_id": "r1"}) or {}).get("query") == "dynamodb"
    assert (await SearchRecord.get(client, "r1")).search_query == "dynamodb"  # type: ignore[union-attr]


async def test_save_checks_versions_and_conditions(players: dynamoxy.Repository[Player]) -> None:
    player = await players.create(Player(player_id="p1", name="Ada"))
    stale = await players.get_or_raise("p1")
    player.name = "Bo"
    await player.save()
    stale.name = "Stale"
    with pytest.raises(dynamoxy.VersionConflictError):
        await stale.save()
    await stale.refresh()  # reload, re-apply the change, save again
    assert (stale.name, stale.version) == ("Bo", player.version)
    stale.name = "Cy"
    await stale.save(condition=Player.score == 0)
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await stale.save(condition=Player.score > 0)
    assert not isinstance(caught.value, dynamoxy.VersionConflictError)
    assert (await players.get_or_raise("p1")).name == "Cy"


async def test_items_stored_before_the_model_had_a_version_can_be_saved(
    client: dynamoxy.Client, players: dynamoxy.Repository[Player]
) -> None:
    # As stored by the application before it added `version: Attr[str] = dynamoxy.version()`.
    legacy = {"player_id": "p1", "name": "Ada", "board": "global", "score": 0, "rank": "SCORE#00000000"}
    await client.table("players").put(legacy)
    player = await players.get_or_raise("p1")
    stale = await players.get_or_raise("p1")
    assert (player.version, player.is_loaded) == (None, True)
    player.name = "Ada L."
    await player.save()  # the stored item must still have no version: it gets its first one
    assert player.version is not None
    assert (await players.get_or_raise("p1")).version == player.version
    stale.name = "Stale"
    with pytest.raises(dynamoxy.VersionConflictError, match=f"stored version {player.version}, expected no version"):
        await stale.save()
    await client.table("players").put({**legacy, "player_id": "p2"})
    gone = await players.get_or_raise("p2")
    await client.table("players").delete({"player_id": "p2"})
    with pytest.raises(dynamoxy.VersionConflictError, match="was deleted after it was loaded"):
        await gone.save()
    assert await players.get("p2") is None, "not created again"
    await client.table("players").put({**legacy, "player_id": "p3"})
    in_transaction = await players.get_or_raise("p3")
    async with client.transaction() as tx:
        tx.save(in_transaction)
    assert in_transaction.version is not None
    assert (await players.get_or_raise("p3")).version == in_transaction.version


async def test_delete_through_the_instance(client: dynamoxy.Client, players: dynamoxy.Repository[Player]) -> None:
    player = await players.create(Player(player_id="p1", name="Ada"))
    deleted = await player.delete()
    assert deleted is not None
    assert deleted is not player, "the item as it was stored"
    assert deleted.name == "Ada"
    assert not player.is_loaded
    assert await players.get("p1") is None
    await client.create_tables(Event)
    events = client.model(Event)
    event = await events.create(Event(stream="s", seq=1, kind="k"))
    with pytest.raises(dynamoxy.ConditionalCheckFailedError):
        await event.delete(condition=Event.kind == "other")
    assert await event.delete(condition=Event.kind == "k") is not None
    assert await event.delete() is None, "nothing is stored any more"
    await event.save()  # still bound: writes it again
    assert await events.get("s", 1) == event


async def test_atomic_changes_through_the_instance(players: dynamoxy.Repository[Player]) -> None:
    player = await players.create(Player(player_id="p1", name="Ada", badges={"rookie"}))
    versions = {player.version}

    async def record_version(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        response = await call_next(request)
        versions.add(player.version)
        return response

    remove = players.client.use(record_version)
    assert await player.increment(Player.score) is player
    await player.increment(Player.score, 9)
    await player.decrement(Player.score, 2)
    await player.add(Player.score, 5)
    await player.add(Player.coins, Decimal("2.5"))
    await player.decrement(Player.coins, Decimal("0.5"))
    await player.add(Player.badges, "first-win")  # one element
    await player.add(Player.badges, {"sharp", "fast"})  # or several
    await player.add(Player.history, "won")  # lists: appended
    await player.add(Player.history, "lost")
    await player.increment(Player.stats["wins"])  # nested: a missing member counts as 0
    await player.increment(Player.stats["wins"], 2)
    await player.decrement(Player.stats["streak"])
    await player.update(Player.name.set("Ada L."), Player.stats["draws"].set(4))
    assert (player.score, player.coins, player.name) == (13, Decimal(2), "Ada L.")
    assert player.badges == {"rookie", "first-win", "sharp", "fast"}
    assert player.history == ["won", "lost"]
    assert player.stats == {"wins": 3, "streak": -1, "draws": 4}
    remove()
    versions.add(player.version)
    assert len(versions) == 15, "every change was one versioned write, with a new version token"
    assert player.rank == "SCORE#00000013", "derived attributes follow the new value"
    assert await players.get_or_raise("p1") == player, "the instance holds what DynamoDB stored"


async def test_lists_are_created_and_derived_index_keys_follow(
    client: dynamoxy.Client, players: dynamoxy.Repository[Player]
) -> None:
    await client.table("players").put(
        {"player_id": "p1", "name": "Ada", "board": "global", "score": 0, "version": "v1"}
    )
    legacy = await players.get_or_raise("p1")  # stored without history (an older item)
    await legacy.add(Player.history, "joined")
    assert legacy.history == ["joined"]
    bo = await players.create(Player(player_id="p2", name="Bo", score=5))
    await legacy.increment(Player.score, 10)
    await bo.increment(Player.score, 7)
    leaders = await players.query(index=Player.by_rank, board="global").descending().all()
    assert [(player.player_id, player.rank) for player in leaders] == [
        ("p2", "SCORE#00000012"),
        ("p1", "SCORE#00000010"),
    ]


async def test_atomic_changes_check_versions_and_fail_before_io(client: dynamoxy.Client) -> None:
    await client.create_tables(Player, Event)
    players = client.model(Player)
    player = await players.create(Player(player_id="p1", name="Ada"))
    stale = await players.get_or_raise("p1")
    await player.increment(Player.score)
    with pytest.raises(dynamoxy.VersionConflictError):
        await stale.increment(Player.score)
    with pytest.raises(dynamoxy.ConditionalCheckFailedError):
        await player.decrement(Player.coins, 5, condition=Player.coins >= 5)
    event = await client.model(Event).create(Event(stream="s", seq=1, kind="k"))
    with dynamoxy.collect() as usage:
        with pytest.raises(TypeError, match=r"Player\.name holds str values: increment\(\) needs a number"):
            await player.increment(Player.name)  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(TypeError, match=r"Player\.stats holds dict\[str, int\] values: add\(\) works on numbers"):
            await player.add(Player.stats, {"x": 1})  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(dynamoxy.ValidationError, match=r"Player\.score: expected int"):
            await player.increment(Player.score, 0.5)  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(
            dynamoxy.ValidationError, match=r"Player\.history: add\(\) appends one element: expected str"
        ):
            await player.add(Player.history, ["a", "b"])  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(dynamoxy.ValidationError, match=r"Player\.badges: add\(\) takes one element or a set"):
            await player.add(Player.badges, ["a", "b"])  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(dynamoxy.ValidationError, match=r"Player\.badges: expected str"):
            await player.add(Player.badges, 3)  # pyrefly: ignore[no-matching-overload]
        with pytest.raises(ValueError, match="at least one element"):
            await player.add(Player.badges, set())
        with pytest.raises(TypeError, match=r"Event\.seq is not a field of Player"):
            await player.increment(Event.seq)
        with pytest.raises(ValueError, match="key or derived attribute"):
            await event.increment(Event.seq)
        with pytest.raises(ValueError, match="at least one action"):
            await player.update()
    assert usage.calls == 0, "refused before any I/O"


class Counted(dynamoxy.Model, abstract=True):
    """A field that the sibling models ``Page`` and ``Post`` inherit."""

    views: Attr[int] = field(default=0)


class Page(Counted, table="pages"):
    page_id: Attr[str] = partition_key()


class Post(Counted, table="posts"):
    post_id: Attr[str] = partition_key()


class CountedRecord(dynamoxy.PydanticModel, abstract=True):
    views: int


class PageRecord(CountedRecord, table="page_records"):
    page_id: Annotated[str, PartitionKey()]


class PostRecord(CountedRecord, table="post_records"):
    post_id: Annotated[str, PartitionKey()]


async def test_atomic_changes_take_the_paths_of_the_model_or_its_bases(client: dynamoxy.Client) -> None:
    await client.create_tables(Post, PostRecord)
    post = await Post.create(client, post_id="p1")
    record = await PostRecord.create(client, post_id="p1", views=0)
    await post.increment(Post.views)
    await post.increment(Counted.views)
    await record.increment(dynamoxy.attr(PostRecord.views))
    await record.increment(dynamoxy.attr(CountedRecord.views))
    assert (post.views, record.views) == (2, 2)
    # Each class has paths of its own, so a sibling's path is another model's, as for any model.
    with dynamoxy.collect() as usage:
        with pytest.raises(TypeError, match=r"^Page\.views is not a field of Post: use Post\.views$"):
            await post.increment(Page.views)
        with pytest.raises(TypeError, match=r"^PageRecord\.views is not a field of PostRecord: use PostRecord\.views$"):
            await record.increment(dynamoxy.attr(PageRecord.views))
    assert usage.calls == 0, "refused before any I/O"


async def test_reads_and_writes_bind_instances(client: dynamoxy.Client) -> None:
    await client.create_tables(Player, Customer, Order, Event)
    players = client.model(Player)
    await players.batch_put([Player(player_id=f"p{index}", name=f"P{index}") for index in range(3)])
    found: list[Any] = [
        await players.get_or_raise("p0"),
        await players.get("p1"),
        await players.query(player_id="p2").one(),
        *(await players.scan().all()),
        *(await players.batch_get(["p0", "p1"])),
        *(await client.transact_get(dynamoxy.Get(Player, "p2"))),
        await players.update(Player.key("p0"), Player.score.add(1)),
        await players.refresh(Player(player_id="p1", name="P1")),
    ]
    await client.model(Customer).create(Customer(customer_id="c1", name="Ada"))
    found += await client.collection(Customer, Order).query(customer_id="c1").all()
    for instance in found:
        await instance.refresh()  # needs the repository that read it
    created = Customer(customer_id="c2", name="New")
    async with client.transaction() as tx:
        tx.create(created)
    created.name = "Renamed"
    await created.save()
    event = Event(stream="s", seq=1, kind="k")
    async with client.batch_writer() as batch:
        await batch.put(event)
    await event.increment(Event.payload["n"])
    assert (await client.model(Event).get_or_raise("s", 1)).payload == {"n": 1}
    assert (await client.model(Customer).get_or_raise(customer_id="c2")).name == "Renamed"


class AuditedPlayers(dynamoxy.Repository[Player]):
    """A repository subclass whose overrides instance methods go through."""

    def __init__(self, client: dynamoxy.Client) -> None:
        super().__init__(client)
        self.log: list[str] = []

    @override
    async def save(self, item: Player, *, condition: dynamoxy.Condition | bool | None = None) -> Player:
        self.log.append(f"save {item.name}")
        return await super().save(item, condition=condition)

    @override
    async def update(
        self,
        target: Player | Mapping[str, Any],
        /,
        *actions: dynamoxy.UpdateAction,
        condition: dynamoxy.Condition | bool | None = None,
        upsert: bool = False,
    ) -> Player:
        self.log.append("update")
        return await super().update(target, *actions, condition=condition, upsert=upsert)

    @override
    async def delete(
        self, target: Player | Mapping[str, Any] | Any, /, *, condition: dynamoxy.Condition | bool | None = None
    ) -> Player | None:
        self.log.append("delete")
        return await super().delete(target, condition=condition)


async def test_instance_methods_use_the_repository_that_read_the_instance(client: dynamoxy.Client) -> None:
    await client.create_tables(Player)
    audited = AuditedPlayers(client)
    await client.model(Player).create(Player(player_id="p1", name="Ada"))
    player = await audited.get_or_raise("p1")
    player.name = "Bo"
    await player.save()
    await player.increment(Player.score)
    async with client.transaction() as tx:  # transactions keep the binding
        tx.update(player, Player.coins.add(1))
    player.name = "Cy"
    await player.save()
    await player.delete()
    assert audited.log == ["save Bo", "update", "save Cy", "delete"]


async def test_instances_stay_with_the_client_that_last_wrote_them() -> None:
    async with testing.memory_client() as primary, testing.memory_client() as replica:
        await primary.create_tables(Player)
        await replica.create_tables(Player)
        player = await primary.model(Player).create(Player(player_id="p1", name="Ada"))
        await replica.model(Player).put(player)  # copied to the other store: bound there now
        await player.increment(Player.score)
        assert (await replica.model(Player).get_or_raise("p1")).score == 1
        assert (await primary.model(Player).get_or_raise("p1")).score == 0


async def test_pydantic_instances(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    accounts = client.model(Account)
    await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    account = await accounts.get_or_raise("a1")
    account.owner = "Ada L."
    await account.save()
    balance = dynamoxy.attr(Account.balance)
    await account.increment(balance, Decimal("10.5"))
    await account.decrement(balance, 3)
    await account.update(dynamoxy.attr(Account.handle).set("ada2"))  # unique: guard items follow
    assert (account.owner, account.balance, account.handle) == ("Ada L.", Decimal("7.5"), "ada2")
    await accounts.create(Account(account_id="a2", owner="Bo", handle="ada"))  # the old handle is free again
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await accounts.create(Account(account_id="a3", owner="Cy", handle="ada2"))
    assert await account.delete() is not None
    assert await accounts.get("a1") is None
    with pytest.raises(RuntimeError, match=r"Account\.save\(\)"):
        await Account(account_id="a4", owner="Di", handle="di").save()


async def test_copies_share_the_binding_and_pickles_drop_it(players: dynamoxy.Repository[Player]) -> None:
    player = await players.create(Player(player_id="p1", name="Ada"))
    clone = copy.deepcopy(player)  # another view of the same stored item, through the same repository
    clone.name = "Clone"
    await clone.save()
    with pytest.raises(dynamoxy.VersionConflictError):
        await player.save()
    restored = pickle.loads(pickle.dumps(clone))
    assert restored == clone
    assert restored.is_loaded, "the stored copy survives; the repository (and its client) does not"
    with pytest.raises(RuntimeError, match="not bound to a repository"):
        await restored.save()
    await players.save(restored)
    await restored.increment(Player.score)
    with pytest.raises(RuntimeError, match="not bound to a repository"):
        await player.copy(player_id="p2").save()  # copy() makes a new, unsaved instance


async def test_lifecycle_hooks_run_around_instance_methods(client: dynamoxy.Client) -> None:
    calls: list[str] = []

    class Tracked(dynamoxy.Model, table="tracked"):
        pk: Attr[str] = partition_key()
        visits: Attr[int] = field(default=0)
        label: Attr[str] = field(default="")

        @dynamoxy.before_save
        def prepare(self) -> None:
            calls.append(f"before_save {self.label!r}")
            self.label = self.label.strip()

        @dynamoxy.after_save
        def saved(self) -> None:
            calls.append("after_save")

        @dynamoxy.before_update
        def checking(self) -> None:
            calls.append(f"before_update {self.visits}")

        @dynamoxy.after_update
        async def updated(self) -> None:
            calls.append(f"after_update {self.visits}")

        @dynamoxy.before_delete
        def removing(self) -> None:
            calls.append("before_delete")

        @dynamoxy.after_delete
        def removed(self) -> None:
            calls.append("after_delete")

    @client.hook("after_update", Tracked)
    def audit(item: Tracked, context: dynamoxy.HookContext) -> None:
        calls.append(f"client after_update ({context.operation})")

    await client.create_tables(Tracked)
    tracked = client.model(Tracked)
    item = await tracked.create(Tracked(pk="t", label=" x "))
    assert item.label == "x"
    await item.increment(Tracked.visits)
    item.label = "y"
    await item.save()
    async with client.transaction() as tx:
        tx.update(item, Tracked.visits.add(2))
    await tracked.update(Tracked.key("t"), Tracked.visits.add(1))  # by key: no instance, so no hooks
    await item.delete()
    assert calls == [
        "before_save ' x '",
        "after_save",
        "before_update 0",
        "after_update 1",
        "client after_update (update)",
        "before_save 'y'",
        "after_save",
        "before_update 1",
        "after_update 3",
        "client after_update (update)",
        "before_delete",
        "after_delete",
    ]


async def test_before_update_hooks_can_abort_updates(client: dynamoxy.Client) -> None:
    class Frozen(dynamoxy.Model, table="frozen"):
        pk: Attr[str] = partition_key()
        visits: Attr[int] = field(default=0)

        @dynamoxy.before_update
        def refuse(self) -> None:
            raise PermissionError("frozen")

    await client.create_tables(Frozen)
    frozen = client.model(Frozen)
    item = await frozen.create(Frozen(pk="f"))
    with dynamoxy.collect() as usage, pytest.raises(PermissionError):
        await item.increment(Frozen.visits)
    assert usage.calls == 0
    with pytest.raises(PermissionError):
        await frozen.update(item, Frozen.visits.add(1))
    assert (await frozen.get_or_raise("f")).visits == 0


async def test_atomic_changes_send_documented_update_actions() -> None:
    # DynamoDB documents ADD for top-level attributes; nested numbers use SET with if_not_exists.
    async with testing.memory_client() as client:
        await client.create_tables(Event)
        updates: list[str] = []

        async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
            if request.operation == "UpdateItem":
                names = request.params["expression_attribute_names"]
                updates.append(
                    re.sub(r"#a\d+", lambda match: names[match.group(0)], request.params["update_expression"])
                )
            return await call_next(request)

        client.use(record)
        event = await client.model(Event).create(Event(stream="s", seq=1, kind="k"))
        await event.increment(F("total"))  # not a field: a number by its value
        await event.increment(Event.payload["n"], 2)
        await event.add(Event.payload["tags"], {"a"})
        with pytest.raises(TypeError, match=r"Event\.payload\.note has no known type"):
            await event.add(Event.payload["note"], "text")
        assert updates == [  # each write also stores its id (_wid)
            "SET _wid = :v1 ADD total :v0",
            "SET payload.n = if_not_exists(payload.n, :v0) + :v1, _wid = :v2",
            "SET _wid = :v1 ADD payload.tags :v0",
        ]
        assert event.payload == {"n": 2, "tags": {"a"}}


async def test_writes_of_loaded_instances_keep_attributes_their_model_does_not_declare(
    client_factory: ClientFactory,
) -> None:
    # Two versions of an application during a rolling deploy, or two services sharing a table.
    newer = client_factory()
    await newer.create_tables(ProfileV2)
    older = client_factory(store=newer.store) if newer.store is not None else client_factory()  # the same tables
    await newer.model(ProfileV2).create(ProfileV2(profile_id="p1", name="Ada", nickname="ada"))
    loaded = await older.model(ProfileV1).get_or_raise("p1")
    loaded.name = "Ada L."
    await loaded.save()
    await older.model(ProfileV1).put(loaded)
    stored = await newer.model(ProfileV2).get_or_raise("p1")
    assert (stored.name, stored.nickname, stored.version) == ("Ada L.", "ada", loaded.version)
    await older.model(ProfileV1).update(loaded, F("nickname").remove())  # how to remove one
    assert (await newer.model(ProfileV2).get_or_raise("p1")).nickname is None
    await older.model(ProfileV1).put(ProfileV1(profile_id="p1", name="New"))  # not loaded: replaced
    replaced = await newer.table("profiles_v").get({"profile_id": "p1"})
    assert replaced is not None
    assert re.fullmatch(r"[0-9a-f]{16}", replaced.pop("_wid")), "the id of the write"
    assert re.fullmatch(r"[0-9a-f]{16}", replaced.pop("version")), "a new version token"
    assert replaced == {"profile_id": "p1", "name": "New"}


async def test_documents_keep_the_keys_their_class_does_not_declare(client: dynamoxy.Client) -> None:
    # A newer version of the application stored zip and number in the maps: saves of an older
    # version kept the item's top-level attributes but erased these.
    await client.create_tables(Mailbox, MailboxRecord)
    mailboxes, records = client.model(Mailbox), client.model(MailboxRecord)
    newer = {"city": "Lund", "zip": "22100", "street": {"name": "Main", "number": 5}}
    await mailboxes.table.put({"mailbox_id": "m", "postal": newer, "history": [{"city": "Ystad", "zip": "27100"}]})
    mailbox = await mailboxes.get_or_raise("m")
    assert mailbox.postal == Postal(city="Lund", street=Street(name="Main"))
    assert mailbox.postal is not None
    mailbox.postal.city = "Malmö"
    await mailboxes.save(mailbox)
    await mailboxes.update(mailbox, Mailbox.history.set([*mailbox.history, Postal(city="Lund")]))
    stored = await mailboxes.table.get({"mailbox_id": "m"})
    assert stored is not None
    assert stored["postal"] == {"city": "Malmö", "zip": "22100", "street": {"name": "Main", "number": 5}}
    assert stored["history"] == [{"city": "Ystad", "zip": "27100"}, {"city": "Lund"}]
    # Copies keep the keys of their documents, not the item's undeclared attributes.
    await mailboxes.table.put({"mailbox_id": "m", "postal": newer, "legacy": True})
    moved = (await mailboxes.get_or_raise("m")).copy(mailbox_id="n")
    await mailboxes.create(moved)
    copied = await mailboxes.table.get({"mailbox_id": "n"})
    assert copied is not None
    copied.pop("_wid")
    assert copied == {"mailbox_id": "n", "postal": newer, "history": []}
    # A new document replaces the map, as a write of an instance that was not loaded replaces the item.
    mailbox.postal = Postal(city="Lund")
    await mailboxes.save(mailbox)
    assert (await mailboxes.table.get({"mailbox_id": "m"}) or {})["postal"] == {"city": "Lund"}

    await records.table.put({"mailbox_id": "m", "postal": {"city": "Lund", "zip": "22100"}})
    record = await records.get_or_raise("m")
    assert record.postal == PostalModel(city="Lund")
    await records.save(record)
    assert (await records.table.get({"mailbox_id": "m"}) or {})["postal"] == {"city": "Lund", "zip": "22100"}
