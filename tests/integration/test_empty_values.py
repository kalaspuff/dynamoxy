"""None and empty sets read back as written, not as the field's default, on every backend.

dynamoxy stores both as no attribute (DynamoDB has no empty sets), and read a missing attribute as
the field's default: a nickname cleared to ``None`` came back as ``"anon"``, and a revoked role
came back, so that the next ``save()`` stored it again.
"""

from __future__ import annotations

from typing import Annotated, Any

import pydantic
import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, version


class Member(Model, table="members"):
    member_id: Attr[str] = partition_key()
    nickname: Attr[str | None] = field(default="anon")
    roles: Attr[set[str]] = field(default_factory=lambda: {"member"})
    badges: Attr[frozenset[int]] = field(default=frozenset({1}))
    labels: Attr[set[str] | None] = field(default=None)
    visits: Attr[int] = field(default=7)
    version: Attr[int] = version()


class PydanticMember(dynamoxy.PydanticModel, table="pydantic-members"):
    member_id: Annotated[str, dynamoxy.PartitionKey()]
    nickname: str | None = "anon"
    roles: set[str] = pydantic.Field(default_factory=lambda: {"member"})
    visits: int = 7


class Team(Model, table="teams"):
    team_id: Attr[str] = partition_key()
    members: Attr[set[str]] = field(min_length=1)


@pytest.mark.parametrize("model", [Member, PydanticMember])
async def test_none_and_empty_sets_read_back_as_written(client: dynamoxy.Client, model: Any) -> None:
    await client.create_tables(model)
    members = client.model(model)
    await members.create(model(member_id="m1", nickname=None, roles=set()))
    stored = await members.table.get({"member_id": "m1"})
    assert stored is not None
    assert not {"nickname", "roles"} & stored.keys(), "None and empty sets are stored as nothing"
    loaded = await members.get_or_raise("m1")
    assert (loaded.nickname, loaded.roles, loaded.visits) == (None, set(), 7)
    await members.save(loaded)  # stores what it read
    again = await members.get_or_raise("m1")
    assert (again.nickname, again.roles) == (None, set()), "the defaults were not stored"
    fresh = model(member_id="m2")
    assert (fresh.nickname, fresh.roles) == ("anon", {"member"}), "new instances get the defaults"


@pytest.mark.parametrize("model", [Member, PydanticMember])
async def test_items_without_an_attribute_read_none_or_an_empty_set(client: dynamoxy.Client, model: Any) -> None:
    await client.create_tables(model)
    members = client.model(model)
    await members.table.put({"member_id": "m1"})  # written before the fields existed, or by another writer
    loaded = await members.get_or_raise("m1")
    assert (loaded.nickname, loaded.roles) == (None, set())
    assert loaded.visits == 7, "fields that can hold neither read their default"


async def test_optional_sets_read_an_empty_set_back_as_none(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    await Member.create(client, member_id="m1", labels=set(), badges=frozenset())
    loaded = await Member.get(client, "m1")
    assert loaded is not None
    assert loaded.badges == frozenset()
    assert loaded.labels is None, "None and an empty set are both stored as nothing"


async def test_sets_emptied_by_updates_read_back_empty(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    member = await Member.create(client, member_id="m1")
    assert member.roles == {"member"}
    await member.update(Member.roles.delete({"member"}))  # DynamoDB removes the emptied set
    assert member.roles == set()
    assert (await Member.get(client, "m1", consistent=True) or member).roles == set()
    async with client.transaction() as tx:  # refreshed without a read
        tx.update(member, Member.badges.delete(frozenset({1})))
    assert member.badges == frozenset()
    assert (await Member.get(client, "m1", consistent=True) or member).badges == frozenset()
    await member.add(Member.roles, "admin")
    assert member.roles == {"admin"}


async def test_setting_an_empty_set_stores_nothing(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    members = client.model(Member)
    member = await Member.create(client, member_id="m1", roles={"admin"}, labels={"x"})
    await member.update(Member.roles.set(set()), Member.labels.set(set()))
    assert (member.roles, member.labels) == (set(), None)
    stored = await members.table.get({"member_id": "m1"})
    assert stored is not None
    assert not {"roles", "labels"} & stored.keys(), "no NULL, which a later ADD fails on"
    await member.update(Member.roles.add({"a"}), Member.labels.add({"b"}))
    assert (member.roles, member.labels) == ({"a"}, {"b"})
    await member.update(Member.roles.remove())
    assert member.roles == set()
    assert (await members.get_or_raise("m1")).roles == set()


async def test_emptying_a_set_checks_its_constraints(client: dynamoxy.Client) -> None:
    await client.create_tables(Team)
    team = await Team.create(client, team_id="t1", members={"ada"})
    with dynamoxy.collect() as collected:
        for action in (Team.members.remove(), Team.members.set(set())):
            with pytest.raises(dynamoxy.ValidationError, match="must have at least 1"):
                await team.update(action)
    assert collected.calls == 0, "nothing was sent"


async def test_upserts_by_key_give_no_default_over_none_or_an_empty_set(client: dynamoxy.Client) -> None:
    await client.create_tables(Member)
    members = client.model(Member)
    await Member.create(client, member_id="m1", nickname=None, roles=set())
    # if_not_exists(nickname, "anon") cannot tell this item's None (no attribute) from a new item.
    updated = await members.update(Member.key("m1"), Member.visits.increment(1), upsert=True)
    assert (updated.nickname, updated.roles, updated.visits) == (None, set(), 8)
    created = await members.update(Member.key("m2"), Member.visits.increment(1), upsert=True)
    assert (created.nickname, created.roles, created.badges, created.visits) == (None, set(), frozenset(), 1)
    named = await members.update(
        Member.key("m3"), Member.nickname.set_if_not_exists("Ada"), Member.roles.add({"member"}), upsert=True
    )
    assert (named.nickname, named.roles, named.visits) == ("Ada", {"member"}, 7)
