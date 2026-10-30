"""Write ids (``_wid``) on every backend: each write that runs alone stores a new one.

A write whose attempt may have been applied recognises it by its id (``test_faults.py``).
Transactions and batches store none: their retries need none, and a transaction sent again with
its token must be identical.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key
from tests.models import Event, User


class Plain(Model, table="write-ids-plain", write_ids=False):
    plain_id: Attr[str] = partition_key()
    n: Attr[int] = field(default=0)


async def stored_id(table: dynamoxy.Table, key: Mapping[str, Any]) -> str | None:
    item = await table.get(key, consistent=True)
    assert item is not None
    return item.get("_wid")


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[User]:
    await client.create_tables(User, Event, Plain)
    return client.model(User)


async def test_each_write_stores_a_new_id(users: dynamoxy.Repository[User]) -> None:
    key = {"user_id": "u1"}
    user = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
    created = await stored_id(users.table, key)
    assert created is not None
    assert re.fullmatch(r"[0-9a-f]{16}", created)
    user.name = "Ada L."
    await users.save(user)
    saved = await stored_id(users.table, key)
    await users.update(user, User.visits.increment(1))
    updated = await stored_id(users.table, key)
    assert len({created, saved, updated}) == 3
    assert "_wid" not in user.to_item(), "not part of the instance"
    assert (await users.get_or_raise("u1")).name == "Ada L."


async def test_transactions_and_batches_store_none(client: dynamoxy.Client, users: dynamoxy.Repository[User]) -> None:
    async with client.transaction() as tx:
        tx.create(User(user_id="u2", email="grace@example.com", name="Grace"))
    assert await stored_id(users.table, {"user_id": "u2"}) is None
    events = client.model(Event)
    await events.batch_put([Event(stream="s", seq=1, kind="k")])
    assert await stored_id(events.table, {"stream": "s", "seq": 1}) is None


async def test_models_can_store_none(client: dynamoxy.Client, users: dynamoxy.Repository[User]) -> None:
    plains = client.model(Plain)
    await plains.create(Plain(plain_id="p1"))
    await plains.update({"plain_id": "p1"}, Plain.n.increment(1))
    assert await stored_id(plains.table, {"plain_id": "p1"}) is None


def test_fields_cannot_use_the_attribute_of_write_ids() -> None:
    class Clash(Model, table="clash"):
        clash_id: Attr[str] = partition_key()
        marker: Attr[str] = field(default="", attribute_name="_wid")

    with pytest.raises(TypeError, match="write_ids=False"):
        dynamoxy.schema_of(Clash)

    class Allowed(Model, table="clash", write_ids=False):
        allowed_id: Attr[str] = partition_key()
        marker: Attr[str] = field(default="", attribute_name="_wid")

    assert not dynamoxy.schema_of(Allowed).write_ids
