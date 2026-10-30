"""Native models: declaration, validation, encoding, keys and schema errors (no I/O)."""

from __future__ import annotations

import copy
import sys
import textwrap
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

import dynamoxy
from dynamoxy import (
    Attr,
    Document,
    Model,
    created_at,
    field,
    partition_key,
    schema_of,
    sort_key,
    ttl,
    updated_at,
)
from dynamoxy.constraints import guard_key
from dynamoxy.expressions import ExpressionBuilder
from tests.models import Customer, Event, LineItem, Order, OrderStatus, Status, User


class Price(Model, table="prices"):
    sku: Attr[str] = partition_key()
    sk: Attr[str] = sort_key(template="PRICE#{amount}#{at:%Y-%m-%d}")
    amount: Attr[Decimal]
    at: Attr[datetime]
    seat: Attr[Decimal | None] = field(default=None, unique=True)


def make_user(**changes: Any) -> User:
    values: dict[str, Any] = {"user_id": "u1", "email": "ada@example.com", "name": "Ada"}
    values.update(changes)
    return User(**values)


def test_construction_defaults_and_repr() -> None:
    user = make_user()
    assert (user.visits, user.tags, user.status, user.age) == (0, set(), Status.ACTIVE, None)
    assert (user.created_at, user.version) == (None, None), "automatic fields are set on write"
    assert repr(user).startswith("User(user_id='u1', email='ada@example.com'")
    assert user == make_user()
    assert user != make_user(name="Bob")
    with pytest.raises(TypeError):
        hash(user)


def test_validation_collects_every_field_error() -> None:
    with pytest.raises(dynamoxy.ValidationError) as caught:
        User(user_id="u1", email="not-an-email", name="", age=-1)  # type: ignore[call-arg]
    errors = {error.path: error.message for error in caught.value.errors}
    assert set(errors) == {"email", "name", "age"}
    assert "pattern" in errors["email"]
    assert "at least 1" in errors["name"]
    assert "greater than or equal to 0" in errors["age"]


def test_constructor_rejects_unknown_and_automatic_fields() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'nickname'"):
        make_user(nickname="x")
    with pytest.raises(TypeError, match="set automatically"):
        make_user(version=3)
    with pytest.raises(dynamoxy.ValidationError, match="name: is required"):
        User(user_id="u1", email="a@b.c")  # type: ignore[call-arg]


def test_assignment_validates() -> None:
    user = make_user()
    user.age = 30
    with pytest.raises(dynamoxy.ValidationError, match="expected int"):
        user.age = "30"  # type: ignore[assignment]
    with pytest.raises(dynamoxy.ValidationError, match="must not be None"):
        user.name = None  # type: ignore[assignment]
    user.status = "suspended"  # type: ignore[assignment]
    assert user.status is Status.SUSPENDED
    with pytest.raises(AttributeError, match="cannot delete"):
        del user.name


def test_encoding_round_trip_and_stored_form() -> None:
    user = make_user(tags={"a"}, age=7)
    item = user.to_item()
    assert item == {
        "user_id": "u1",
        "email": "ada@example.com",
        "name": "Ada",
        "age": 7,
        "visits": 0,
        "tags": {"a"},
        "status": "active",
    }
    assert User.from_item(item) == user


def test_documents_nest_as_maps() -> None:
    order = Order(
        customer_id="c1",
        order_id="o1",
        placed_at=datetime(2026, 1, 1, tzinfo=UTC),
        total=Decimal("9.50"),
        lines=[LineItem(sku="A", quantity=2, price=Decimal("4.75"))],
    )
    item = order.to_item()
    assert item["lines"] == [{"sku": "A", "quantity": 2, "price": Decimal("4.75")}]
    assert item["pk"] == "CUSTOMER#c1"
    assert item["sk"] == "ORDER#o1"
    assert item["gsi1pk"] == "STATUS#new"
    assert item["gsi1sk"] == "2026-01-01T00:00:00.000000+00:00"
    assert item["_type"] == "Order"
    decoded = Order.from_item(item)
    assert decoded.lines == [LineItem(sku="A", quantity=2, price=Decimal("4.75"))]
    assert decoded.to_dict()["lines"] == [{"sku": "A", "quantity": 2, "price": Decimal("4.75")}]
    with pytest.raises(dynamoxy.ValidationError, match="quantity"):
        LineItem(sku="A", quantity=0, price=Decimal(1))


