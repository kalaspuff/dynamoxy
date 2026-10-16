"""Pydantic models: markers, keys, nested models and expressions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

import pydantic
import pytest

import dynamoxy
from dynamoxy import F, schema_of
from dynamoxy.expressions import ExpressionBuilder
from tests.models import Account, Contact, ContactRecord


class Address(pydantic.BaseModel):
    street: str
    city: str = "Oslo"


class Profile(dynamoxy.PydanticModel, table="profiles", entity="Profile"):
    pk: Annotated[str, dynamoxy.PartitionKey(template="USER#{user_id}")] = ""
    sk: Annotated[str, dynamoxy.SortKey(template="PROFILE")] = ""
    user_id: str
    email: Annotated[str, dynamoxy.Unique(), dynamoxy.AttributeName("e")] = pydantic.Field(max_length=50)
    address: Address | None = None
    tags: set[str] = pydantic.Field(default_factory=set)
    created: Annotated[datetime | None, dynamoxy.CreatedAt()] = None
    updated: Annotated[datetime | None, dynamoxy.UpdatedAt("epoch")] = None
    expires: Annotated[datetime | None, dynamoxy.TTL()] = None
    status_key: Annotated[str, dynamoxy.Template("EMAIL#{email}")] = ""

    by_email = dynamoxy.GlobalIndex(partition_key="status_key")


def test_markers_configure_the_schema() -> None:
    schema = schema_of(Profile)
    assert schema.partition_key is not None
    assert schema.partition_key.template is not None
    assert schema.fields["email"].attribute == "e"
    assert [spec.name for spec in schema.unique_fields] == ["email"]
    assert schema.created_at_field is not None
    assert schema.updated_at_field is not None
    assert schema.ttl_field is not None
    assert schema.fields["updated"].codec.dynamo_type == "N"
    assert schema.indexes["by_email"].partition_key.name == "status_key"
    assert schema.entity == "Profile"


def test_encoding_and_decoding() -> None:
    profile = Profile(user_id="u1", email="a@b.c", address=Address(street="Main"), tags={"x"})
    item = profile.to_item()
    assert item == {
        "pk": "USER#u1",
        "sk": "PROFILE",
        "user_id": "u1",
        "e": "a@b.c",
        "address": {"street": "Main", "city": "Oslo"},
        "tags": {"x"},
        "status_key": "EMAIL#a@b.c",
        "_type": "Profile",
    }
    decoded = Profile.from_item(item)
    assert decoded.address == Address(street="Main")
    assert decoded.email == "a@b.c"
    assert Profile.key(user_id="u1") == {"pk": "USER#u1", "sk": "PROFILE"}
    assert profile.primary_key() == {"pk": "USER#u1", "sk": "PROFILE"}


def test_pydantic_validates_and_class_attributes_are_paths() -> None:
    with pytest.raises(pydantic.ValidationError):
        Profile(user_id="u1", email="x" * 60)
    builder = ExpressionBuilder()
    text = builder.condition(dynamoxy.attr(Profile.email) == "a@b.c")
    assert text == "#a0 = :v0"
    assert builder.names == {"#a0": "e"}
    profile = Profile(user_id="u1", email="a@b.c")
    assert profile.email == "a@b.c", "instances keep plain values"
    with pytest.raises(TypeError, match="model attribute"):
        dynamoxy.attr("email")


def test_decode_errors_are_item_decode_errors() -> None:
    stored = "ada@example.com" + "x" * 50
    with pytest.raises(dynamoxy.ItemDecodeError, match="cannot decode Profile") as caught:
        Profile.from_item({"pk": "USER#u1", "sk": "PROFILE", "user_id": "u1", "e": stored})
    # Pydantic quotes inputs in its messages; dynamoxy's leave them out (they may be personal data).
    assert "ada@example.com" not in str(caught.value)
    assert "email: String should have at most 50 characters" in str(caught.value)


class Shipment(dynamoxy.Model, table="shipments"):
    shipment_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
    address: dynamoxy.Attr[Address]


def test_nested_pydantic_errors_leave_inputs_out() -> None:
    with pytest.raises(dynamoxy.ValidationError) as caught:
        Shipment(shipment_id="s1", address={"street": 4155550123})  # type: ignore[arg-type]
    shown = f"{caught.value} {caught.value.errors!r}"
    assert "4155550123" not in shown
    assert "address: Address: street: Input should be a valid string" in shown


def test_account_model_from_the_shared_models() -> None:
    account = Account(account_id="a1", owner="Ada", handle="ada", balance=Decimal("10.5"))
    assert account.to_item() == {"account_id": "a1", "owner": "Ada", "handle": "ada", "balance": Decimal("10.5")}
    assert schema_of(Account).version_field is not None
    assert not account.is_loaded


def test_existing_models_are_stored_through_a_subclass() -> None:
    schema = schema_of(ContactRecord)
    assert schema.partition_key is not None
    assert schema.partition_key.name == "contact_id"
    assert [spec.name for spec in schema.unique_fields] == ["email"]
    record = ContactRecord.model_validate(Contact(contact_id="c1", email="a@b.c", name=" Ada ").model_dump())
    assert isinstance(record, Contact)
    assert record.to_item() == {"contact_id": "c1", "email": "a@b.c", "name": "Ada"}, "validators are inherited"
    assert ContactRecord.from_item(record.to_item()).name == "Ada"
    builder = ExpressionBuilder()
    assert builder.condition(dynamoxy.attr(ContactRecord.name) == "Ada") == "#a0 = :v0"
    assert builder.names == {"#a0": "name"}
    assert not hasattr(Contact, "__dynamoxy_options__"), "the base model stays plain pydantic"
    with pytest.raises(pydantic.ValidationError):
        ContactRecord(contact_id="c2", email="b@c.d", name="")


def test_plain_pydantic_models_nest_in_native_models() -> None:
    class Holder(dynamoxy.Model, table="holders"):
        pk: dynamoxy.Attr[str] = dynamoxy.partition_key()
        address: dynamoxy.Attr[Address]

    holder = Holder(pk="h", address=Address(street="Elm"))
    assert holder.to_item() == {"pk": "h", "address": {"street": "Elm", "city": "Oslo"}}
    assert Holder.from_item(holder.to_item()).address == Address(street="Elm")
    builder = ExpressionBuilder()
    assert builder.condition(Holder.address["city"] == "Oslo") == "#a0.#a1 = :v0"
    assert builder.condition(F("x") == 1) == "#a2 = :v1"


def test_timestamps_formats() -> None:
    schema = schema_of(Profile)
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert schema.fields["created"].codec.encode(moment) == "2026-01-01T00:00:00.000000+00:00"
    assert schema.fields["updated"].codec.encode(moment) == 1767225600


class Visit(dynamoxy.PydanticModel, table="pydantic-visits", datetime_format="iso_z"):
    visitor_id: Annotated[str, dynamoxy.PartitionKey()]
    at: datetime
    history: list[datetime] = pydantic.Field(default_factory=list)
    created: Annotated[datetime | None, dynamoxy.CreatedAt()] = None
    updated: Annotated[datetime | None, dynamoxy.UpdatedAt("iso")] = None


def test_datetime_format_of_a_pydantic_model() -> None:
    moment = datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)
    zulu = "2026-10-13T08:48:47.966937Z"
    visit = Visit(visitor_id="v1", at=moment, history=[moment])
    assert visit.to_item() == {"visitor_id": "v1", "at": zulu, "history": [zulu]}
    schema = schema_of(Visit)
    assert schema.fields["created"].codec.encode(moment) == zulu
    assert schema.fields["updated"].codec.encode(moment) == "2026-10-13T08:48:47.966937+00:00", "explicit format"
    decoded = Visit.from_item({"visitor_id": "v1", "at": "2026-10-13T08:48:47.966937+00:00", "history": [zulu]})
    assert (decoded.at, decoded.history) == (moment, [moment])

    class Stamped(dynamoxy.PydanticModel, table="pydantic-stamped"):
        stamped_id: Annotated[str, dynamoxy.PartitionKey()]
        created: Annotated[datetime | None, dynamoxy.CreatedAt("iso_z")] = None

    assert schema_of(Stamped).fields["created"].codec.encode(moment) == zulu
