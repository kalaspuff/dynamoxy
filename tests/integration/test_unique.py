"""Unique constraints (guard items) on every backend."""

from __future__ import annotations

from typing import Any

import pytest

import dynamoxy
from tests.models import Account, User


def make_user(user_id: str, email: str, **changes: Any) -> User:
    return User(user_id=user_id, email=email, name=user_id, **changes)


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[User]:
    await client.create_tables(User, Account)
    return client.repository(User)


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
    accounts = client.repository(Account)
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