def test_derived_fields_cannot_be_assigned() -> None:
    order = Order(customer_id="c1", order_id="o1", placed_at=datetime(2026, 1, 1, tzinfo=UTC), total=Decimal(1))
    with pytest.raises(AttributeError, match="derived from"):
        order.pk = "x"
    with pytest.raises(TypeError, match="set automatically"):
        Order(pk="x", customer_id="c1", order_id="o1", placed_at=datetime.now(UTC), total=Decimal(1))  # type: ignore[call-arg]


def test_keys_from_values_and_instances() -> None:
    assert User.key("u1") == {"user_id": "u1"}
    assert User.key(user_id="u1") == {"user_id": "u1"}
    assert Order.key(customer_id="c1", order_id="o1") == {"pk": "CUSTOMER#c1", "sk": "ORDER#o1"}
    assert Order.key(pk="CUSTOMER#c1", sk="ORDER#o1") == {"pk": "CUSTOMER#c1", "sk": "ORDER#o1"}
    assert Customer.key(customer_id="c1") == {"pk": "CUSTOMER#c1", "sk": "PROFILE"}
    assert Event.key("s", 3) == {"stream": "s", "seq": 3}
    with pytest.raises(ValueError, match=r"the key needs sk=\.\.\."):
        Order.key(customer_id="c1")
    with pytest.raises(ValueError, match="unexpected key arguments"):
        User.key("u1", extra=1)
    with pytest.raises(ValueError, match="expected int"):
        Event.key("s", "three")
    assert make_user().primary_key() == {"user_id": "u1"}


def test_schema_describes_the_model() -> None:
    schema = schema_of(User)
    assert schema.table == "users"
    assert schema.partition_key is not None
    assert schema.partition_key.name == "user_id"
    assert [spec.name for spec in schema.unique_fields] == ["email"]
    assert schema.version_field is not None
    assert schema.created_at_field is not None
    assert set(schema.indexes) == {"by_email", "by_status"}
    assert schema.indexes["by_status"].projection.type == "KEYS_ONLY"
    assert schema_of(Event).indexes["by_kind_local"].kind == "local"
    assert schema_of(Event).ttl_field is not None
    assert schema_of(Event).ttl_field.codec.encode(datetime(2026, 1, 1, tzinfo=UTC)) == 1767225600  # type: ignore[union-attr]
    assert schema_of(Order).type_attribute == "_type"
    assert schema_of(Order).entity == "Order"


def test_schema_errors_are_explained() -> None:
    class NoKey(Model, table="broken"):
        name: Attr[str]

    with pytest.raises(TypeError, match="exactly one partition key"):
        schema_of(NoKey)

    class BadTemplate(Model, table="broken"):
        pk: Attr[str] = partition_key(template="X#{missing}")

    with pytest.raises(TypeError, match="refers to 'missing'"):
        schema_of(BadTemplate)

    class ListKey(Model, table="broken"):
        pk: Attr[list[str]] = partition_key()

    with pytest.raises(TypeError, match="key attributes must be stored as text, numbers or bytes"):
        schema_of(ListKey)

    class TwoSorts(Model, table="broken"):
        pk: Attr[str] = partition_key()
        a: Attr[str] = sort_key()
        b: Attr[str] = sort_key()

    with pytest.raises(TypeError, match="2 sort key fields"):
        schema_of(TwoSorts)

    class Clash(Model, table="broken"):
        pk: Attr[str] = partition_key()
        a: Attr[str] = field(attribute_name="x")
        b: Attr[str] = field(attribute_name="x")

    with pytest.raises(TypeError, match="both stored as 'x'"):
        schema_of(Clash)

    with pytest.raises(TypeError, match="cannot have a default"):
        field(template="X#{a}", default="y")  # type: ignore[call-overload]


def test_inheritance_and_abstract_models() -> None:
    class Base(Model, abstract=True):
        created: Attr[str] = field(default="now")

    class Thing(Base, table="things"):
        thing_id: Attr[str] = partition_key()

    thing = Thing(thing_id="t1")
    assert thing.to_item() == {"thing_id": "t1", "created": "now"}
    assert not schema_of(Base).is_table


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Python 3.14 evaluates annotations lazily (PEP 649)")
def test_lazy_annotations_may_refer_to_classes_declared_later() -> None:
    # This module's annotations are strings (`from __future__ import annotations`); compile the
    # models without inheriting that flag, so that theirs are evaluated lazily.
    source = textwrap.dedent(
        """
        from dynamoxy import Attr, Document, Model, field, partition_key

        class Shipment(Model, table="shipments"):
            shipment_id: Attr[str] = partition_key()
            address: Attr[Address | None] = field(default=None)

        class Address(Document):
            city: Attr[str]
        """
    )
    namespace: dict[str, Any] = {}
    exec(compile(source, "<lazy annotations>", "exec", dont_inherit=True), namespace)
    shipment = namespace["Shipment"](shipment_id="s1", address=namespace["Address"](city="Lund"))
    assert shipment.to_item() == {"shipment_id": "s1", "address": {"city": "Lund"}}


