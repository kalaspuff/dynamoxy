"""Error messages leave out keys and stored values, which may be personal data, on every backend.

The errors keep them as attributes (``.key``, ``.value``, ``.item``, ``FieldError.value``), and
``repr()`` of their field errors leaves the values out too.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key
from tests.models import User

# A key and a unique value that look like personal data.
PERSON = "ada.lovelace@example.com"
EMAIL = "ada@private.example.com"


class Wallet(Model, table="wallets"):
    """A model whose key is personal data, with a value that decodes only from a number."""

    owner: Attr[str] = partition_key()
    balance: Attr[Decimal] = field(default=Decimal(0))


def assert_private(error: BaseException, *values: str) -> None:
    """``values`` are in neither the message of ``error`` nor the repr of its field errors."""
    shown = f"{error} {getattr(error, 'errors', ())!r}"
    for value in values:
        assert value not in shown, f"{type(error).__name__} shows {value!r}: {shown}"


@pytest.fixture
async def users(client: dynamoxy.Client) -> dynamoxy.Repository[User]:
    await client.create_tables(User, Wallet)
    return client.model(User)


def make_user(user_id: str = PERSON, email: str = EMAIL) -> User:
    return User(user_id=user_id, email=email, name="Ada")


async def test_missing_items(users: dynamoxy.Repository[User]) -> None:
    with pytest.raises(dynamoxy.ItemNotFoundError) as caught:
        await users.get_or_raise(PERSON)
    assert_private(caught.value, PERSON)
    assert caught.value.key == {"user_id": PERSON}
    assert "(see .key)" in str(caught.value)

    with pytest.raises(dynamoxy.ItemNotFoundError) as caught:
        await users.update({"user_id": PERSON}, User.name.set("Ada"))
    assert_private(caught.value, PERSON)
    assert caught.value.key == {"user_id": PERSON}

    user = await users.create(make_user())
    await users.delete(PERSON)
    with pytest.raises(dynamoxy.ItemNotFoundError) as caught:
        await users.refresh(user)
    assert_private(caught.value, PERSON)


async def test_conflicts(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    with pytest.raises(dynamoxy.ItemAlreadyExistsError) as caught:
        await users.create(make_user(email="other@example.com"))
    assert_private(caught.value, PERSON, EMAIL)
    assert caught.value.key == {"user_id": PERSON}
    assert caught.value.item is not None
    assert caught.value.item["email"] == EMAIL

    stale = await users.get_or_raise(PERSON)
    await users.save(user)
    stale.name = "Augusta"
    with pytest.raises(dynamoxy.VersionConflictError) as conflict:
        await users.save(stale)
    assert_private(conflict.value, PERSON, EMAIL)
    assert conflict.value.key == {"user_id": PERSON}
    assert "stored version 2, expected version 1" in str(conflict.value)

    with pytest.raises(dynamoxy.ConditionalCheckFailedError) as failed:
        await users.save(user, condition=User.name == "Augusta")
    assert type(failed.value) is dynamoxy.ConditionalCheckFailedError
    assert_private(failed.value, PERSON, EMAIL)
    assert failed.value.key == {"user_id": PERSON}


async def test_unique_values(users: dynamoxy.Repository[User]) -> None:
    await users.create(make_user())
    with pytest.raises(dynamoxy.UniqueConstraintError) as caught:
        await users.create(make_user(user_id="u2"))
    assert_private(caught.value, EMAIL)
    assert (caught.value.field, caught.value.value) == ("email", EMAIL)
    assert "(see .value)" in str(caught.value)


async def test_changed_keys(users: dynamoxy.Repository[User]) -> None:
    user = await users.create(make_user())
    user.user_id = "someone.else@example.com"
    with pytest.raises(ValueError, match="the key of this instance changed") as caught:
        await users.save(user)
    assert_private(caught.value, PERSON, "someone.else@example.com")


async def test_stored_items_that_do_not_decode(client: dynamoxy.Client, users: dynamoxy.Repository[User]) -> None:
    wallets = client.model(Wallet)
    await wallets.table.put({"owner": PERSON, "balance": "secret-balance"})  # another writer
    with pytest.raises(dynamoxy.ItemDecodeError) as caught:
        await wallets.get(PERSON)
    assert_private(caught.value, PERSON, "secret-balance")
    assert caught.value.key == {"owner": PERSON}
    assert [(problem.path, problem.value) for problem in caught.value.errors] == [("balance", "secret-balance")]
