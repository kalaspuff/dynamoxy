"""Models shared by the tests: a plain table, an event log, a single-table design, counters, inherited
timestamps and pydantic.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from dynamoxy import (
    Attr,
    Document,
    GlobalIndex,
    LocalIndex,
    Model,
    PartitionKey,
    Projection,
    PydanticModel,
    Unique,
    Version,
    created_at,
    field,
    partition_key,
    sort_key,
    ttl,
    updated_at,
    version,
)


class Status(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class User(Model, table="users"):
    """A user with a unique email, versioning and timestamps."""

    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True, max_length=320, pattern=r"[^@\s]+@[^@\s]+")
    name: Attr[str] = field(min_length=1)
    age: Attr[int | None] = field(default=None, ge=0)
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    status: Attr[Status] = field(default=Status.ACTIVE)
    created_at: Attr[datetime] = created_at()
    updated_at: Attr[datetime] = updated_at()
    version: Attr[int] = version()

    by_email = GlobalIndex(partition_key=email)
    by_status = GlobalIndex(partition_key=status, sort_key=created_at, projection=Projection.keys_only())


class Event(Model, table="events"):
    """An event log: no unique attributes and no version, so batch writes work."""

    stream: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    kind: Attr[str]
    payload: Attr[dict[str, Any]] = field(default_factory=dict)
    expires_at: Attr[datetime | None] = ttl(default=None)

    # `kind` has no field specifier, so it has no value in the class body yet: refer to it by name.
    by_kind = GlobalIndex(partition_key="kind", sort_key=seq)
    by_kind_local = LocalIndex(sort_key="kind", projection=Projection.include("payload"))


class OrderStatus(enum.StrEnum):
    NEW = "new"
    PAID = "paid"
    SHIPPED = "shipped"


class LineItem(Document):
    sku: Attr[str]
    quantity: Attr[int] = field(ge=1)
    price: Attr[Decimal]


class Customer(Model, table="app", entity="Customer"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="PROFILE")
    customer_id: Attr[str]
    name: Attr[str]


class Order(Model, table="app", entity="Order"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="ORDER#{order_id}")
    customer_id: Attr[str]
    order_id: Attr[str]
    placed_at: Attr[datetime]
    total: Attr[Decimal] = field(ge=0)
    status: Attr[OrderStatus] = field(default=OrderStatus.NEW)
    lines: Attr[list[LineItem]] = field(default_factory=list)
    gsi1pk: Attr[str] = field(template="STATUS#{status}")
    gsi1sk: Attr[str] = field(template="{placed_at}")

    by_status = GlobalIndex("gsi1", partition_key=gsi1pk, sort_key=gsi1sk)


class Player(Model, table="players"):
    """A versioned model with counters, collections and an index key derived from a counter."""

    player_id: Attr[str] = partition_key()
    name: Attr[str] = field(min_length=1)
    board: Attr[str] = field(default="global")
    score: Attr[int] = field(default=0)
    coins: Attr[Decimal] = field(default=Decimal(0))
    badges: Attr[set[str]] = field(default_factory=set)
    history: Attr[list[str]] = field(default_factory=list)
    stats: Attr[dict[str, int]] = field(default_factory=dict)
    rank: Attr[str] = field(template="SCORE#{score:08d}")
    updated_at: Attr[datetime] = updated_at()
    version: Attr[int] = version()

    by_rank = GlobalIndex(partition_key=board, sort_key=rank)


class Timestamped(Model, abstract=True):
    """Timestamps declared once; each subclass stores them in its own ``datetime_format``."""

    published_at: Attr[datetime] = sort_key()
    seen_at: Attr[datetime | None] = field(default=None)


class Article(Timestamped, table="articles", datetime_format="iso_z"):
    """Inherits its timestamps and stores them with a ``Z`` suffix."""

    article_id: Attr[str] = partition_key()


class Account(PydanticModel, table="accounts"):
    """A pydantic model with optimistic locking."""

    account_id: Annotated[str, PartitionKey()]
    owner: str
    handle: Annotated[str, Unique()]
    balance: Decimal = Decimal(0)
    version: Annotated[int | None, Version()] = None


class Contact(BaseModel):
    """A plain pydantic model, as an application may already have one (an API schema)."""

    contact_id: str
    email: str
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class ContactRecord(Contact, PydanticModel, table="contacts"):
    """Stores the unchanged ``Contact``: only the fields that need markers are re-declared."""

    contact_id: Annotated[str, PartitionKey()]
    email: Annotated[str, Unique()]
