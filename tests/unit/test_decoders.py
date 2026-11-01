"""The decoder generated per model reads every item exactly as the slow path does.

``ModelSchema._values_of`` decodes an item in one pass, and hands items it cannot decode to
``ModelSchema._decoded_values``, which reports every problem. Both must agree: same values for
items that decode, and an ``ItemDecodeError`` from the slow path whenever the generated decoder
gives up.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import Field

import dynamoxy
from dynamoxy import Attr, Document, Model, PartitionKey, PydanticModel, field, partition_key, sort_key, version
from dynamoxy.schema import ModelSchema, schema_of
from tests.models import Order, Player, User


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Point(Document):
    x: Attr[int]
    y: Attr[int] = field(default=0)


class Everything(Model, table="decoders-everything"):
    pk: Attr[str] = partition_key()
    n: Attr[int] = sort_key()
    text: Attr[str] = field(default="")
    flag: Attr[bool | None] = field(default=None)
    amount: Attr[Decimal] = field(default=Decimal(0))
    ratio: Attr[float] = field(default=0.0)
    raw: Attr[bytes | None] = field(default=None)
    day: Attr[date | None] = field(default=None)
    at: Attr[datetime | None] = field(default=None)
    ident: Attr[UUID | None] = field(default=None)
    color: Attr[Color] = field(default=Color.RED)
    names: Attr[set[str]] = field(default_factory=set)
    numbers: Attr[set[int] | None] = field(default=None)
    items: Attr[list[int]] = field(default_factory=list)
    mapping: Attr[dict[str, str]] = field(default_factory=dict)
    point: Attr[Point | None] = field(default=None)
    anything: Attr[Any] = field(default=None)
    stored_as: Attr[str] = field(default="x", attribute_name="other-name")
    required: Attr[str]
    version: Attr[str] = version()


class Empty(Model, abstract=True):
    pass


class PydanticThing(PydanticModel, table="decoders-pydantic"):
    pk: Annotated[str, PartitionKey()]
    count: int = 0
    tags: set[str] = Field(default_factory=set)
    label: str | None = None
    doubled: int = Field(default_factory=lambda data: data["count"] * 2)
    required: str


_values = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=5),
    st.sampled_from(["red", "blue", "2026-01-02", "2026-01-02T03:04:05+00:00", "12345678-1234-1234-1234-123456789abc"]),
    st.integers(-5, 5),
    st.decimals(allow_nan=False, allow_infinity=False, places=2, min_value=-10, max_value=10),
    st.binary(max_size=3),
    st.frozensets(st.text(max_size=2), max_size=2).map(set),
    st.frozensets(st.integers(0, 3), min_size=1, max_size=2).map(set),
    st.lists(st.integers(0, 3), max_size=2),
    st.dictionaries(st.text(max_size=2), st.text(max_size=2), max_size=2),
    st.fixed_dictionaries({"x": st.integers(0, 3)}),
)


#: An item of each model that decodes; the strategy changes, adds and removes attributes of it.
_VALID: dict[type, dict[str, Any]] = {
    Everything: {"pk": "a", "n": 1, "required": "r", "color": "blue", "point": {"x": 1}, "names": {"a"}},
    User: {"user_id": "u", "email": "a@b.c", "name": "n", "created_at": "2026-01-02T03:04:05+00:00"},
    Order: {"pk": "CUSTOMER#c", "sk": "ORDER#o", "customer_id": "c", "order_id": "o", "total": Decimal(3)}
    | {"placed_at": "2026-01-02T03:04:05+00:00", "lines": [{"sku": "s", "quantity": 1, "price": Decimal(1)}]},
    Player: {"player_id": "p", "name": "n", "score": 3, "rank": "SCORE#00000003", "stats": {"a": 1}},
    PydanticThing: {"pk": "a", "required": "r", "count": 2, "tags": {"t"}},
}

_REMOVED = object()


@st.composite
def _items(draw: st.DrawFn, model: type) -> dict[str, Any]:
    schema = schema_of(model)
    attributes = [*schema.attributes, "extra", "_type"]
    changes = draw(st.dictionaries(st.sampled_from(attributes), st.one_of(st.just(_REMOVED), _values), max_size=3))
    item = dict(_VALID[model])
    for attribute, value in changes.items():
        if value is _REMOVED:
            item.pop(attribute, None)
        else:
            item[attribute] = value
    return item


def _check_agreement(schema: ModelSchema, item: dict[str, Any]) -> None:
    nested: str | None = None
    try:
        fast = schema._values_of(item)
    except dynamoxy.ItemDecodeError as error:  # a nested document that does not decode: both raise it
        nested = str(error)
    if nested is not None:
        with pytest.raises(dynamoxy.ItemDecodeError) as caught:
            schema._decoded_values(item)
        assert str(caught.value) == nested
        return
    try:
        slow = schema._decoded_values(item)
    except dynamoxy.ItemDecodeError:
        assert fast is None, f"the generated decoder read {item!r}, which the slow path refuses"
        return
    assert fast is not None, f"the generated decoder gave up on {item!r}, which the slow path reads"
    assert fast == slow
    assert list(fast) == [name for name in schema.fields if name in fast]  # field order


@pytest.mark.parametrize("model", [Everything, User, Order, Player, PydanticThing])
@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_the_generated_decoder_agrees_with_the_slow_path(model: type, data: st.DataObject) -> None:
    _check_agreement(schema_of(model), data.draw(_items(model)))


@pytest.mark.parametrize("model", list(_VALID))
def test_the_valid_items_decode(model: type) -> None:
    assert schema_of(model)._values_of(_VALID[model]) is not None


def test_the_generated_decoder_applies_the_missing_attribute_rule() -> None:
    schema = schema_of(Everything)
    values = schema._values_of({"pk": "a", "n": 1, "required": "r"})
    assert values is not None
    assert values["text"] == ""
    assert values["flag"] is None
    assert values["numbers"] is None
    assert values["names"] == set()
    assert values["items"] == []
    assert values["mapping"] == {}
    assert values["version"] is None
    other = schema._values_of({"pk": "b", "n": 2, "required": "r"})
    assert other is not None
    assert other["names"] is not values["names"]
    assert other["items"] is not values["items"]
    assert schema._values_of({"pk": "a", "n": 1}) is None  # required attribute missing
    assert schema._values_of({"pk": "a", "n": Decimal("1.5"), "required": "r"}) is None  # does not decode


def test_a_default_factory_that_takes_data_is_left_to_pydantic() -> None:
    schema = schema_of(PydanticThing)
    values = schema._values_of({"pk": "a", "count": 3, "required": "r"})
    assert values is not None
    assert "doubled" not in values
    assert PydanticThing.from_item({"pk": "a", "count": 3, "required": "r"}).doubled == 6


def test_a_model_without_fields_gets_a_decoder() -> None:
    assert schema_of(Empty)._values_of({"anything": 1}) == {}


def test_decoded_pydantic_instances_are_loaded_and_validated() -> None:
    thing = PydanticThing.from_item({"pk": "a", "count": 2, "required": "r", "tags": {"t"}})
    assert thing.is_loaded
    assert thing.model_fields_set >= {"pk", "count", "required", "tags"}
    with pytest.raises(dynamoxy.ItemDecodeError, match="cannot decode PydanticThing") as caught:
        PydanticThing.from_item({"pk": "a", "count": True, "required": "r"})
    assert "True" not in str(caught.value)  # pydantic's message without the input