def test_attribute_names_and_custom_codecs() -> None:
    class Stored(Model, table="stored"):
        name: Attr[str] = partition_key(attribute_name="PK")
        data: Attr[dict[str, Any]] = field(default_factory=dict, codec=dynamoxy.codecs.JsonCodec(), attribute_name="d")

    item = Stored(name="k", data={"x": [1, 2]}).to_item()
    assert item == {"PK": "k", "d": '{"x":[1,2]}'}
    assert Stored.from_item(item).data == {"x": [1, 2]}
    assert str(Stored.name) == "PK"
    assert Stored.key("k") == {"PK": "k"}


def test_fields_must_not_shadow_the_model_api() -> None:
    with pytest.raises(TypeError, match=r"attribute_name='key'"):

        class Shadow(Model, table="shadow"):
            key: Attr[str] = partition_key()  # pyrefly: ignore[bad-override]


@pytest.mark.parametrize("name", ["save", "delete", "update", "add", "increment", "decrement", "refresh"])
def test_persistence_methods_are_part_of_the_model_api(name: str) -> None:
    namespace = {"__annotations__": {"pk": Attr[str], name: Attr[int]}, "pk": partition_key()}
    with pytest.raises(TypeError, match=rf"Shadow\.{name}: the name is part of the model API"):
        type("Shadow", (Model,), namespace, table="shadow")


def test_documents_may_use_the_names_of_model_methods() -> None:
    class Change(Document):
        add: Attr[list[str]] = field(default_factory=list)
        delete: Attr[list[str]] = field(default_factory=list)
        key: Attr[str | None] = field(default=None)

    change = Change(add=["a"], key="k")
    assert (change.add, change.delete, change.key) == (["a"], [], "k")
    with pytest.raises(TypeError, match=r"Patch\.copy: the name is part of the model API"):

        class Patch(Document):
            copy: Attr[str]  # pyrefly: ignore[bad-override]


def test_item_decode_errors_name_the_attribute() -> None:
    with pytest.raises(dynamoxy.ItemDecodeError, match="no 'name' attribute") as caught:
        User.from_item({"user_id": "u1", "email": "a@b.c"})
    assert caught.value.key == {"user_id": "u1"}
    with pytest.raises(dynamoxy.ItemDecodeError, match="cannot decode"):
        User.from_item({"user_id": "u1", "email": "a@b.c", "name": "x", "status": "deleted"})


def test_messages_leave_values_and_keys_out() -> None:
    # Values and keys may be personal data, and messages end up in logs: the errors hold them.
    with pytest.raises(dynamoxy.ValidationError) as caught:
        User(user_id="u1", email=4155550123, name="Ada", status="on-leave")  # type: ignore[arg-type]
    shown = f"{caught.value} {caught.value.errors!r}"
    assert "4155550123" not in shown
    assert "on-leave" not in shown
    assert "email: expected str, got int" in shown
    assert "status: expected a Status: one of 'active', 'suspended' (got str)" in shown
    assert {error.path: error.value for error in caught.value.errors} == {"email": 4155550123, "status": "on-leave"}

    with pytest.raises(dynamoxy.ItemDecodeError) as decode:
        User.from_item({"user_id": "ada@example.com", "email": "a@b.c", "name": "x", "status": "on-leave"})
    shown = f"{decode.value} {decode.value.errors!r}"
    assert "ada@example.com" not in shown
    assert "on-leave" not in shown
    assert "status: cannot decode a stored str as Status" in shown
    assert decode.value.key == {"user_id": "ada@example.com"}
    assert decode.value.errors[0].value == "on-leave"

    with pytest.raises(ValueError, match="unexpected key arguments 1 more positional value") as extra:
        User.key("u1", "ada@example.com")
    assert "ada@example.com" not in str(extra.value)


def test_validators_and_hooks_are_collected() -> None:
    class Checked(Model, table="checked"):
        pk: Attr[str] = partition_key()
        low: Attr[int]
        high: Attr[int]

        @dynamoxy.validator
        def ordered(self) -> None:
            if self.low > self.high:
                raise ValueError("low must not exceed high")

        @dynamoxy.before_save
        def normalize(self) -> None: ...

    with pytest.raises(dynamoxy.ValidationError, match="low must not exceed high"):
        Checked(pk="a", low=2, high=1)
    schema = schema_of(Checked)
    assert len(schema.validators) == 1
    assert [hook.__name__ for hook in schema.hooks["before_save"]] == ["normalize"]


