"""Codecs: how Python types are stored, validated and read back."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dynamoxy import codecs
from dynamoxy.codecs import resolve_codec


class Color(enum.StrEnum):
    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


def round_trip(annotation: Any, value: Any) -> Any:
    codec = resolve_codec(annotation)
    return codec.decode(codec.encode(codec.validate(value)))


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (str, "hello"),
        (int, 42),
        (float, 1.5),
        (Decimal, Decimal("12.345")),
        (bool, True),
        (bytes, b"\x00\x01"),
        (date, date(2026, 1, 2)),
        (timedelta, timedelta(hours=1, microseconds=5)),
        (uuid.UUID, uuid.UUID("12345678-1234-5678-1234-567812345678")),
        (Color, Color.BLUE),
        (Level, Level.HIGH),
        (Literal["a", "b"], "b"),
        (list[int], [1, 2, 3]),
        (tuple[str, ...], ("a", "b")),
        (set[str], {"a", "b"}),
        (frozenset[int], frozenset({1, 2})),
        (set[bytes], {b"x"}),
        (dict[str, int], {"a": 1}),
        (dict[str, list[Color]], {"a": [Color.RED]}),
        (int | None, None),
        (int | None, 5),
        (Any, {"nested": [1, "x", None]}),
    ],
)
def test_values_round_trip(annotation: Any, value: Any) -> None:
    assert round_trip(annotation, value) == value


def test_datetimes_are_sortable_utc_text() -> None:
    codec = resolve_codec(datetime)
    oslo = timezone(timedelta(hours=2))
    stored = codec.encode(codec.validate(datetime(2026, 5, 1, 12, 0, tzinfo=oslo)))
    assert stored == "2026-05-01T10:00:00.000000+00:00"
    earlier = codec.encode(datetime(2026, 5, 1, 9, 59, 59, 999999, tzinfo=UTC))
    assert earlier < stored, "fixed-width UTC text sorts like time"
    assert codec.decode(stored) == datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="naive datetimes"):
        codec.validate(datetime(2026, 1, 1))


def test_datetimes_with_a_z_suffix() -> None:
    codec = codecs.DateTimeCodec("iso_z")
    oslo = timezone(timedelta(hours=2))
    stored = codec.encode(codec.validate(datetime(2026, 10, 13, 10, 48, 47, 966937, tzinfo=oslo)))
    assert stored == "2026-10-13T08:48:47.966937Z"
    assert codec.encode(datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)) == "2026-10-13T08:48:47.000000Z"
    assert codec.decode(stored) == datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)
    assert codec.decode(stored).tzinfo is UTC
    assert (codec.dynamo_type, codec.key_type()) == ("S", "S")
    with pytest.raises(ValueError, match="naive datetimes"):
        codec.validate(datetime(2026, 1, 1))


@pytest.mark.parametrize("format", ["iso", "iso_z", "epoch", "epoch_ms"])
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-10-13T08:48:47.966937Z", datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)),
        ("2026-10-13T08:48:47.966937+00:00", datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)),
        ("2026-10-13T08:48:47.000000Z", datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)),
        ("2026-10-13T08:48:47Z", datetime(2026, 10, 13, 8, 48, 47, tzinfo=UTC)),
        ("2026-10-13T08:48:47.966Z", datetime(2026, 10, 13, 8, 48, 47, 966000, tzinfo=UTC)),
        ("2026-10-13t08:48:47.966937z", datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)),
        ("2026-10-13T10:48:47.966937+02:00", datetime(2026, 10, 13, 8, 48, 47, 966937, tzinfo=UTC)),
    ],
)
def test_iso_text_is_read_in_either_style(format: codecs.DateTimeFormat, stored: str, expected: datetime) -> None:
    decoded = codecs.DateTimeCodec(format).decode(stored)
    assert decoded == expected
    assert decoded.utcoffset() is not None


def test_epoch_datetimes() -> None:
    epoch = codecs.DateTimeCodec("epoch")
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert epoch.encode(moment) == 1767225600
    assert epoch.decode(1767225600) == moment
    assert epoch.dynamo_type == "N"
    milliseconds = codecs.DateTimeCodec("epoch_ms")
    assert milliseconds.encode(moment) == 1767225600000
    assert milliseconds.decode(1767225600000) == moment
    with pytest.raises(ValueError, match="format must be one of 'iso', 'iso_z', 'epoch', 'epoch_ms', got 'weekly'"):
        codecs.DateTimeCodec("weekly")  # type: ignore[arg-type]


def test_datetime_format_of_resolved_codecs() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert resolve_codec(datetime, datetime_format="iso_z").encode(moment) == "2026-01-02T03:04:05.000000Z"
    nested = resolve_codec(list[datetime] | None, datetime_format="iso_z")
    assert nested.encode([moment]) == ["2026-01-02T03:04:05.000000Z"]
    assert resolve_codec(dict[str, datetime], datetime_format="epoch").encode({"at": moment}) == {"at": 1767323045}
    explicit = Annotated[datetime, codecs.UseCodec(codecs.DateTimeCodec("iso"))]
    assert resolve_codec(explicit, datetime_format="iso_z").encode(moment) == "2026-01-02T03:04:05.000000+00:00"


@pytest.mark.parametrize(
    ("annotation", "value", "message"),
    [
        (str, 1, "expected str"),
        (int, True, "expected int"),
        (int, "1", "expected int"),
        (float, "x", "expected float"),
        (float, float("nan"), "cannot store nan"),
        (Decimal, Decimal("NaN"), "cannot store"),
        (bool, 1, "expected bool"),
        (bytes, "x", "expected bytes"),
        (date, datetime(2026, 1, 1, tzinfo=UTC), "expected date"),
        (uuid.UUID, "not-a-uuid", "expected UUID"),
        (Color, "green", "one of 'red', 'blue'"),
        (Literal[1, "a"], True, "expected one of"),
        (list[int], [1, "2"], r"\[1\]: expected int"),
        (dict[str, int], {"a": "b"}, r"\.a: expected int"),
        (dict[str, int], {1: 1}, "map keys must be str"),
        (set[str], ["a"], "expected set"),
    ],
)
def test_invalid_values_are_rejected(annotation: Any, value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_codec(annotation).validate(value)


def test_numbers_are_normalized() -> None:
    assert resolve_codec(int).validate(Decimal(5)) == 5
    assert resolve_codec(Decimal).validate(1.1) == Decimal("1.1")
    assert resolve_codec(Decimal).validate("2.50") == Decimal("2.50")
    assert resolve_codec(float).decode(Decimal("0.5")) == 0.5


def test_empty_sets_are_not_stored() -> None:
    codec = resolve_codec(set[str])
    assert codec.encode(set()) is None
    assert codec.decode(None) == set()
    assert codec.dynamo_type == "SS"
    assert resolve_codec(set[int]).dynamo_type == "NS"
    assert resolve_codec(set[bytes]).dynamo_type == "BS"


def test_key_types() -> None:
    assert resolve_codec(str).key_type() == "S"
    assert resolve_codec(int).key_type() == "N"
    assert resolve_codec(bytes).key_type() == "B"
    assert resolve_codec(datetime).key_type() == "S"
    assert resolve_codec(Color).key_type() == "S"
    assert resolve_codec(Level).key_type() == "N"
    assert resolve_codec(int | None).key_type() == "N"
    assert resolve_codec(bool).key_type() is None
    assert resolve_codec(list[int]).key_type() is None


def test_json_codec_stores_one_string() -> None:
    codec = codecs.JsonCodec()
    stored = codec.encode({"b": [1, 2.5], "a": None})
    assert stored == '{"a":null,"b":[1,2.5]}'
    assert codec.decode(stored) == {"a": None, "b": [1, 2.5]}
    with pytest.raises(ValueError, match="not JSON-serializable"):
        codec.validate({"x": object()})


def test_annotated_use_codec_wins() -> None:
    codec = codecs.JsonCodec()
    assert resolve_codec(Annotated[dict[str, Any], codecs.UseCodec(codec)]) is codec
    assert isinstance(resolve_codec(Annotated[int, "ignored metadata"]), codecs.IntCodec)


def test_unsupported_types_suggest_a_codec() -> None:
    with pytest.raises(TypeError, match="pass codec="):
        resolve_codec(complex)
    with pytest.raises(TypeError, match="element type of sets"):
        resolve_codec(set)
    with pytest.raises(TypeError, match="sets can hold"):
        resolve_codec(set[float | None])
    with pytest.raises(TypeError, match="map keys must be str"):
        resolve_codec(dict[int, int])
    with pytest.raises(TypeError, match="variable-length tuples"):
        resolve_codec(tuple[int, str])
    assert isinstance(resolve_codec(int | str), codecs.AnyCodec)
    with pytest.raises(TypeError, match="union"):
        resolve_codec(int | list[int])


def test_descriptions_name_types() -> None:
    assert resolve_codec(dict[str, list[int]]).describe() == "dict[str, list[int]]"
    assert resolve_codec(set[str] | None).describe() == "set[str] | None"
    assert resolve_codec(Color).describe() == "Color"


@given(st.decimals(allow_nan=False, allow_infinity=False, places=6, min_value=-(10**20), max_value=10**20))
def test_decimals_round_trip_exactly(value: Decimal) -> None:
    assert round_trip(Decimal, value) == value


@pytest.mark.parametrize("format", ["iso", "iso_z"])
@given(st.datetimes(timezones=st.just(UTC), min_value=datetime(1971, 1, 1), max_value=datetime(2999, 1, 1)))
def test_datetime_text_order_matches_time_order(format: codecs.DateTimeFormat, moment: datetime) -> None:
    codec = codecs.DateTimeCodec(format)
    later = moment + timedelta(microseconds=1)
    assert codec.encode(moment) < codec.encode(later)
    assert len(codec.encode(moment)) == len(codec.encode(later)), "fixed width"
    assert codec.decode(codec.encode(moment)) == moment
