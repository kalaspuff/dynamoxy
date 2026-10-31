"""Pydantic models: markers, keys, nested models and expressions."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import pydantic
import pytest

import dynamoxy
from dynamoxy import F, schema_of
from dynamoxy.codecs import DateTimeCodec, JsonCodec
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


class Mailbox(dynamoxy.PydanticModel, table="mailboxes"):
    mailbox_id: Annotated[str, dynamoxy.PartitionKey()]
    address: Address = Address(street="Main")
    folders: list[str] = ["inbox"]  # noqa: RUF012 - a field: pydantic copies its default per instance


def test_mutable_defaults_of_decoded_items_are_copies() -> None:
    read = [Mailbox.from_item({"mailbox_id": mailbox_id}) for mailbox_id in ("a", "b")]
    read[0].address.city = "Bergen"
    read[0].folders.append("spam")
    assert (read[1].address.city, read[1].folders) == ("Oslo", ["inbox"])
    assert (Mailbox(mailbox_id="c").address.city, Mailbox(mailbox_id="c").folders) == ("Oslo", ["inbox"])


class Preferences(pydantic.BaseModel):
    theme: str | None = "light"


class Settings(dynamoxy.PydanticModel, table="settings"):
    settings_id: Annotated[str, dynamoxy.PartitionKey()]
    nickname: str | None = "anon"
    roles: set[str] = pydantic.Field(default_factory=lambda: {"member"})
    preferences: Preferences = Preferences()


def test_none_and_empty_sets_read_back_as_written() -> None:
    settings = Settings(settings_id="s1", nickname=None, roles=set(), preferences=Preferences(theme=None))
    item = settings.to_item()
    assert item == {"settings_id": "s1", "preferences": {}}, "None and empty sets are stored as nothing"
    assert Settings.from_item(item) == settings, "and nothing reads as them, not as the defaults"
    # Items without the attributes read the same; a required field reads its default.
    old = Settings.from_item({"settings_id": "s2"})
    assert (old.nickname, old.roles, old.preferences.theme) == (None, set(), "light")


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


@pytest.mark.parametrize("name", ["key", "to_item", "is_loaded", "create", "get", "query", "save", "update", "delete"])
def test_fields_cannot_take_the_names_of_the_model_api(name: str) -> None:
    # As in native models: a field's path would replace the method (pydantic only warns).
    namespace = {"__annotations__": {"pk": Annotated[str, dynamoxy.PartitionKey()], name: str}}
    message = rf"Shadow\.{name}: the name is part of the model API \({name}\); .*dynamoxy\.AttributeName\('{name}'\)"
    with pytest.warns(UserWarning, match="shadows an attribute"), pytest.raises(TypeError, match=message):
        type("Shadow", (dynamoxy.PydanticModel,), namespace, table="shadow")


def test_existing_models_keep_api_names_through_aliases() -> None:
    class SearchRequest(pydantic.BaseModel):  # an API schema, with a field named like a model method
        search_id: str
        query: str

    with pytest.raises(TypeError, match=r"SearchRecord\.query: the name is part of the model API"):

        class SearchRecord(SearchRequest, dynamoxy.PydanticModel, table="searches"):
            search_id: Annotated[str, dynamoxy.PartitionKey()]

    class AliasedRequest(pydantic.BaseModel):  # the same schema: `query` in input and output
        model_config = pydantic.ConfigDict(populate_by_name=True)

        search_id: str
        search_query: str = pydantic.Field(alias="query")

    class AliasedRecord(AliasedRequest, dynamoxy.PydanticModel, table="searches"):
        search_id: Annotated[str, dynamoxy.PartitionKey()]
        search_query: Annotated[str, dynamoxy.AttributeName("query")] = pydantic.Field(alias="query")

    record = AliasedRecord.model_validate({"search_id": "s1", "query": "dynamodb"})
    assert record.model_dump(by_alias=True) == {"search_id": "s1", "query": "dynamodb"}
    assert record.to_item() == {"search_id": "s1", "query": "dynamodb"}
    assert AliasedRecord.from_item(record.to_item()) == record
    assert ExpressionBuilder().condition(dynamoxy.attr(AliasedRecord.search_query) == "x") == "#a0 = :v0"


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


class Reminder(dynamoxy.PydanticModel, table="reminders"):
    """Optional fields with a codec of their own (``UseCodec``) or a ``Literal`` of ``None``."""

    reminder_id: Annotated[str, dynamoxy.PartitionKey()]
    due: Annotated[datetime | None, dynamoxy.UseCodec(DateTimeCodec("iso_z"))] = None
    data: Annotated[dict[str, Any] | None, dynamoxy.UseCodec(JsonCodec())] = None
    state: Literal["on", "off", None] = None  # noqa: PYI061 - the spelling under test


def test_fields_with_a_codec_of_their_own_may_be_optional() -> None:
    reminder = Reminder(reminder_id="r1")
    schema = schema_of(Reminder)
    schema.validate(reminder)  # what every write checks
    assert all(spec.optional for name, spec in schema.fields.items() if name != "reminder_id")
    assert reminder.to_item() == {"reminder_id": "r1"}, "None is not stored"
    assert Reminder.from_item({"reminder_id": "r1"}) == reminder
    moment = datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)
    filled = Reminder(reminder_id="r1", due=moment, data={"a": [1]}, state="on")
    item = filled.to_item()
    assert item == {"reminder_id": "r1", "due": "2026-10-13T08:48:47.000000Z", "data": '{"a":[1]}', "state": "on"}
    assert Reminder.from_item(item) == filled


class Sighted(dynamoxy.PydanticModel, abstract=True):
    seen_at: datetime | None = None


class Bird(Sighted, table="pydantic-birds", datetime_format="iso_z"):
    bird_id: Annotated[str, dynamoxy.PartitionKey()]


def test_inherited_fields_encode_values_in_the_format_of_the_subclass() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    builder = ExpressionBuilder()
    builder.condition(dynamoxy.attr(Bird.seen_at) == moment)
    builder.update([dynamoxy.attr(Bird.seen_at).set(moment)])
    builder.condition(dynamoxy.attr(Sighted.seen_at) == moment)
    assert builder.values == {
        ":v0": "2026-01-01T00:00:00.000000Z",
        ":v1": "2026-01-01T00:00:00.000000Z",
        ":v2": "2026-01-01T00:00:00.000000+00:00",  # the base class keeps its format
    }


class Tracked(dynamoxy.PydanticModel, abstract=True):
    hits: int = 0


def test_paths_answer_class_access_outside_class_creation() -> None:
    seen: list[object] = []

    class Hooked(Tracked, abstract=True):
        @classmethod
        def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
            super().__pydantic_init_subclass__(**kwargs)
            seen.append(cls.hits)  # the path, once pydantic has collected the fields

    class Counter(Hooked, table="pydantic-counters"):
        counter_id: Annotated[str, dynamoxy.PartitionKey()]

    assert seen == [Counter.hits]
    assert repr(Counter.hits) == "<attribute Counter.hits>"
    namespace = {"__annotations__": {"broken_id": Annotated[str, dynamoxy.PartitionKey()], "save": int}}
    with pytest.warns(UserWarning, match="shadows an attribute"), pytest.raises(TypeError, match="model API"):
        type("Broken", (Tracked,), namespace, table="pydantic-broken")
    assert repr(Tracked.hits) == "<attribute Tracked.hits>", "a failed class creation leaves paths as they were"
    assert Counter.model_fields["hits"].default == 0


class Flock(dynamoxy.PydanticModel, table="pydantic-flocks"):
    flock_id: Annotated[str, dynamoxy.PartitionKey()]
    seen: list[datetime] = pydantic.Field(default_factory=list)


def test_contains_encodes_list_elements_as_stored() -> None:
    builder = ExpressionBuilder()
    builder.condition(dynamoxy.attr(Flock.seen).contains(datetime(2026, 1, 1, tzinfo=UTC)))
    assert builder.values == {":v0": "2026-01-01T00:00:00.000000+00:00"}


@pytest.mark.parametrize("installed", [True, False])
def test_star_imports_work_with_and_without_pydantic(installed: bool) -> None:
    script = textwrap.dedent(
        f"""
        import sys
        if not {installed}:
            sys.modules["pydantic"] = None  # as if it were not installed
        from dynamoxy import *
        import dynamoxy
        assert ("PydanticModel" in dir()) is {installed}
        assert ("PydanticModel" in dynamoxy.__all__) is {installed}
        assert Model is dynamoxy.Model
        if not {installed}:
            try:
                dynamoxy.PydanticModel
            except ImportError as error:
                assert "pip install 'dynamoxy[pydantic]'" in str(error)
            else:
                raise AssertionError("PydanticModel without pydantic")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


