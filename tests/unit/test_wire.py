"""Conversions between Python values and DynamoDB values (native module)."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import dynamoxy
from dynamoxy import _core


def test_python_values_map_to_dynamodb_types() -> None:
    wire = dynamoxy.to_wire(
        {
            "s": "text",
            "n": 42,
            "f": 1.5,
            "d": Decimal("1.50"),
            "b": b"\x01",
            "ba": bytearray(b"\x02"),
            "mv": memoryview(b"\x03"),
            "t": True,
            "z": None,
            "l": [1, "a"],
            "tu": (1,),
            "m": {"k": "v"},
            "om": OrderedDict(k=1),
            "ss": {"a"},
            "ns": {1, Decimal("2.5")},
            "bs": frozenset({b"x"}),
            "big": 10**30,
        }
    )
    assert wire == {
        "s": {"S": "text"},
        "n": {"N": "42"},
        "f": {"N": "1.5"},
        "d": {"N": "1.5"},
        "b": {"B": b"\x01"},
        "ba": {"B": b"\x02"},
        "mv": {"B": b"\x03"},
        "t": {"BOOL": True},
        "z": {"NULL": True},
        "l": {"L": [{"N": "1"}, {"S": "a"}]},
        "tu": {"L": [{"N": "1"}]},
        "m": {"M": {"k": {"S": "v"}}},
        "om": {"M": {"k": {"N": "1"}}},
        "ss": {"SS": ["a"]},
        "ns": wire["ns"],
        "bs": {"BS": [b"x"]},
        "big": {"N": "1" + "0" * 30},
    }
    assert sorted(wire["ns"]["NS"]) == ["1", "2.5"]


def test_numbers_read_back_as_int_or_decimal() -> None:
    plain = dynamoxy.from_wire({"i": {"N": "7"}, "d": {"N": "7.25"}, "big": {"N": "1" + "0" * 30}, "neg": {"N": "-3"}})
    assert plain == {"i": 7, "d": Decimal("7.25"), "big": 10**30, "neg": -3}
    assert isinstance(plain["i"], int)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (float("nan"), "cannot be NaN"),
        (float("inf"), "cannot be NaN or infinite"),
        (Decimal("NaN"), "cannot be NaN"),
        (10**40 + 1, "38 significant digits"),
        (set(), "does not store empty sets"),
        ({1, "a"}, "same type"),
        ({None}, "sets may only contain"),
        (object(), "cannot store a value of type object"),
        ({1: "x"}, "attribute names must be str"),
    ],
)
def test_invalid_values_are_validation_errors(value: Any, message: str) -> None:
    with pytest.raises(dynamoxy.ValidationError, match=message):
        dynamoxy.to_wire({"value": value})


def nested(levels: int, inner: Any = 1, wrap: Callable[[Any], Any] = lambda value: [value]) -> Any:
    """``inner`` inside ``levels`` lists (or what ``wrap`` builds)."""
    value = inner
    for _ in range(levels):
        value = wrap(value)
    return value


def wire_nested(levels: int) -> dict[str, Any]:
    return nested(levels, {"N": "1"}, lambda value: {"L": [value]})


def test_values_nest_at_most_32_levels_deep() -> None:
    # Like DynamoDB: an attribute's value is 1 level deep, and one inside 31 lists is 32.
    assert dynamoxy.from_wire(dynamoxy.to_wire({"v": nested(31)})) == {"v": nested(31)}
    assert _core.value_from_wire(wire_nested(31)) == nested(31)
    # The elements of a set are no level of their own.
    assert dynamoxy.from_wire(dynamoxy.to_wire({"v": nested(31, {"a"})})) == {"v": nested(31, {"a"})}
    message = "Nesting Levels have exceeded supported limits"
    with pytest.raises(dynamoxy.ValidationError, match=message):
        dynamoxy.to_wire({"v": nested(32)})
    with pytest.raises(dynamoxy.ValidationError, match=message):
        dynamoxy.from_wire({"v": wire_nested(32)})


def test_deep_and_cyclic_values_raise_instead_of_crashing() -> None:
    # The conversions stop at DynamoDB's limit: deeper values used to exhaust the stack.
    cyclic: dict[str, Any] = {"pk": "a"}
    cyclic["self"] = cyclic
    loop: list[Any] = []
    loop.append(loop)
    deep_tuple = nested(1000, wrap=lambda value: (value,))
    deep_set = nested(1000, wrap=lambda value: frozenset({value}))
    for value in ({"v": nested(100_000)}, cyclic, {"v": loop}, {"v": {deep_tuple}}, {"v": deep_set}):
        with pytest.raises(dynamoxy.ValidationError, match="Nesting Levels"):
            dynamoxy.to_wire(value)
    with pytest.raises(dynamoxy.ValidationError, match="Nesting Levels"):
        _core.value_from_wire(wire_nested(100_000))
    with pytest.raises(dynamoxy.ValidationError, match="Nesting Levels"):
        _core._check_expressions(condition="a = :v", values={":v": nested(100_000)})


def test_number_sets_deduplicate_equal_numbers() -> None:
    assert dynamoxy.to_wire({"ns": {1, 1.0, Decimal("1.00")}})["ns"] == {"NS": ["1"]}  # noqa: B033
    # Equal as DynamoDB numbers, not in Python (0.1 is not exactly a tenth).
    assert dynamoxy.to_wire({"ns": {0.1, Decimal("0.1"), Decimal("0.10")}})["ns"] == {"NS": ["0.1"]}
    # A set of 60,000 numbers (about 210 KB, an item DynamoDB accepts) took seconds to deduplicate.
    # Python keeps a float and a Decimal of each tenth apart; DynamoDB stores one number for both.
    tenths = [f"{index}.1" for index in range(30_000)]
    numbers = {*map(float, tenths), *map(Decimal, tenths)}
    assert len(numbers) == 60_000
    assert sorted(_core.value_to_wire(numbers)["NS"], key=Decimal) == tenths


def test_base64_binary_for_json() -> None:
    wire = dynamoxy.to_wire({"b": b"foobar", "bs": {b"\x00\xff"}}, base64_binary=True)
    assert wire == {"b": {"B": "Zm9vYmFy"}, "bs": {"BS": ["AP8="]}}
    assert dynamoxy.from_wire(wire) == {"b": b"foobar", "bs": {b"\x00\xff"}}


def test_single_values() -> None:
    assert _core.value_to_wire([1, None]) == {"L": [{"N": "1"}, {"NULL": True}]}
    assert _core.value_from_wire({"SS": ["a", "b"]}) == {"a", "b"}
    with pytest.raises(dynamoxy.ValidationError, match="typed DynamoDB value"):
        _core.value_from_wire({"X": 1})
    with pytest.raises(dynamoxy.ValidationError, match="base64"):
        _core.value_from_wire({"B": "not base64!"})


plain_values = st.recursive(
    st.one_of(
        st.text(max_size=8),
        st.integers(-(10**15), 10**15),
        st.booleans(),
        st.none(),
        st.binary(max_size=8),
        st.decimals(allow_nan=False, allow_infinity=False, places=4, min_value=-(10**10), max_value=10**10),
        st.frozensets(st.text(max_size=4), min_size=1, max_size=4).map(set),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(st.text(min_size=1, max_size=5), children, max_size=4)
    ),
    max_leaves=10,
)


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, list):
        return [normalize(element) for element in value]
    if isinstance(value, dict):
        return {key: normalize(element) for key, element in value.items()}
    return value


@given(st.dictionaries(st.text(min_size=1, max_size=6), plain_values, max_size=6))
def test_round_trips_through_the_wire_format(item: dict[str, Any]) -> None:
    assert dynamoxy.from_wire(dynamoxy.to_wire(item)) == normalize(item)
    assert dynamoxy.from_wire(dynamoxy.to_wire(item, base64_binary=True)) == normalize(item)


def test_reserved_words() -> None:
    assert _core.is_reserved_word("name")
    assert _core.is_reserved_word("Status")
    assert not _core.is_reserved_word("email")


def test_matches_boto3_type_serializer() -> None:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    item = {
        "s": "x",
        "n": Decimal("1.5"),
        "i": 10,
        "b": b"\x00",
        "t": True,
        "z": None,
        "l": [Decimal(1), "a", [b"\x01"]],
        "m": {"k": {"k2": "v"}},
        "ss": {"a", "b"},
        "ns": {1, 2},
    }
    expected: dict[str, Any] = {key: serializer.serialize(value) for key, value in item.items()}
    wire: dict[str, Any] = dynamoxy.to_wire(item)
    for key, kind in (("ss", "SS"), ("ns", "NS")):
        wire[key] = {kind: sorted(wire[key][kind])}
        expected[key] = {kind: sorted(expected[key][kind])}
    assert wire == expected
