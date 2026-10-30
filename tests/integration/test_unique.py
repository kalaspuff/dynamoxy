"""Unique constraints (guard items) on every backend."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key
from tests.models import Account, User


class Membership(Model, table="memberships"):
    """Unique values in a table with a sort key, and no entity type."""

    org_id: Attr[str] = partition_key()
    user_id: Attr[str] = sort_key()
    email: Attr[str] = field(unique=True)


class Profile(Model, table="profiles"):
    """Optional unique values, one of them a number."""

    profile_id: Attr[str] = partition_key()
    handle: Attr[str | None] = field(default=None, unique=True)
    seat: Attr[Decimal | None] = field(default=None, unique=True)


async def guards(repository: dynamoxy.Repository[Any]) -> list[str]:
    """The partition keys of the guard items in the repository's table."""
    partition = repository.schema.partition_key
    assert partition is not None
    items = await repository.table.scan(consistent=True).all()
    return sorted(item[partition.attribute] for item in items if item[partition.attribute].startswith("_unique#"))


def make_user(user_id: str, email: str, **changes: Any) -> User:
    return User(user_id=user_id, email=email, name=user_id, **changes)


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[User]:
    await client.create_tables(User, Account)
    return client.model(User)


async def test_values_are_unique_across_items(users: dynamoxy.Repository[User]) -> None:
    await users.create(make_user("u1", "ada@example.com"))
    with pytest.raises(dynamoxy.UniqueConstraintError) as caught:
        await users.create(make_user("u2", "ada@example.com"))
    error = caught.value
    assert (error.model, error.field, error.value) == (User, "email", "ada@example.com")
    assert isinstance(error, dynamoxy.ConditionalCheckFailedError)
    assert await users.get("u2") is None, "nothing of the failed write is stored"


async def test_changing_a_value_frees_the_old_one(users: dynamoxy.Repository[User]) -> None:
    ada = await users.create(make_user("u1", "ada@example.com"))
    await users.update(ada, User.email.set("ada@new.example.com"))
    assert ada.email == "ada@new.example.com"
    assert ada.version == 2
    await users.create(make_user("u2", "ada@example.com"))
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await users.update(ada, User.email.set("ada@example.com"))
    stored = await users.get_or_raise("u1")
    assert stored.email == "ada@new.example.com"


async def test_save_and_put_maintain_guards(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user("u1", "one@example.com"))
    user.email = "two@example.com"
    await users.save(user)
    await users.create(make_user("u2", "one@example.com"))
    # A put of an instance that was never loaded reads the stored item to find the old value.
    await users.put(make_user("u1", "three@example.com"))
    await users.create(make_user("u3", "two@example.com"))
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await users.put(make_user("u1", "one@example.com"))


async def test_delete_frees_the_value(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user("u1", "ada@example.com"))
    await users.delete(user)
    await users.create(make_user("u2", "ada@example.com"))
    assert await users.delete("u1") is None
    deleted = await users.delete("u2")
    assert deleted is not None
    assert deleted.email == "ada@example.com"
    await users.create(make_user("u3", "ada@example.com"))


async def test_guard_items_stay_out_of_scans_and_counts(users: dynamoxy.Repository[User]) -> None:
    for index in range(3):
        await users.create(make_user(f"u{index}", f"u{index}@example.com"))
    assert await users.count() == 3
    assert sorted(user.user_id for user in await users.scan().all()) == ["u0", "u1", "u2"]
    raw = await users.table.scan().all()
    assert len(raw) == 6, "three items and three guard items"


async def test_unique_updates_need_plain_values(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user("u1", "ada@example.com"))
    with pytest.raises(ValueError, match=r"update it with \.set"):
        await users.update(user, User.email.set_if_not_exists("x@example.com"))


async def test_batches_refuse_unique_models(users: dynamoxy.Repository[User]) -> None:
    with pytest.raises(TypeError, match="unique attributes"):
        await users.batch_put([make_user("u1", "a@example.com")])