class Tree(dynamoxy.PydanticModel, table="trees"):
    """Recursive and nested models (core schema definitions), and a plain validator that takes info."""

    tree_id: Annotated[str, dynamoxy.PartitionKey(), pydantic.AfterValidator(str.upper)]
    parent: Tree | None = None
    address: Address | None = None
    label: Annotated[str, pydantic.PlainValidator(lambda value, info: f"{value}:{info.field_name}")] = ""

    @pydantic.model_validator(mode="after")
    def needs_label(self) -> Tree:
        if not self.label:
            raise ValueError("needs a label")
        return self

    @pydantic.model_validator(mode="before")
    @classmethod
    def needs_data(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("needs a dict")
        return data


def test_values_outside_instances_run_the_fields_schema() -> None:
    schema = schema_of(Tree)
    # Through the core schema's definitions, without the model validators (which need the data or
    # the instance): one of mode="after" wraps the model, one of mode="before" its fields.
    assert schema.key_from(("t1",), {}) == {"tree_id": "T1"}
    assert schema.fields["address"].validate({"street": "Main"}) == Address(street="Main")
    with pytest.raises(ValueError, match=r"street: Field required"):  # where, for nested values
        schema.fields["address"].validate({"city": "Oslo"})
    parent = Tree(tree_id="p", label="x")
    assert schema.fields["parent"].validate(parent) is parent
    # A plain validator that takes a ValidationInfo cannot be left out: the codec checks the value.
    assert schema.fields["label"].validate("x") == "x"
    with pytest.raises(ValueError, match="expected str"):
        schema.fields["label"].validate(5)


def test_core_schemas_of_another_layout_leave_the_type_check(monkeypatch: pytest.MonkeyPatch) -> None:
    class Note(dynamoxy.PydanticModel, table="notes"):
        note_id: Annotated[str, dynamoxy.PartitionKey(), pydantic.AfterValidator(str.upper)]

    monkeypatch.setattr(Note, "__pydantic_core_schema__", {"type": "model"})  # no "schema"
    assert schema_of(Note).key_from(("n1",), {}) == {"note_id": "n1"}


def test_pydantic_errors_of_values_outside_instances_say_where() -> None:
    class Shelf(dynamoxy.PydanticModel, table="shelves"):
        shelf_id: Annotated[str, dynamoxy.PartitionKey()]
        sizes: list[Annotated[int, pydantic.Field(ge=0)]] = []  # noqa: RUF012 - a field: pydantic copies its default

    with pytest.raises(ValueError, match=r"^1: Input should be greater than or equal to 0$"):
        schema_of(Shelf).fields["sizes"].validate([1, -1])