def test_copy_is_not_loaded() -> None:
    user = make_user()
    clone = user.copy(name="Bob")
    assert clone.name == "Bob"
    assert user.name == "Ada"
    assert not clone.is_loaded


def test_copy_starts_automatic_fields_empty() -> None:
    stored = User.from_item(
        {
            "user_id": "u1",
            "email": "ada@example.com",
            "name": "Ada",
            "created_at": "2026-01-01T00:00:00.000000+00:00",
            "updated_at": "2026-01-02T00:00:00.000000+00:00",
            "version": 3,
        }
    )
    clone = stored.copy(name="Bob")
    assert (clone.created_at, clone.updated_at, clone.version) == (None, None, None)
    assert (clone.user_id, clone.name) == ("u1", "Bob")
    assert (stored.version, stored.name) == (3, "Ada"), "the original keeps its values"
    assert stored.copy(version=3).version == 3, "unless they are given"


def test_copy_refuses_unknown_fields() -> None:
    with pytest.raises(TypeError, match="unexpected field"):
        make_user().copy(nmae="Bob")


def test_key_templates_render_what_is_stored() -> None:
    local = timezone(timedelta(hours=2))
    price = Price(sku="a", amount=Decimal("1.50"), at=datetime(2026, 1, 1, 0, 30, tzinfo=local))
    item = price.to_item()
    assert item["sk"] == "PRICE#1.5#2025-12-31", "the number as DynamoDB stores it, the date in UTC"
    assert Price.from_item({**item, "amount": Decimal("1.5")}).to_item()["sk"] == item["sk"], "read back: the same key"


def test_guard_keys_use_the_stored_numbers() -> None:
    schema = schema_of(Price)
    spec = schema.fields["seat"]
    keys = {guard_key(schema, spec, value)["sku"] for value in (Decimal("5.0"), Decimal(5), 5, 5.0, Decimal("5E0"))}
    assert keys == {"_unique#Price#seat#5"}
    assert guard_key(schema, spec, 1e16)["sku"] == "_unique#Price#seat#10000000000000000"


def test_repository_subclasses_inherit_their_model() -> None:
    class UserRepository(dynamoxy.Repository[User]):
        pass

    class AuditedUserRepository(UserRepository):
        pass

    class Explicit(dynamoxy.Repository[User]):
        model = User

    class ExplicitChild(Explicit):
        pass

    class Shared[M](dynamoxy.Repository[M]):
        """Helpers shared by the repositories of several models."""

    class SharedUsers(Shared[User]):
        pass

    client = dynamoxy.testing.memory_client()
    for repository in (UserRepository, AuditedUserRepository, Explicit, ExplicitChild, SharedUsers):
        assert repository(client).model_class is User
    with pytest.raises(TypeError, match="does not know its model"):
        Shared(client)


def test_python_copies_have_their_own_persistence_state() -> None:
    stored = User.from_item({"user_id": "u1", "email": "ada@example.com", "name": "Ada", "version": 3})
    for duplicate in (copy.copy(stored), copy.deepcopy(stored)):
        assert duplicate == stored
        assert duplicate.is_loaded, "an exact duplicate, loaded like the original"
        assert duplicate.__dict__["__dynamoxy_state__"] is not stored.__dict__["__dynamoxy_state__"]


def test_documents_accept_dicts() -> None:
    class Wrapper(Model, table="wrappers"):
        pk: Attr[str] = partition_key()
        line: Attr[LineItem]

    wrapper = Wrapper(pk="p", line={"sku": "S", "quantity": 1, "price": Decimal(2)})  # type: ignore[arg-type]
    assert isinstance(wrapper.line, LineItem)


def test_enums_and_statuses_in_templates() -> None:
    order = Order(
        customer_id="c",
        order_id="o",
        placed_at=datetime(2026, 1, 1, tzinfo=UTC),
        total=Decimal(0),
        status=OrderStatus.PAID,
    )
    assert order.to_item()["gsi1pk"] == "STATUS#paid"


class Plain(Document):
    note: Attr[str]


def test_document_has_no_table() -> None:
    assert not schema_of(Plain).is_table
    with pytest.raises(TypeError, match="not stored in a table"):
        schema_of(Plain).key_from((), {})


class Stamp(Document, datetime_format="iso_z"):
    at: Attr[datetime]


class Note(Document):
    at: Attr[datetime]