async def test_pydantic_unique_and_versions(client: dynamoxy.Client, users: dynamoxy.Repository[User]) -> None:
    accounts = client.model(Account)
    account = await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    assert account.version == 1
    assert account.is_loaded
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await accounts.create(Account(account_id="a2", owner="Bob", handle="ada"))
    stale = await accounts.get_or_raise("a1")
    await accounts.update(account, Account.balance.add(10))  # type: ignore[arg-type]
    assert account.balance == 10
    assert account.version == 2
    with pytest.raises(dynamoxy.VersionConflictError):
        await accounts.save(stale)


async def test_partitions_of_unique_models_can_be_queried(client: dynamoxy.Client) -> None:
    await client.create_tables(Membership)
    memberships = client.model(Membership)
    for user_id in ("u1", "u2", "u3"):
        await memberships.create(Membership(org_id="o1", user_id=user_id, email=f"{user_id}@example.com"))
    # DynamoDB refuses filters on the key attributes a query reads: guard items need none here.
    assert [member.user_id for member in await memberships.query(org_id="o1").all()] == ["u1", "u2", "u3"]
    assert await memberships.count(org_id="o1") == 3
    assert [
        member.user_id for member in await memberships.query(org_id="o1").where(Membership.user_id > "u1").all()
    ] == [
        "u2",
        "u3",
    ]
    assert (await memberships.query(org_id="o1").descending().first()).user_id == "u3"  # type: ignore[union-attr]
    assert await memberships.count() == 3, "scans still leave guard items out"
    assert sorted(member.user_id for member in await memberships.scan().all()) == ["u1", "u2", "u3"]


