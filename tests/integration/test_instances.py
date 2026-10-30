"""Persistence methods of model instances (``save``, ``delete``, ``add``, ...) on every backend."""

from __future__ import annotations

import copy
import pickle
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any, override

import pytest

import dynamoxy
from dynamoxy import Attr, F, field, partition_key, testing
from tests.conftest import ClientFactory
from tests.models import Account, Customer, Event, Order, Player

if TYPE_CHECKING:
    from collections.abc import Mapping


class ProfileV1(dynamoxy.Model, table="profiles_v"):
    """An older version of a model: it does not know the field ``ProfileV2.nickname``."""

    profile_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    version: Attr[int] = dynamoxy.version()


class ProfileV2(dynamoxy.Model, table="profiles_v"):
    profile_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    nickname: Attr[str | None] = field(default=None)
    version: Attr[int] = dynamoxy.version()


@pytest.fixture
async def players(client: dynamoxy.Client) -> dynamoxy.Repository[Player]:
    await client.create_tables(Player)
    return client.model(Player)


async def test_save_writes_the_changes_of_a_loaded_instance(players: dynamoxy.Repository[Player]) -> None:
    await players.create(Player(player_id="p1", name="Ada"))
    player = await players.get_or_raise("p1")
    player.name = "Ada Lovelace"
    player.score = 7
    assert await player.save() is player
    assert (player.version, player.rank) == (2, "SCORE#00000007"), "automatic and derived fields are maintained"
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


async def test_save_checks_versions_and_conditions(players: dynamoxy.Repository[Player]) -> None:
    player = await players.create(Player(player_id="p1", name="Ada"))
    stale = await players.get_or_raise("p1")
    player.name = "Bo"
    await player.save()
    stale.name = "Stale"
    with pytest.raises(dynamoxy.VersionConflictError):
        await stale.save()
    await stale.refresh()  # reload, re-apply the change, save again
    assert (stale.name, stale.version) == ("Bo", 2)
    stale.name = "Cy"
    await stale.save(condition=Player.score == 0)
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await stale.save(condition=Player.score > 0)
    assert not isinstance(caught.value, dynamoxy.VersionConflictError)
    assert (await players.get_or_raise("p1")).name == "Cy"


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
    assert player.version == 15, "every change was one versioned write"
    assert player.rank == "SCORE#00000013", "derived attributes follow the new value"
    assert await players.get_or_raise("p1") == player, "the instance holds what DynamoDB stored"


async def test_lists_are_created_and_derived_index_keys_follow(
    client: dynamoxy.Client, players: dynamoxy.Repository[Player]
) -> None:
    await client.table("players").put({"player_id": "p1", "name": "Ada", "board": "global", "score": 0, "version": 1})
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
    assert (account.owner, account.balance, account.handle, account.version) == ("Ada L.", Decimal("7.5"), "ada2", 5)
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
    assert (stored.name, stored.nickname, stored.version) == ("Ada L.", "ada", 3)
    await older.model(ProfileV1).update(loaded, F("nickname").remove())  # how to remove one
    assert (await newer.model(ProfileV2).get_or_raise("p1")).nickname is None
    await older.model(ProfileV1).put(ProfileV1(profile_id="p1", name="New"))  # not loaded: replaced
    replaced = await newer.table("profiles_v").get({"profile_id": "p1"})
    assert replaced is not None
    assert re.fullmatch(r"[0-9a-f]{16}", replaced.pop("_wid")), "the id of the write"
    assert replaced == {"profile_id": "p1", "name": "New", "version": 1}
