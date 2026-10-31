"""Static typing of the public API, checked by pyright, mypy and pyrefly (``scripts/check_typing.py``).

Nothing here runs: the module is only type-checked. ``assert_type`` pins what each checker infers,
and every line that must be refused carries ``# type: ignore[<mypy's code>]``: mypy
(``warn_unused_ignores``) and pyright (``reportUnnecessaryTypeIgnoreComment``) fail when such a
line type-checks, and the script fails when pyrefly's errors fall on any other line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, assert_type

import pydantic

import dynamoxy
from dynamoxy import Attr, Get, Model, PydanticModel, Repository, field, partition_key, sort_key, version


class User(Model, table="typing-users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    name: Attr[str]
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    version: Attr[str] = version()


class Wallet(PydanticModel, table="typing-wallets"):
    wallet_id: Annotated[str, dynamoxy.PartitionKey()]
    owner: str
    balance: int = 0
    note: str | None = pydantic.Field(default=None, max_length=100)
    opened_at: Annotated[datetime | None, dynamoxy.CreatedAt()] = None


class Customer(Model, table="typing-shop", entity="customer"):
    pk: Attr[str] = partition_key(template="customer#{customer_id}")
    sk: Attr[str] = sort_key(template="profile")
    customer_id: Attr[str]


class Order(Model, table="typing-shop", entity="order"):
    pk: Attr[str] = partition_key(template="customer#{customer_id}")
    sk: Attr[str] = sort_key(template="order#{order_id}")
    customer_id: Attr[str]
    order_id: Attr[str]


class UserRepository(Repository[User]):
    async def by_email(self, email: str) -> User | None:
        return await self.query(index="by_email", email=email).first()


def constructors() -> None:
    assert_type(User(user_id="u1", email="ada@example.com", name="Ada"), User)
    User(user_id="u1", email="ada@example.com", name="Ada", visits="3")  # type: ignore[arg-type]
    User(email="ada@example.com", name="Ada")  # type: ignore[call-arg]
    User(user_id="u1", email="ada@example.com", nmae="Ada")  # type: ignore[call-arg]
    User(user_id="u1", email="ada@example.com", name="Ada", version="v")  # type: ignore[call-arg]

    assert_type(Wallet(wallet_id="w1", owner="ada"), Wallet)
    Wallet(wallet_id="w1", owner=3)  # type: ignore[arg-type]
    Wallet(owner="ada")  # type: ignore[call-arg]
    Wallet(wallet_id="w1", ownr="ada")  # type: ignore[call-arg]
    Wallet("w1", "ada")  # type: ignore[call-arg]


def instances(user: User, wallet: Wallet) -> None:
    assert_type(user.visits, int)
    assert_type(user.tags, set[str])
    assert_type(wallet.balance, int)
    assert_type(wallet.opened_at, datetime | None)
    user.visits = "3"  # type: ignore[assignment]
    wallet.balance = "3"  # type: ignore[assignment]


def conditions() -> None:
    assert_type(User.visits > 3, dynamoxy.Condition)
    _ = User.visits > "3"  # type: ignore[operator]
    assert_type(dynamoxy.attr(Wallet.balance) >= 10, dynamoxy.Condition)
    _ = dynamoxy.attr(Wallet.balance) >= "10"  # type: ignore[operator]
    assert_type(User.visits.set(4), dynamoxy.UpdateAction)
    User.visits.set("4")  # type: ignore[arg-type]


async def model_methods(client: dynamoxy.Client) -> None:
    ada = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
    assert_type(ada, User)
    await User.create(client, user_id="u1", email="ada@example.com", name="Ada", visits="3")  # type: ignore[arg-type]
    await User.create(client, user_id="u1", email="ada@example.com")  # type: ignore[call-arg]

    wallet = await Wallet.create(client, wallet_id="w1", owner="ada")
    assert_type(wallet, Wallet)
    await Wallet.create(client, wallet_id="w1", owner=3)  # type: ignore[arg-type]
    await Wallet.create(client, wallet_idd="w1", owner="ada")  # type: ignore[call-arg]

    assert_type(await User.get(client, "u1"), User | None)
    assert_type(await Wallet.get(client, "w1"), Wallet | None)
    assert_type(await User.query(client, user_id="u1").all(), list[User])
    assert_type(await Wallet.query(client, wallet_id="w1").first(), Wallet | None)

    assert_type(await ada.increment(User.visits), User)
    await ada.increment(User.name)  # type: ignore[arg-type]
    assert_type(await ada.add(User.tags, "admin"), User)
    await ada.add(User.tags, 3)  # type: ignore[misc]
    assert_type(await wallet.increment(dynamoxy.attr(Wallet.balance), 5), Wallet)


async def repositories(client: dynamoxy.Client, ada: User) -> None:
    users = client.model(User)
    assert_type(users, Repository[User])
    assert_type(await users.get("u1"), User | None)
    assert_type(await users.get_or_raise("u1"), User)
    assert_type(await users.create(ada), User)
    await users.create(Wallet(wallet_id="w1", owner="ada"))  # type: ignore[arg-type]
    assert_type(await users.scan().filter(User.visits > 3).all(), list[User])
    assert_type(await users.batch_get(["u1", "u2"]), list[User])

    by_email = UserRepository(client)
    assert_type(await by_email.by_email("ada@example.com"), User | None)
    assert_type(await by_email.get("u1"), User | None)


async def transactions(client: dynamoxy.Client, ada: User, wallet: Wallet) -> None:
    async with client.transaction() as tx:
        assert_type(tx, dynamoxy.Transaction)
        tx.create(ada).update(wallet, dynamoxy.attr(Wallet.balance).add(5))
        tx.check(User, "u1", User.visits > 3)

    async with client.batch_writer() as batch:
        assert_type(batch, dynamoxy.BatchWriter)
        await batch.put(ada)
        await batch.delete(wallet)

    user, other = await client.transact_get(Get(User, "u1"), Get(Wallet, "w1"))
    assert_type(user, User | None)
    assert_type(other, Wallet | None)


async def collections(client: dynamoxy.Client) -> None:
    items = await client.collection(Customer, Order).query(customer_id="c1").all()
    assert_type(items, list[Customer | Order])


async def errors(client: dynamoxy.Client) -> None:
    try:
        await User.create(client, user_id="u2", email="ada@example.com", name="Imposter")
    except dynamoxy.UniqueConstraintError as error:
        assert_type(error.field, str)
    except dynamoxy.DynamoxyError as error:
        assert_type(error, dynamoxy.DynamoxyError)