async def test_optional_unique_values_can_be_removed_and_set_again(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    profile = await profiles.create(Profile(profile_id="p1", handle="ada"))
    await profiles.update(profile, Profile.handle.set(None))
    assert profile.handle is None
    await profiles.create(Profile(profile_id="p2", handle="ada"))  # the old value is free
    await profiles.update(profile, Profile.handle.set("lovelace"))
    await profiles.update(Profile.key("p1"), Profile.handle.set("countess"))
    assert (await profiles.get_or_raise("p1")).handle == "countess"
    assert await guards(profiles) == ["_unique#Profile#handle#ada", "_unique#Profile#handle#countess"]


async def test_unique_values_stored_as_null_can_be_set(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    # A NULL stored by another writer (or by set(None) of older versions) reads as None.
    await profiles.table.put({"profile_id": "p1", "handle": None, "seat": None})
    profile = await profiles.get_or_raise("p1")
    assert (profile.handle, profile.seat) == (None, None)
    await profiles.update(profile, Profile.handle.set("ada"))
    await profiles.table.put({"profile_id": "p2", "seat": None})
    other = await profiles.get_or_raise("p2")
    other.seat = Decimal(7)
    await other.save()
    await profiles.table.put({"profile_id": "p3", "handle": None})
    third = await profiles.get_or_raise("p3")
    third.handle = "grace"
    await profiles.put(third)
    assert await guards(profiles) == [
        "_unique#Profile#handle#ada",
        "_unique#Profile#handle#grace",
        "_unique#Profile#seat#7",
    ]


async def test_unique_numbers_compare_as_numbers(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    await profiles.create(Profile(profile_id="p1", seat=Decimal("5.0")))
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await profiles.create(Profile(profile_id="p2", seat=Decimal(5)))
    loaded = await profiles.get_or_raise("p1")
    await profiles.update(loaded, Profile.seat.set(Decimal(6)))
    await profiles.create(Profile(profile_id="p3", seat=Decimal("5.00")))  # the guard of 5 went with the change
    assert await guards(profiles) == ["_unique#Profile#seat#5", "_unique#Profile#seat#6"]


async def test_put_of_an_instance_whose_unique_values_changed_meanwhile(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    stale = await profiles.create(Profile(profile_id="p1", handle="one"))
    other = await profiles.get_or_raise("p1")
    await profiles.update(other, Profile.handle.set("two"))
    stale.seat = Decimal(1)
    await profiles.put(stale)  # put() replaces what is stored: the guards follow the stored item
    stored = await profiles.get_or_raise("p1")
    assert (stored.handle, stored.seat) == ("one", Decimal(1))
    await profiles.create(Profile(profile_id="p2", handle="two")), "the value of the other write is free again"
    assert await guards(profiles) == [
        "_unique#Profile#handle#one",
        "_unique#Profile#handle#two",
        "_unique#Profile#seat#1",
    ]


def record_write_hooks(client: dynamoxy.Client) -> list[str]:
    """The update and delete hooks that run for profiles from now on, in order."""
    calls: list[str] = []
    client.hook("before_update", Profile)(lambda profile, context: calls.append("before_update"))
    client.hook("after_update", Profile)(lambda profile, context: calls.append("after_update"))
    client.hook("before_delete", Profile)(lambda profile, context: calls.append("before_delete"))
    client.hook("after_delete", Profile)(lambda profile, context: calls.append("after_delete"))
    return calls


async def test_conditional_deletes_of_missing_items_check_the_condition(client: dynamoxy.Client) -> None:
    await client.create_tables(User, Profile)
    profiles = client.model(Profile)
    # As without unique attributes, the condition applies to the missing item: the delete fails
    # unless it holds for an item that does not exist.
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await profiles.delete("p1", condition=Profile.handle == "ada")
    assert type(caught.value) is dynamoxy.ConditionalCheckFailedError
    assert (caught.value.key, caught.value.item) == (Profile.key("p1"), None)
    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as caught:
        await client.model(User).delete(User.key("u1"), condition=User.name == "Ada")
    assert type(caught.value) is dynamoxy.ConditionalCheckFailedError, "not a version conflict"
    assert await profiles.delete("p1", condition=Profile.handle.not_exists()) is None
    assert await profiles.table.scan(consistent=True).all() == []


async def test_conditional_deletes_of_missing_items_guard_transactions(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:  # noqa: PT012 - the block commits
        async with client.transaction() as tx:
            tx.delete(Profile.key("p1"), model=Profile, condition=Profile.handle == "ada")
            tx.create(Profile(profile_id="p2", handle="bo"))
    reasons = dynamoxy.cancellation_reasons(caught.value)
    assert [(reason.operation, reason.code) for reason in reasons] == [
        ("delete Profile", "ConditionalCheckFailed"),
        ("create Profile", "None"),
        ("create Profile", "None"),
    ]
    assert type(reasons[0].error) is dynamoxy.ConditionalCheckFailedError
    assert await profiles.get("p2") is None, "the rest of the transaction was not applied"
    async with client.transaction() as tx:
        tx.delete(Profile.key("p1"), model=Profile, condition=Profile.handle.not_exists())
        tx.create(Profile(profile_id="p2", handle="bo"))
    assert await profiles.get("p2") is not None
    with dynamoxy.collect() as collected:
        await client.transaction().delete(Profile.key("p1"), model=Profile).commit()
    assert collected.by_operation() == {"GetItem": 1}, "nothing to delete, and nothing to check"


async def test_conditional_deletes_read_again_when_the_item_is_stored_meanwhile(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)

    async def store_after_the_read(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        response = await call_next(request)
        if request.operation == "GetItem":
            remove()
            await profiles.create(Profile(profile_id="p1", handle="ada"))
        return response

    remove = client.use(store_after_the_read)
    # The read finds no item, and the delete requires that there still is none: it fails, and is
    # planned again from the item stored meanwhile, so the item and its guard go together.
    deleted = await profiles.delete("p1", condition=Profile.handle == "ada")
    assert deleted is not None
    assert deleted.handle == "ada"
    assert await profiles.table.scan(consistent=True).all() == []
    # A transaction cannot plan again: it is canceled, and the item keeps its guard.
    remove = client.use(store_after_the_read)
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:
        await client.transaction().delete(Profile.key("p1"), model=Profile, condition=Profile.handle == "ada").commit()
    (error,) = caught.value.errors
    assert type(error) is dynamoxy.ConditionalCheckFailedError
    assert "another writer stored the item after it was read: retry" in str(error)
    assert await guards(profiles) == ["_unique#Profile#handle#ada"]
    assert await profiles.delete("p1", condition=Profile.handle == "ada") is not None


async def test_deletes_of_instances_deleted_meanwhile_return_none(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    calls = record_write_hooks(client)
    # As without unique attributes: nothing is deleted, and the instance is no longer loaded.
    for profile_id, handle in (("p1", "ada"), ("p2", None)):
        stale = await profiles.create(Profile(profile_id=profile_id, handle=handle))
        await profiles.delete(profile_id)
        assert await profiles.delete(stale) is None
        assert not stale.is_loaded
    assert calls == ["before_delete", "after_delete"] * 2, "hooks of instances only, once per delete"
    assert await profiles.delete(Profile(profile_id="p3")) is None
    assert calls[4:] == ["before_delete", "after_delete"]
    assert await profiles.table.scan(consistent=True).all() == []


async def test_deletes_of_instances_whose_unique_values_changed_meanwhile(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    stale = await profiles.create(Profile(profile_id="p1", handle="one"))
    unset = await profiles.create(Profile(profile_id="p2"))
    await profiles.update(Profile.key("p1"), Profile.handle.set("two"), Profile.seat.set(Decimal(1)))
    await profiles.update(Profile.key("p2"), Profile.handle.set("three"))
    calls = record_write_hooks(client)
    with dynamoxy.collect() as collected:
        deleted = await profiles.delete(stale)  # reads the stored item, and deletes it with its guards
    assert deleted is not None
    assert (deleted.handle, deleted.seat) == ("two", Decimal(1))
    assert collected.by_operation() == {"TransactWriteItems": 2, "GetItem": 1}
    assert calls == ["before_delete", "after_delete"]
    assert not stale.is_loaded
    deleted = await profiles.delete(unset)  # a delete without guards, which another writer added
    assert deleted is not None
    assert deleted.handle == "three"
    assert await profiles.table.scan(consistent=True).all() == []


async def test_updates_of_instances_whose_unique_values_changed_meanwhile(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    stale = await profiles.create(Profile(profile_id="p1", handle="one"))
    await profiles.update(Profile.key("p1"), Profile.handle.set("two"))
    calls = record_write_hooks(client)
    await profiles.update(stale, Profile.handle.set("three"))  # reads the stored item, and moves its guard
    assert stale.handle == "three"
    assert calls == ["before_update", "after_update"]
    assert (await profiles.get_or_raise("p1")).handle == "three"
    assert await guards(profiles) == ["_unique#Profile#handle#three"]
    await profiles.update(Profile.key("p1"), Profile.handle.remove())
    await stale.update(Profile.seat.set(Decimal(2)), Profile.handle.set("four"))
    assert await guards(profiles) == ["_unique#Profile#handle#four", "_unique#Profile#seat#2"]
    await profiles.delete("p1")
    with pytest.raises(dynamoxy.ItemNotFoundError):
        await profiles.update(stale, Profile.handle.set("five"))
    assert await profiles.table.scan(consistent=True).all() == []


async def test_transactions_explain_stale_unique_values(client: dynamoxy.Client) -> None:
    await client.create_tables(Profile)
    profiles = client.model(Profile)
    stale = await profiles.create(Profile(profile_id="p1", handle="one"))
    await profiles.update(Profile.key("p1"), Profile.handle.set("two"))
    # A transaction cannot read the item again: it is canceled, and says why.
    for transaction in (
        client.transaction().delete(stale),
        client.transaction().update(stale, Profile.handle.set("three")),
    ):
        with pytest.raises(dynamoxy.TransactionCanceledError) as caught:
            await transaction.commit()
        (error,) = caught.value.errors
        assert type(error) is dynamoxy.ConditionalCheckFailedError
        assert "may have changed since the instance was loaded: refresh() it and retry" in str(error)
    await stale.refresh()
    async with client.transaction() as tx:
        tx.delete(stale)
    assert await profiles.table.scan(consistent=True).all() == []
