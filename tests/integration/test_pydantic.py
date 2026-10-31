"""Pydantic models on every backend: equality, persistence state and existing models."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import pydantic
import pytest

import dynamoxy
from dynamoxy.codecs import DateTimeCodec, JsonCodec
from tests.models import Account, Contact, ContactRecord


async def test_loaded_models_compare_by_value(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    accounts = client.model(Account)
    created = await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    loaded = await accounts.get_or_raise("a1")
    assert loaded.is_loaded
    assert loaded == created
    assert loaded == Account(account_id="a1", owner="Ada", handle="ada", version=1), "being loaded is not a value"
    assert loaded != Account(account_id="a1", owner="Bo", handle="ada", version=1)
    loaded.owner = "Bo"  # pydantic validates the assignment; the persistence state stays
    assert loaded.is_loaded
    assert set(loaded.model_dump()) == {"account_id", "owner", "handle", "balance", "version"}
    await accounts.save(loaded)  # optimistic locking still knows the stored version
    assert loaded.version == 2


async def test_existing_models_through_a_subclass(client: dynamoxy.Client) -> None:
    await client.create_tables(ContactRecord)
    contacts = client.model(ContactRecord)
    existing = Contact(contact_id="c1", email="ada@example.com", name="Ada")
    await contacts.create(ContactRecord.model_validate(existing.model_dump()))
    loaded = await contacts.get_or_raise("c1")
    assert isinstance(loaded, Contact)
    assert loaded == ContactRecord.model_validate(existing.model_dump())
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await contacts.create(ContactRecord(contact_id="c2", email="ada@example.com", name="Bo"))


async def test_model_copies_track_their_own_stored_item(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    accounts = client.model(Account)
    ada = await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    bob = ada.model_copy(update={"account_id": "a2", "owner": "Bob", "handle": "bob", "version": None})
    await accounts.create(bob)
    # The guard transition of `ada` uses what `ada` knows about its stored item, not `bob`'s.
    await accounts.update(ada, dynamoxy.attr(Account.handle).set("ada2"))
    assert (await accounts.get_or_raise("a1")).handle == "ada2"
    await accounts.create(Account(account_id="a3", owner="Cy", handle="ada"))  # free again
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await accounts.create(Account(account_id="a4", owner="Di", handle="bob"))  # still taken


class Reminder(dynamoxy.PydanticModel, table="reminders"):
    """Optional fields with a codec of their own (``UseCodec``) or a ``Literal`` of ``None``."""

    reminder_id: Annotated[str, dynamoxy.PartitionKey()]
    due: Annotated[datetime | None, dynamoxy.UseCodec(DateTimeCodec("iso_z"))] = None
    data: Annotated[dict[str, Any] | None, dynamoxy.UseCodec(JsonCodec())] = None
    state: Literal["on", "off", None] = None  # noqa: PYI061 - the spelling under test


async def test_fields_with_a_codec_of_their_own_may_be_optional(client: dynamoxy.Client) -> None:
    await client.create_tables(Reminder)
    created = await Reminder.create(client, reminder_id="r1")
    assert (created.due, created.data, created.state) == (None, None, None)
    moment = datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)
    await created.update(
        dynamoxy.attr(Reminder.due).set(moment),
        dynamoxy.attr(Reminder.data).set({"a": [1]}),
        dynamoxy.attr(Reminder.state).set("on"),
    )
    loaded = await Reminder.get(client, "r1", consistent=True)
    assert loaded is not None
    assert (loaded.due, loaded.data, loaded.state) == (moment, {"a": [1]}, "on")
    await loaded.update(dynamoxy.attr(Reminder.due).remove(), dynamoxy.attr(Reminder.data).set(None))
    loaded.state = None
    await loaded.save()
    stored = await client.table("reminders").get({"reminder_id": "r1"}, consistent=True)
    assert stored is not None
    assert not {"due", "data", "state"} & set(stored), "None is not stored"
    assert await Reminder.get(client, "r1", consistent=True) == loaded


class Counted(dynamoxy.PydanticModel, abstract=True):
    """Fields that the models below inherit: one with a default and one required."""

    views: int = 0
    title: str


class Article(Counted, table="pydantic-articles"):
    article_id: Annotated[str, dynamoxy.PartitionKey()]


class Story(Counted, table="pydantic-stories"):
    """Redeclares the inherited fields with defaults of its own (pydantic does not warn)."""

    story_id: Annotated[str, dynamoxy.PartitionKey()]
    views: int = 10
    title: str = "untitled"


async def test_subclasses_inherit_the_defaults_and_required_fields_of_their_base(client: dynamoxy.Client) -> None:
    assert Article.model_fields["views"].default == 0
    assert Article.model_fields["title"].is_required()
    with pytest.raises(pydantic.ValidationError, match="title"):
        Article.model_validate({"article_id": "a0"})
    assert Article(article_id="a0", title="Hi").to_item() == {"article_id": "a0", "views": 0, "title": "Hi"}
    await client.create_tables(Article, Story)
    article = await Article.create(client, article_id="a1", title="Hello")
    assert article.views == 0
    await article.increment(dynamoxy.attr(Article.views))
    await article.increment(dynamoxy.attr(Counted.views))
    assert (await client.model(Article).get_or_raise("a1", consistent=True)).views == 2
    # An item stored before the base class had the field reads its default.
    await client.table("pydantic-articles").put({"article_id": "a2", "title": "Old"})
    assert await Article.get(client, "a2", consistent=True) == Article(article_id="a2", title="Old")
    story = await Story.create(client, story_id="s1")
    assert (story.views, story.title) == (10, "untitled")
    assert Counted.model_fields["views"].default == 0, "the base keeps its own defaults"
    found = await Article.query(client, article_id="a1").filter(dynamoxy.attr(Article.views) == 2).all()
    assert [each.article_id for each in found] == ["a1"]


class Finish(enum.Enum):
    MATTE = "matte"
    GLOSS = "gloss"


class Paint(dynamoxy.PydanticModel, table="paints"):
    """Pydantic holds the values of enum members with ``use_enum_values=True``."""

    model_config = pydantic.ConfigDict(use_enum_values=True)

    paint_id: Annotated[str, dynamoxy.PartitionKey()]
    finish: Finish = Finish.MATTE
    finishes: list[Finish] = pydantic.Field(default_factory=list)
    gsi1pk: Annotated[str, dynamoxy.Template("FINISH#{finish}")] = ""

    by_finish = dynamoxy.GlobalIndex("gsi1", partition_key="gsi1pk")


async def test_models_may_use_enum_values(client: dynamoxy.Client) -> None:
    await client.create_tables(Paint)
    paints = client.model(Paint)
    created = await paints.create(Paint(paint_id="p1", finish=Finish.GLOSS, finishes=[Finish.MATTE]))
    assert (created.finish, created.gsi1pk) == ("gloss", "FINISH#gloss")
    [stored] = await client.table("paints").scan().all()
    assert (stored["finish"], stored["finishes"]) == ("gloss", ["matte"])
    loaded = await paints.get_or_raise("p1")
    assert (loaded.finish, loaded.finishes) == ("gloss", ["matte"])
    await paints.save(loaded)
    await paints.update(loaded, dynamoxy.attr(Paint.finishes).append([Finish.GLOSS]))
    assert (await paints.get_or_raise("p1")).finishes == ["matte", "gloss"]
    assert [paint.paint_id for paint in await paints.query(index=Paint.by_finish, finish="gloss").all()] == ["p1"]
    assert await paints.scan().filter(dynamoxy.attr(Paint.finish) == "gloss").count() == 1


class Shipment(dynamoxy.PydanticModel, table="shipments"):
    """Annotations that name classes declared below the model."""

    shipment_id: Annotated[str, dynamoxy.PartitionKey()]
    address: Destination | None = None
    stops: list[Destination] = pydantic.Field(default_factory=list)


class Destination(pydantic.BaseModel):
    city: str
    next: Destination | None = None


async def test_models_may_refer_to_classes_declared_later(client: dynamoxy.Client) -> None:
    await client.create_tables(Shipment)
    shipments = client.model(Shipment)
    nested = Destination(city="Oslo", next=Destination(city="Bergen"))
    await Shipment.create(client, shipment_id="s1", address=nested, stops=[Destination(city="Lund")])
    loaded = await shipments.get_or_raise("s1")
    assert loaded.address == nested
    assert loaded.stops == [Destination(city="Lund")]


def test_unresolvable_annotations_name_the_missing_class() -> None:
    class Parcel(dynamoxy.PydanticModel, table="parcels"):
        parcel_id: Annotated[str, dynamoxy.PartitionKey()]
        sender: Nowhere | None = None  # noqa: F821  # pyrefly: ignore[unknown-name] - never declared

    with pytest.raises(TypeError, match=r"Parcel: the annotation 'Nowhere' is not defined"):
        dynamoxy.schema_of(Parcel)


class Labels(pydantic.RootModel[list[str]]):
    pass


class Dimensions(pydantic.RootModel[dict[str, int]]):
    pass


class Parcel(dynamoxy.PydanticModel, table="parcels"):
    """``RootModel`` fields, stored as their root values; a default factory that takes the data."""

    parcel_id: Annotated[str, dynamoxy.PartitionKey()]
    labels: Labels = pydantic.Field(default_factory=lambda: Labels([]))
    size: Dimensions | None = None
    slug: str = pydantic.Field(default_factory=lambda data: data["parcel_id"].lower())


async def test_root_model_fields_are_stored_as_their_root(client: dynamoxy.Client) -> None:
    await client.create_tables(Parcel)
    parcels = client.model(Parcel)
    await parcels.create(Parcel(parcel_id="P1", labels=Labels(["fragile"]), size=Dimensions({"width": 2})))
    [stored] = await client.table("parcels").scan().all()
    assert (stored["labels"], stored["size"]) == (["fragile"], {"width": 2})
    loaded = await parcels.get_or_raise("P1")
    assert (loaded.labels, loaded.size) == (Labels(["fragile"]), Dimensions({"width": 2}))
    await parcels.update(loaded, dynamoxy.attr(Parcel.labels).append(Labels(["heavy"])))
    assert (await parcels.get_or_raise("P1")).labels == Labels(["fragile", "heavy"])
    assert await parcels.scan().filter(dynamoxy.attr(Parcel.labels).contains("heavy")).count() == 1


async def test_default_factories_may_take_the_data(client: dynamoxy.Client) -> None:
    await client.create_tables(Parcel)
    parcels = client.model(Parcel)
    assert (await parcels.create(Parcel(parcel_id="P1"))).slug == "p1"
    await client.table("parcels").put({"parcel_id": "P2"})  # written without the attribute
    loaded = await parcels.get_or_raise("P2")
    assert (loaded.slug, loaded.labels) == ("p2", Labels([]))
    await parcels.update(Parcel.key("P3"), dynamoxy.attr(Parcel.size).set(Dimensions({"width": 1})), upsert=True)
    assert (await parcels.get_or_raise("P3")).slug == "p3"