class ZuluBase(Model, abstract=True, datetime_format="iso_z"):
    created_at: Attr[datetime] = created_at()
    updated_at: Attr[datetime] = updated_at()


class Visit(ZuluBase, table="visits"):
    visitor_id: Attr[str] = partition_key()
    at: Attr[datetime] = sort_key()
    left_at: Attr[datetime | None] = field(default=None)
    history: Attr[list[datetime]] = field(default_factory=list)
    legacy_at: Attr[datetime] = field(codec=dynamoxy.codecs.DateTimeCodec("iso"))
    expires_at: Attr[datetime | None] = ttl(default=None)
    day: Attr[str] = field(template="DAY#{at}")
    stamp: Attr[Stamp | None] = field(default=None)
    note: Attr[Note | None] = field(default=None)


def test_datetime_format_of_a_model() -> None:
    moment = datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)
    zulu = "2026-10-13T08:48:47.966937Z"
    visit = Visit(
        visitor_id="v1",
        at=moment,
        left_at=moment,
        history=[moment],
        legacy_at=moment,
        expires_at=moment,
        stamp=Stamp(at=moment),
        note=Note(at=moment),
    )
    assert visit.to_item() == {
        "visitor_id": "v1",
        "at": zulu,
        "left_at": zulu,
        "history": [zulu],
        "legacy_at": "2026-10-13T08:48:47.966937+00:00",  # an explicit codec wins
        "expires_at": 1791881327,  # TTL attributes are always epoch seconds
        "day": f"DAY#{zulu}",
        "stamp": {"at": zulu},
        "note": {"at": "2026-10-13T08:48:47.966937+00:00"},  # documents choose their own format
    }
    schema = schema_of(Visit)
    assert schema.options.datetime_format == "iso_z"
    assert schema.fields["created_at"].codec.encode(moment) == zulu
    assert schema.fields["updated_at"].codec.encode(moment) == zulu
    assert Visit.key("v1", moment) == {"visitor_id": "v1", "at": zulu}
    builder = ExpressionBuilder()
    builder.condition(Visit.at.between(moment, moment))
    assert builder.values == {":v0": zulu, ":v1": zulu}
    assert Visit.from_item(visit.to_item()).to_item() == visit.to_item()


def test_items_in_either_iso_style_decode() -> None:
    moment = datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)
    offset, zulu = "2026-10-13T08:48:47.000000+00:00", "2026-10-13T08:48:47Z"
    visit = Visit.from_item(
        {"visitor_id": "v1", "at": offset, "legacy_at": zulu, "created_at": zulu, "note": {"at": zulu}}
    )
    assert (visit.at, visit.legacy_at, visit.created_at) == (moment, moment, moment)
    assert visit.note == Note(at=moment)


def test_datetime_format_is_inherited_and_overridable() -> None:
    class Legacy(ZuluBase, table="legacy", datetime_format="iso"):
        legacy_id: Attr[str] = partition_key()
        at: Attr[datetime]

    class Explicit(ZuluBase, table="explicit"):
        explicit_id: Attr[str] = partition_key()
        created_at: Attr[datetime] = created_at(format="epoch_ms")

    moment = datetime(2026, 1, 1, tzinfo=UTC)
    legacy = schema_of(Legacy)
    assert legacy.fields["at"].codec.encode(moment) == "2026-01-01T00:00:00.000000+00:00"
    assert legacy.fields["created_at"].codec.encode(moment) == "2026-01-01T00:00:00.000000+00:00"
    explicit = schema_of(Explicit)
    assert explicit.fields["created_at"].codec.encode(moment) == 1767225600000
    assert explicit.fields["updated_at"].codec.encode(moment) == "2026-01-01T00:00:00.000000Z"
    assert schema_of(Order).fields["placed_at"].codec.encode(moment) == "2026-01-01T00:00:00.000000+00:00"


def test_invalid_datetime_formats_fail_on_declaration() -> None:
    expected = "datetime_format must be one of 'iso', 'iso_z', 'epoch', 'epoch_ms', got 'weekly'"
    with pytest.raises(ValueError, match=expected):

        class Weekly(Model, table="weekly", datetime_format="weekly"):  # type: ignore[arg-type]
            weekly_id: Attr[str] = partition_key()

    with pytest.raises(ValueError, match=expected):

        class WeeklyDocument(Document, datetime_format="weekly"):  # type: ignore[arg-type]
            at: Attr[datetime]

    with pytest.raises(ValueError, match="format must be one of"):
        created_at(format="weekly")  # type: ignore[arg-type]
