"""Codecs: how Python values are stored in DynamoDB and read back.

dynamoxy picks a codec from each attribute's type annotation:

==================================  ==========================================================
Annotation                          Stored as
==================================  ==========================================================
``str``                             ``S``
``int`` / ``float`` / ``Decimal``   ``N`` (exact decimals; ``float`` NaN/infinity rejected)
``bool``                            ``BOOL``
``bytes``                           ``B``
``datetime``                        ``S``: ISO 8601 in UTC with microseconds, fixed width, so
                                    text order is time order (naive datetimes are rejected);
                                    ``+00:00`` by default, ``Z`` with ``"iso_z"``;
                                    ``"epoch"`` / ``"epoch_ms"`` store ``N``
``date``                            ``S`` (``YYYY-MM-DD``)
``timedelta``                       ``N`` (seconds)
``UUID``                            ``S``
``Enum`` / ``Literal[...]``         the member value (``S`` or ``N``)
``list[X]`` / ``tuple[X, ...]``     ``L``
``set[X]`` / ``frozenset[X]``       ``SS`` / ``NS`` / ``BS`` (empty sets are left out)
``dict[str, X]``                    ``M``
nested ``Document`` / pydantic      ``M``
``X | None``                        ``X``, or nothing when ``None``
``Any``                             as given
==================================  ==========================================================

Reading is exact: ``decode`` raises ``ValueError`` (an ``ItemDecodeError`` for the item) for a stored
value of another type, or one the type cannot hold exactly, such as ``2.5`` for an ``int``,
instead of converting it (see ADR 0015).

A model chooses the format of all its ``datetime`` fields with the ``datetime_format`` class
keyword (see :class:`DateTimeCodec`). Use ``codec=`` on a field (or
``Annotated[X, dynamoxy.UseCodec(...)]``) for anything else, for example :class:`JsonCodec` to
store a structure as one JSON string. A codec is any object with the :class:`Codec` interface.
It stores the values of ``X``: a field of ``X | None`` (or ``Literal[..., None]``) stays optional
and stores nothing for ``None``, whatever its codec.
"""

from __future__ import annotations

import enum
import json
import operator
import types
import typing
import uuid
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal, override

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "AnyCodec",
    "BoolCodec",
    "BytesCodec",
    "Codec",
    "DateCodec",
    "DateTimeCodec",
    "DateTimeFormat",
    "DecimalCodec",
    "DictCodec",
    "DocumentCodec",
    "EnumCodec",
    "FloatCodec",
    "IntCodec",
    "JsonCodec",
    "ListCodec",
    "LiteralCodec",
    "OptionalCodec",
    "RootModelCodec",
    "SetCodec",
    "StrCodec",
    "TimedeltaCodec",
    "UUIDCodec",
    "UseCodec",
    "resolve_codec",
]


class Codec[T]:
    """Converts between a Python value and its stored form.

    Subclass it (or duck-type it) for custom types. ``validate`` checks (and may normalize) values
    assigned to attributes; ``encode``/``decode`` convert to and from the stored representation,
    which must be a DynamoDB-compatible Python value (str, int, Decimal, bytes, bool, None, list,
    dict or set). ``encode`` returns ``None`` for a value that is stored as no attribute; codecs
    of sets (``dynamo_type`` ``"SS"``, ``"NS"`` or ``"BS"``) do so for an empty set, and decode
    ``None`` (a missing attribute) as one.

    Attributes:
        dynamo_type: The DynamoDB type written (``"S"``, ``"N"``, ``"B"`` for key attributes;
            ``"BOOL"``, ``"L"``, ``"M"``, ``"SS"``, ... or ``None`` when it varies).
    """

    dynamo_type: ClassVar[str | None] = None

    def validate(self, value: Any) -> T:
        """Checks a Python value; returns it (possibly normalized).

        Raises:
            ValueError: The value is not acceptable (the message says what is allowed).
        """
        return value

    def encode(self, value: T) -> Any:
        """Converts a valid Python value to its stored form."""
        return value

    def decode(self, value: Any) -> T:
        """Converts a stored value back.

        Raises:
            ValueError: The stored value is of another type, or the type cannot hold it exactly.
        """
        return value

    def element(self) -> Codec[Any] | None:
        """The codec of collection elements (lists and sets), if any."""
        return None

    def member(self, key: str) -> Codec[Any] | None:
        """The codec of a map member, if known."""
        return None

    def describe(self) -> str:
        """A short description for error messages (``"str"``, ``"list[int]"``)."""
        return type(self).__name__.removesuffix("Codec").lower() or "value"

    def key_type(self) -> str | None:
        """The key attribute type (``"S"``, ``"N"`` or ``"B"``), or ``None`` when unusable as a key."""
        return self.dynamo_type if self.dynamo_type in ("S", "N", "B") else None


def _type_error(expected: str, value: Any) -> ValueError:
    # Messages name the type, never the value: values may be personal data, and messages end up in logs.
    return ValueError(f"expected {expected}, got {type(value).__name__}")


# Decoding is exact: a stored value of another type, or one the declared type cannot hold (a
# number with a fraction in an ``int`` field), raises instead of being converted, which would
# change it silently on the next write (or, for keys, read another item's key). Integral numbers
# arrive as ``int`` and other numbers as ``Decimal``; items built in Python (``from_item()``)
# may also hold ``float``s and other ``int`` and ``str`` subclasses.


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _decode_str(value: Any) -> str:
    if type(value) is str:
        return value
    if isinstance(value, str):
        return str.__str__(value)  # the plain text of a str subclass (a StrEnum member)
    raise _type_error("text", value)


def _decode_int(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return operator.index(value)  # a plain int from an int subclass (an IntEnum member)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if _is_number(value):
        raise ValueError(f"expected a whole number, got a {type(value).__name__} that is not one")
    raise _type_error("a number", value)


def _decode_float(value: Any) -> float:
    if type(value) is float:
        return value
    if _is_number(value):
        return float(value)
    raise _type_error("a number", value)


def _decode_bool(value: Any) -> bool:
    if value is True or value is False:
        return value
    raise _type_error("a boolean", value)


def _plain(value: Any) -> Any:
    """``value`` as the exact ``str`` or ``int`` it is (a member of a ``(str, Enum)``, ...)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.__class__ is not str:
        return str.__str__(value)
    if isinstance(value, int) and value.__class__ is not int:
        return int.__int__(value)
    return value


class StrCodec(Codec[str]):
    """``str`` as ``S``."""

    dynamo_type = "S"

    @override
    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise _type_error("str", value)
        # A subclass (a member of a `(str, Enum)`, ...) as the text it holds, which is what
        # DynamoDB stores: its str() ("Kind.BOOK") would end up in keys, templates and guards.
        return value if value.__class__ is str else str.__str__(value)

    @override
    def decode(self, value: Any) -> str:
        return _decode_str(value)

    @override
    def describe(self) -> str:
        return "str"


class IntCodec(Codec[int]):
    """``int`` as ``N``."""

    dynamo_type = "N"

    @override
    def validate(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            # `is_finite()` first: infinity is integral, and comparing a signaling NaN raises.
            if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
                return int(value)
            raise _type_error("int", value)
        # A subclass (a member of an `(int, Enum)`, ...) as its number, as for `str`.
        return value if value.__class__ is int else int.__int__(value)

    @override
    def decode(self, value: Any) -> int:
        return _decode_int(value)

    @override
    def describe(self) -> str:
        return "int"


class FloatCodec(Codec[float]):
    """``float`` as ``N`` (NaN and infinity cannot be stored)."""

    dynamo_type = "N"

    @override
    def validate(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise _type_error("float", value)
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            raise ValueError(f"DynamoDB cannot store {result}")
        return result

    @override
    def decode(self, value: Any) -> float:
        return _decode_float(value)

    @override
    def describe(self) -> str:
        return "float"


class DecimalCodec(Codec[Decimal]):
    """``Decimal`` as ``N`` (exact; up to 38 significant digits)."""

    dynamo_type = "N"

    @override
    def validate(self, value: Any) -> Decimal:
        if isinstance(value, bool):
            raise _type_error("Decimal", value)
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, float):
            result = Decimal(repr(value))
        elif isinstance(value, str):
            try:
                result = Decimal(value)
            except ArithmeticError:
                raise _type_error("Decimal", value) from None
        else:
            raise _type_error("Decimal", value)
        if not result.is_finite():
            raise ValueError(f"DynamoDB cannot store {result}")
        return result

    @override
    def decode(self, value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if _is_number(value):
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        raise _type_error("a number", value)

    @override
    def describe(self) -> str:
        return "Decimal"


class BoolCodec(Codec[bool]):
    """``bool`` as ``BOOL``."""

    dynamo_type = "BOOL"

    @override
    def validate(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise _type_error("bool", value)
        return value

    @override
    def decode(self, value: Any) -> bool:
        return _decode_bool(value)

    @override
    def describe(self) -> str:
        return "bool"


class BytesCodec(Codec[bytes]):
    """``bytes`` as ``B``."""

    dynamo_type = "B"

    @override
    def validate(self, value: Any) -> bytes:
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        if not isinstance(value, bytes):
            raise _type_error("bytes", value)
        return value

    @override
    def decode(self, value: Any) -> bytes:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        raise _type_error("bytes", value)

    @override
    def describe(self) -> str:
        return "bytes"


#: How :class:`DateTimeCodec` stores datetimes: ISO 8601 text (``"iso"``, ``"iso_z"``) or
#: epoch numbers (``"epoch"``, ``"epoch_ms"``).
type DateTimeFormat = Literal["iso", "iso_z", "epoch", "epoch_ms"]

_DATETIME_FORMATS: tuple[DateTimeFormat, ...] = typing.get_args(DateTimeFormat.__value__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SECOND = timedelta(seconds=1)
_MILLISECOND = timedelta(milliseconds=1)


def _check_datetime_format(format: Any, what: str) -> DateTimeFormat:
    """``format`` if it is a :data:`DateTimeFormat`.

    Raises:
        ValueError: It is not (the message lists the formats).
    """
    if format not in _DATETIME_FORMATS:
        allowed = ", ".join(repr(name) for name in _DATETIME_FORMATS)
        raise ValueError(f"{what} must be one of {allowed}, got {format!r}")
    return format


class DateTimeCodec(Codec[datetime]):
    """``datetime`` as ISO 8601 text in UTC (default), or as epoch seconds / milliseconds.

    The ISO forms always have microseconds and the UTC designator, so their text order is time
    order: they work in sort keys and ``between`` queries. ``"iso"`` writes
    ``2026-10-13T08:48:47.966937+00:00`` and ``"iso_z"`` the RFC 3339 form with a ``Z`` suffix,
    ``2026-10-13T08:48:47.966937Z``. Naive datetimes are rejected (use ``datetime.now(UTC)``).
    Epoch seconds is the format DynamoDB's Time to Live needs. Epoch values are exact: the
    whole seconds or milliseconds since 1970 before the datetime (rounded down).

    Text is read in either ISO style whatever the format: ``Z`` or an offset, with or without
    fractional seconds, so items written by other services or before a format change decode.

    Args:
        format: ``"iso"``, ``"iso_z"``, ``"epoch"`` (seconds, ``N``) or ``"epoch_ms"``
            (milliseconds, ``N``).
    """

    def __init__(self, format: DateTimeFormat = "iso") -> None:
        self.format: DateTimeFormat = _check_datetime_format(format, "DateTimeCodec format")

    @property
    def dynamo_type(self) -> str:  # type: ignore[override]
        """``S`` for ISO text, ``N`` for epoch formats."""
        return "N" if self.format in ("epoch", "epoch_ms") else "S"

    @override
    def validate(self, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise _type_error("datetime", value)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetimes are ambiguous; use timezone-aware values such as datetime.now(UTC)")
        return value

    @override
    def encode(self, value: datetime) -> Any:
        utc = value.astimezone(UTC)
        if self.format == "iso":
            return utc.isoformat(timespec="microseconds")
        if self.format == "iso_z":
            return utc.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"
        # Exact integer arithmetic (a float timestamp is a millisecond early for some values),
        # rounding down, also before 1970.
        return (utc - _EPOCH) // (_SECOND if self.format == "epoch" else _MILLISECOND)

    @override
    def decode(self, value: Any) -> datetime:
        if isinstance(value, str):
            text = value if type(value) is str else str.__str__(value)
            # RFC 3339 allows a lower-case "z"; fromisoformat only accepts "Z".
            parsed = datetime.fromisoformat(text[:-1] + "Z" if text.endswith("z") else text)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        if self.format in ("iso", "iso_z") or not _is_number(value):
            raise _type_error("ISO 8601 text" if self.format in ("iso", "iso_z") else "a number or text", value)
        # Python keeps microseconds: finer fractions that other writers store are rounded.
        scale = 1_000 if self.format == "epoch_ms" else 1_000_000
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return _EPOCH + timedelta(microseconds=round(number * scale))

    @override
    def describe(self) -> str:
        return "datetime"

    @override
    def key_type(self) -> str | None:
        return self.dynamo_type


class DateCodec(Codec[date]):
    """``date`` as ``YYYY-MM-DD`` (``S``)."""

    dynamo_type = "S"

    @override
    def validate(self, value: Any) -> date:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise _type_error("date", value)
        return value

    @override
    def encode(self, value: date) -> str:
        return value.isoformat()

    @override
    def decode(self, value: Any) -> date:
        if not isinstance(value, str):
            raise _type_error("ISO 8601 text", value)
        return date.fromisoformat(str.__str__(value))

    @override
    def describe(self) -> str:
        return "date"


class TimedeltaCodec(Codec[timedelta]):
    """``timedelta`` as seconds (``N``)."""

    dynamo_type = "N"

    @override
    def validate(self, value: Any) -> timedelta:
        if not isinstance(value, timedelta):
            raise _type_error("timedelta", value)
        return value

    @override
    def encode(self, value: timedelta) -> Decimal:
        return Decimal(value.days * 86_400 + value.seconds) + Decimal(value.microseconds) / 1_000_000

    @override
    def decode(self, value: Any) -> timedelta:
        if not _is_number(value):
            raise _type_error("a number", value)
        # Exact (a float loses microseconds of long durations); finer fractions are rounded.
        seconds = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return timedelta(microseconds=round(seconds * 1_000_000))

    @override
    def describe(self) -> str:
        return "timedelta"


class UUIDCodec(Codec[uuid.UUID]):
    """``UUID`` as its canonical text (``S``)."""

    dynamo_type = "S"

    @override
    def validate(self, value: Any) -> uuid.UUID:
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                raise _type_error("UUID", value) from None
        if not isinstance(value, uuid.UUID):
            raise _type_error("UUID", value)
        return value

    @override
    def encode(self, value: uuid.UUID) -> str:
        return str(value)

    @override
    def decode(self, value: Any) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if not isinstance(value, str):
            raise _type_error("text", value)
        return uuid.UUID(str.__str__(value))

    @override
    def describe(self) -> str:
        return "UUID"


class EnumCodec[E: enum.Enum](Codec[E]):
    """An ``Enum`` member as its value (``S`` for text values, ``N`` for numbers)."""

    def __init__(self, enum_type: type[E]) -> None:
        self.enum_type = enum_type
        values = [member.value for member in enum_type]
        if values and all(isinstance(value, str) for value in values):
            self._dynamo_type: str | None = "S"
        elif values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            self._dynamo_type = "N"
        else:
            self._dynamo_type = None

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """``S`` or ``N`` depending on the values."""
        return self._dynamo_type

    @override
    def validate(self, value: Any) -> E:
        if isinstance(value, self.enum_type):
            return value
        try:
            return self.enum_type(value)
        except ValueError:
            allowed = ", ".join(repr(member.value) for member in self.enum_type)
            raise ValueError(
                f"expected a {self.enum_type.__name__}: one of {allowed} (got {type(value).__name__})"
            ) from None

    @override
    def encode(self, value: E) -> Any:
        if isinstance(value, self.enum_type):
            return value.value
        # The value of a member, as pydantic models with `use_enum_values=True` hold it.
        return self.validate(value).value

    @override
    def decode(self, value: Any) -> E:
        kind = type(value)
        if (kind is str and self._dynamo_type == "S") or (kind is int and self._dynamo_type == "N"):
            return self.enum_type(value)
        if isinstance(value, self.enum_type):
            return value
        if self._dynamo_type == "N":
            value = _decode_int(value)
        elif self._dynamo_type == "S":
            value = _decode_str(value)
        member = self.enum_type(value)
        # `True == 1`: a stored boolean only reads as a member whose value is a boolean.
        if isinstance(value, bool) != isinstance(member.value, bool):
            raise ValueError(f"expected a {self.enum_type.__name__}, got bool")
        return member

    @override
    def describe(self) -> str:
        return self.enum_type.__name__

    @override
    def key_type(self) -> str | None:
        return self._dynamo_type


class LiteralCodec(Codec[Any]):
    """One of a fixed set of values (``Literal["a", "b"]``, ``Literal[Color.RED]``).

    A member of a plain ``Enum`` is stored as its value and reads back as the member, as an
    ``Enum`` field does; other values are stored as the plain ``str``, ``int``, ``bytes`` or
    ``bool`` they are.
    """

    def __init__(self, values: Sequence[Any]) -> None:
        self.values = tuple(values)
        stored: list[Any] = []
        for value in self.values:
            plain = _literal_stored(value)
            if not isinstance(plain, (str, int, bytes)):
                raise TypeError(
                    f"cannot store the literal {value!r}: its value {plain!r} is not a str, int, bytes or bool"
                )
            stored.append(plain)
        self._stored = tuple(stored)
        self._unchanged = not any(_is_plain_member(value) for value in self.values)
        if all(isinstance(value, str) for value in self._stored):
            self._dynamo_type: str | None = "S"
        elif all(isinstance(value, int) and not isinstance(value, bool) for value in self._stored):
            self._dynamo_type = "N"
        else:
            self._dynamo_type = None

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """``S`` or ``N`` depending on the values."""
        return self._dynamo_type

    @override
    def validate(self, value: Any) -> Any:
        # `True == 1`: a bool only matches a bool literal, an int only a non-bool literal. The
        # value is the plain str or int of the literal it matches, also for a subclass on either
        # side (a member of a `(str, Enum)`), as DynamoDB stores it; a member of a plain Enum
        # stays the member.
        for allowed in self.values:
            if value == allowed and isinstance(value, bool) == isinstance(allowed, bool):
                return _literal_python(allowed)
        raise ValueError(f"expected one of {', '.join(repr(v) for v in self.values)} (got {type(value).__name__})")

    @override
    def encode(self, value: Any) -> Any:
        return _literal_stored(value)

    @override
    def decode(self, value: Any) -> Any:
        if self._dynamo_type == "N":
            value = _decode_int(value)
        elif self._dynamo_type == "S":
            value = _decode_str(value)
        elif isinstance(value, (bytearray, memoryview)):
            value = bytes(value)
        for allowed, stored in zip(self.values, self._stored, strict=True):
            if value == stored and isinstance(value, bool) == isinstance(stored, bool):
                return _literal_python(allowed)
        raise ValueError(f"expected one of {', '.join(repr(v) for v in self.values)} (got {value!r})")

    @override
    def describe(self) -> str:
        return f"Literal[{', '.join(repr(value) for value in self.values)}]"

    @override
    def key_type(self) -> str | None:
        return self._dynamo_type


def _is_plain_member(value: Any) -> bool:
    """Whether ``value`` is a member of an ``Enum`` that does not mix in ``str``, ``int`` or ``bytes``."""
    return isinstance(value, enum.Enum) and not isinstance(value, (str, int, bytes))


def _literal_python(value: Any) -> Any:
    """A literal as instances hold it: a plain ``Enum`` member as is, anything else as plain value."""
    return value if _is_plain_member(value) else _plain(value)


def _literal_stored(value: Any) -> Any:
    """A literal as DynamoDB stores it: a plain ``Enum`` member as its value."""
    return _plain(value.value) if _is_plain_member(value) else _plain(value)


class AnyCodec(Codec[Any]):
    """Values stored as given (they must be DynamoDB-compatible); ``allowed`` restricts types."""

    def __init__(self, allowed: tuple[type, ...] = ()) -> None:
        self.allowed = allowed

    @override
    def validate(self, value: Any) -> Any:
        allowed = self.allowed
        # `bool` is an `int`: a boolean only fits a union that names bool.
        if allowed and (not isinstance(value, allowed) or (isinstance(value, bool) and bool not in allowed)):
            raise _type_error(self.describe(), value)
        return _plain(value)

    @override
    def decode(self, value: Any) -> Any:
        allowed = self.allowed
        if not allowed or value is None:
            return value
        if isinstance(value, bool):
            if bool in allowed:
                return value
        elif isinstance(value, str):
            if str in allowed:
                return _decode_str(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            if bytes in allowed:
                return bytes(value)
        elif _is_number(value):
            return self._number(value)
        raise _type_error(self.describe(), value)

    def _number(self, value: float | Decimal) -> Any:
        """A stored number as the first type of the union that holds it exactly (``float`` last)."""
        allowed = self.allowed
        if isinstance(value, int) and int in allowed:
            return _decode_int(value)
        if isinstance(value, Decimal) and Decimal in allowed:
            return value
        if isinstance(value, float) and float in allowed:
            return value
        if int in allowed:
            try:
                return _decode_int(value)
            except ValueError:
                pass
        if Decimal in allowed:
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        if float in allowed:
            return float(value)
        raise ValueError(f"expected {self.describe()}, got a {type(value).__name__} that none of them holds")

    @override
    def element(self) -> Codec[Any] | None:
        return self

    @override
    def member(self, key: str) -> Codec[Any] | None:
        return self

    @override
    def describe(self) -> str:
        return " | ".join(kind.__name__ for kind in self.allowed) if self.allowed else "Any"


#: Codec classes whose ``encode`` returns values unchanged (exact types: subclasses may override
#: it), so collections of their values are copied without calling it.
_ENCODED_UNCHANGED = frozenset({Codec, AnyCodec, StrCodec, IntCodec, FloatCodec, DecimalCodec, BoolCodec, BytesCodec})


def _encodes_unchanged(codec: Codec[Any]) -> bool:
    """Whether ``codec.encode`` returns values unchanged (exact types, as ``_ENCODED_UNCHANGED``).

    A ``LiteralCodec`` does unless it has members of a plain ``Enum``, which it stores as their
    values.
    """
    kind = type(codec)
    if kind is LiteralCodec:
        return typing.cast("LiteralCodec", codec)._unchanged
    return kind in _ENCODED_UNCHANGED


#: The function that the ``decode`` of these codec classes is (exact types: a cheaper call).
_CONVERSIONS: dict[type, Callable[[Any], Any]] = {
    StrCodec: _decode_str,
    IntCodec: _decode_int,
    FloatCodec: _decode_float,
    BoolCodec: _decode_bool,
}


def _decodes_unchanged(codec: Codec[Any]) -> bool:
    """Whether ``codec.decode`` returns stored values as they are.

    Exact types only (subclasses may decode differently): collections of their values are copied
    without calling it.
    """
    if type(codec) is AnyCodec:
        return not codec.allowed
    return type(codec) is Codec


def _decoder(codec: Codec[Any]) -> Callable[[Any], Any]:
    """``codec.decode``, or the function it is (a cheaper call)."""
    return _CONVERSIONS.get(type(codec)) or codec.decode


#: The type of stored values each built-in codec class decodes to themselves (exact types), so
#: decoding skips the call for them.
_UNCHANGED: dict[type, type] = {StrCodec: str, IntCodec: int, BoolCodec: bool, DecimalCodec: Decimal}


def _element_decoding(codec: Codec[Any]) -> tuple[Callable[[Any], Any] | None, type | None]:
    """How a collection decodes its elements: ``(decode, unchanged type)``.

    ``decode`` is ``None`` when elements are kept as they are; elements of exactly the unchanged
    type are kept without calling it.
    """
    if _decodes_unchanged(codec):
        return None, None
    return _decoder(codec), _UNCHANGED.get(type(codec))


class OptionalCodec[T](Codec[T | None]):
    """``X | None``: ``None`` is allowed (and not stored)."""

    def __init__(self, inner: Codec[T]) -> None:
        self.inner = inner

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """The inner codec's type."""
        return self.inner.dynamo_type

    @override
    def validate(self, value: Any) -> T | None:
        return None if value is None else self.inner.validate(value)

    @override
    def encode(self, value: T | None) -> Any:
        return None if value is None else self.inner.encode(value)

    @override
    def decode(self, value: Any) -> T | None:
        return None if value is None else self.inner.decode(value)

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner.element()

    @override
    def member(self, key: str) -> Codec[Any] | None:
        return self.inner.member(key)

    @override
    def describe(self) -> str:
        return f"{self.inner.describe()} | None"

    @override
    def key_type(self) -> str | None:
        return self.inner.key_type()


class ListCodec[T](Codec[list[T]]):
    """``list[X]`` (or ``tuple[X, ...]`` with ``as_tuple``) as ``L``."""

    dynamo_type = "L"

    def __init__(self, inner: Codec[T], *, as_tuple: bool = False) -> None:
        self.inner = inner
        self.as_tuple = as_tuple
        self._elements = _element_decoding(inner)

    @override
    def validate(self, value: Any) -> Any:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise _type_error("list", value)
        items = []
        for index, element in enumerate(value):
            try:
                items.append(self.inner.validate(element))
            except ValueError as error:
                raise ValueError(f"[{index}]: {error}") from None
        return tuple(items) if self.as_tuple else items

    @override
    def encode(self, value: Any) -> list[Any]:
        inner = self.inner
        if _encodes_unchanged(inner):
            return list(value)
        return [inner.encode(element) for element in value]

    @override
    def decode(self, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise _type_error("a list", value)
        decode, unchanged = self._elements
        if decode is None:
            return tuple(value) if self.as_tuple else list(value)
        items = [element if type(element) is unchanged else decode(element) for element in value]
        return tuple(items) if self.as_tuple else items

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner

    @override
    def describe(self) -> str:
        return f"{'tuple' if self.as_tuple else 'list'}[{self.inner.describe()}]"


class SetCodec[T](Codec[set[T]]):
    """``set[X]`` as ``SS``, ``NS`` or ``BS``.

    An empty set is not stored (DynamoDB forbids it), and a missing attribute reads back as an
    empty set (as ``None`` in a ``set[X] | None`` field, which stores ``None`` the same way).
    """

    def __init__(self, inner: Codec[T], *, frozen: bool = False) -> None:
        if isinstance(inner, OptionalCodec) or inner.key_type() not in ("S", "N", "B"):
            raise TypeError(f"sets can hold str, numbers or bytes, not {inner.describe()}; use a list instead")
        self.inner = inner
        self.frozen = frozen
        self._elements = _element_decoding(inner)

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """``SS``, ``NS`` or ``BS`` depending on the elements."""
        return {"S": "SS", "N": "NS", "B": "BS"}[self.inner.key_type() or "S"]

    @override
    def validate(self, value: Any) -> Any:
        if not isinstance(value, AbstractSet):
            raise _type_error("set", value)
        items = {self.inner.validate(element) for element in value}
        return frozenset(items) if self.frozen else items

    @override
    def encode(self, value: Any) -> Any:
        inner = self.inner
        if _encodes_unchanged(inner):
            return set(value) or None
        return {inner.encode(element) for element in value} or None

    @override
    def decode(self, value: Any) -> Any:
        if value is None:  # an empty set is not stored
            return frozenset() if self.frozen else set()
        if type(value) is not set and not isinstance(value, AbstractSet):
            raise _type_error("a set", value)
        decode, unchanged = self._elements
        if decode is None:
            items = set(value)
        else:
            items = {element if type(element) is unchanged else decode(element) for element in value}
        return frozenset(items) if self.frozen else items

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner

    @override
    def describe(self) -> str:
        return f"{'frozenset' if self.frozen else 'set'}[{self.inner.describe()}]"

    @override
    def key_type(self) -> str | None:
        return None


class DictCodec[T](Codec[dict[str, T]]):
    """``dict[str, X]`` as ``M``."""

    dynamo_type = "M"

    def __init__(self, inner: Codec[T]) -> None:
        self.inner = inner
        self._elements = _element_decoding(inner)

    @override
    def validate(self, value: Any) -> dict[str, T]:
        if not isinstance(value, Mapping):
            raise _type_error("dict", value)
        result: dict[str, T] = {}
        for key, element in value.items():
            if not isinstance(key, str):
                raise ValueError(f"map keys must be str, got {type(key).__name__}")
            try:
                result[key] = self.inner.validate(element)
            except ValueError as error:
                raise ValueError(f".{key}: {error}") from None
        return result

    @override
    def encode(self, value: dict[str, T]) -> dict[str, Any]:
        inner = self.inner
        if _encodes_unchanged(inner):
            return dict(value.items())
        return {key: inner.encode(element) for key, element in value.items()}

    @override
    def decode(self, value: Any) -> dict[str, T]:
        if type(value) is not dict and not isinstance(value, Mapping):
            raise _type_error("a map", value)
        decode, unchanged = self._elements
        if decode is None:
            return dict(value.items())
        items: dict[str, Any] = {
            key: element if type(element) is unchanged else decode(element) for key, element in value.items()
        }
        return items

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner

    @override
    def member(self, key: str) -> Codec[Any] | None:
        return self.inner

    @override
    def describe(self) -> str:
        return f"dict[str, {self.inner.describe()}]"


class JsonCodec(Codec[Any]):
    """Any JSON-serializable value stored as one JSON string (``S``).

    Useful for deep or schemaless structures (no 32-level nesting limit, floats stay floats).
    """

    dynamo_type = "S"

    def __init__(self, *, dumps: Callable[[Any], str] | None = None, loads: Callable[[str], Any] | None = None) -> None:
        self._dumps = dumps or (lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True))
        self._loads = loads or json.loads

    @override
    def validate(self, value: Any) -> Any:
        try:
            self._dumps(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"not JSON-serializable: {error}") from None
        return value

    @override
    def encode(self, value: Any) -> str:
        return self._dumps(value)

    @override
    def decode(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise _type_error("JSON text", value)
        return self._loads(value)

    @override
    def describe(self) -> str:
        return "JSON"

    @override
    def key_type(self) -> str | None:
        return None


class DocumentCodec(Codec[Any]):
    """A nested model (``dynamoxy.Document``, ``dynamoxy.Model`` or a pydantic model) as ``M``."""

    dynamo_type = "M"

    def __init__(self, model: type) -> None:
        self.model = model
        # Native documents and models refuse instances of subclasses, which they would store
        # without the subclass's fields. Pydantic models store them as the declared class, as
        # pydantic serializes them.
        self._exact = hasattr(model, "__dynamoxy_fields__")

    def _schema(self) -> Any:
        from dynamoxy.schema import schema_of

        return schema_of(self.model)

    @override
    def validate(self, value: Any) -> Any:
        schema = self._schema()
        if type(value) is self.model:
            return value
        if isinstance(value, self.model):
            if self._exact:
                raise self._subclass_error(value)
            return value
        if isinstance(value, Mapping):
            return schema.adapter.create(dict(value))
        raise _type_error(self.model.__name__, value)

    def _subclass_error(self, value: Any) -> ValueError:
        name, other = self.model.__name__, type(value).__name__
        return ValueError(f"expected {name}, got {other}, a subclass whose fields {name} does not store")

    @override
    def encode(self, value: Any) -> dict[str, Any]:
        if self._exact and type(value) is not self.model and isinstance(value, self.model):
            raise self._subclass_error(value)
        return self._schema().encode(value, include_type=False)

    @override
    def decode(self, value: Any) -> Any:
        if type(value) is not dict and not isinstance(value, Mapping):
            raise _type_error("a map", value)
        return self._schema().decode(value)

    @override
    def member(self, key: str) -> Codec[Any] | None:
        field = self._schema().fields.get(key)
        return field.codec if field is not None else None

    @override
    def describe(self) -> str:
        return self.model.__name__


class RootModelCodec(Codec[Any]):
    """A pydantic ``RootModel`` as its root value (``class Tags(RootModel[list[str]])`` as ``L``)."""

    def __init__(self, model: type, inner: Codec[Any]) -> None:
        self.model = model
        self.inner = inner

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """The type of the root value."""
        return self.inner.dynamo_type

    def _model(self, value: Any) -> Any:
        from pydantic import ValidationError as PydanticValidationError

        from dynamoxy._pydantic import _without_inputs

        try:
            return self.model.model_validate(value)  # pyrefly: ignore[missing-attribute]
        except PydanticValidationError as error:
            raise _without_inputs(error) from None

    @override
    def validate(self, value: Any) -> Any:
        return value if isinstance(value, self.model) else self._model(value)

    @override
    def encode(self, value: Any) -> Any:
        root = value.root if isinstance(value, self.model) else self._model(value).root
        return self.inner.encode(self.inner.validate(root))

    @override
    def decode(self, value: Any) -> Any:
        return self._model(self.inner.decode(value))

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner.element()

    @override
    def member(self, key: str) -> Codec[Any] | None:
        return self.inner.member(key)

    @override
    def describe(self) -> str:
        return self.model.__name__

    @override
    def key_type(self) -> str | None:
        return self.inner.key_type()


def _is_root_model(annotation: type) -> bool:
    return bool(getattr(annotation, "__pydantic_root_model__", False)) and _is_model(annotation)


class UseCodec:
    """``Annotated`` marker choosing the codec of an attribute: ``Annotated[Money, UseCodec(MoneyCodec())]``.

    The codec stores the values of the type: ``Annotated[Money | None, UseCodec(MoneyCodec())]``
    is optional, and ``None`` is not stored.
    """

    __slots__ = ("codec",)

    def __init__(self, codec: Codec[Any]) -> None:
        self.codec = codec

    def __repr__(self) -> str:
        return f"UseCodec({self.codec!r})"


_SCALARS: dict[type, Callable[[], Codec[Any]]] = {
    str: StrCodec,
    int: IntCodec,
    float: FloatCodec,
    Decimal: DecimalCodec,
    bool: BoolCodec,
    bytes: BytesCodec,
    date: DateCodec,
    timedelta: TimedeltaCodec,
    uuid.UUID: UUIDCodec,
}


def _is_model(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    if hasattr(annotation, "__dynamoxy_fields__"):
        return True
    return any(base.__module__.startswith("pydantic") and base.__name__ == "BaseModel" for base in annotation.__mro__)


def _allows_none(annotation: Any) -> bool:
    """Whether a type annotation allows ``None``: ``X | None``, ``Optional[X]``, ``Literal[..., None]``.

    Also through ``Annotated`` and type aliases. ``Any`` does not count: its fields are required
    like other fields.
    """
    if annotation is None or annotation is type(None):
        return True
    if isinstance(annotation, typing.TypeAliasType):
        return _allows_none(annotation.__value__)
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _allows_none(typing.get_args(annotation)[0])
    if origin is Literal:
        return any(value is None for value in typing.get_args(annotation))
    if origin is typing.Union or origin is types.UnionType:
        return any(_allows_none(argument) for argument in typing.get_args(annotation))
    return False


def _optional(codec: Codec[Any]) -> Codec[Any]:
    """``codec``, allowing ``None`` too (an :class:`OptionalCodec`, never one inside another)."""
    return codec if isinstance(codec, OptionalCodec) else OptionalCodec(codec)


def _given_codec(codec: Codec[Any], annotation: Any) -> Codec[Any]:
    """The codec of an attribute that chooses ``codec`` (``codec=``, ``UseCodec``) for ``annotation``.

    The codec stores the values of the type. When the annotation allows ``None`` too
    (``X | None``, ``Literal[..., None]``), so does the attribute, which then stores nothing for
    ``None``, as ``X | None`` attributes do.
    """
    return _optional(codec) if _allows_none(annotation) else codec


def resolve_codec(annotation: Any, *, where: str = "attribute", datetime_format: DateTimeFormat = "iso") -> Codec[Any]:
    """The codec for a type annotation.

    Args:
        annotation: The type.
        where: What the type annotates, for error messages (``"User.email"``).
        datetime_format: How ``datetime`` values are stored (a :class:`DateTimeCodec` format),
            also inside lists, sets and maps.

    Raises:
        TypeError: The type cannot be stored; the message suggests ``codec=``.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        base, *metadata = typing.get_args(annotation)
        for marker in metadata:
            if isinstance(marker, UseCodec):
                return _given_codec(marker.codec, base)
        return resolve_codec(base, where=where, datetime_format=datetime_format)
    if annotation is Any or annotation is object:
        return AnyCodec()
    if isinstance(annotation, typing.TypeAliasType):
        return resolve_codec(annotation.__value__, where=where, datetime_format=datetime_format)
    if origin is typing.Union or origin is types.UnionType:
        arguments = typing.get_args(annotation)
        options = [argument for argument in arguments if argument is not type(None)]
        optional = len(options) < len(arguments)
        if len(options) == 1:
            inner = resolve_codec(options[0], where=where, datetime_format=datetime_format)
        elif all(option in (str, int, float, bool, Decimal, bytes) for option in options):
            inner = AnyCodec(tuple(options))
        else:
            raise TypeError(
                f"{where}: cannot store the union {annotation}; use one type, Any, or pass codec=... "
                "(see dynamoxy.codecs)"
            )
        return _optional(inner) if optional else inner
    if origin is Literal:
        values = typing.get_args(annotation)
        literal = LiteralCodec([value for value in values if value is not None])
        # `Literal["a", None]` is `Literal["a"] | None`: None is not stored.
        return _optional(literal) if len(literal.values) < len(values) else literal
    if origin in (list, Sequence, typing.Sequence):
        (element,) = typing.get_args(annotation) or (Any,)
        return ListCodec(resolve_codec(element, where=where, datetime_format=datetime_format))
    if origin is tuple:
        arguments = typing.get_args(annotation)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return ListCodec(resolve_codec(arguments[0], where=where, datetime_format=datetime_format), as_tuple=True)
        raise TypeError(f"{where}: only variable-length tuples (tuple[X, ...]) can be stored, not {annotation}")
    if origin in (set, frozenset, AbstractSet, typing.AbstractSet):
        (element,) = typing.get_args(annotation) or (Any,)
        return SetCodec(
            resolve_codec(element, where=where, datetime_format=datetime_format), frozen=origin is frozenset
        )
    if origin in (dict, Mapping, typing.Mapping):
        arguments = typing.get_args(annotation) or (str, Any)
        if arguments[0] is not str:
            raise TypeError(f"{where}: map keys must be str, not {arguments[0]}")
        return DictCodec(resolve_codec(arguments[1], where=where, datetime_format=datetime_format))
    if annotation in (list, tuple):
        return ListCodec(AnyCodec(), as_tuple=annotation is tuple)
    if annotation is dict:
        return DictCodec(AnyCodec())
    if annotation in (set, frozenset):
        raise TypeError(f"{where}: give the element type of sets (set[str], set[int] or set[bytes])")
    if isinstance(annotation, type):
        if issubclass(annotation, bool):
            return BoolCodec()
        if issubclass(annotation, enum.Enum):
            return EnumCodec(annotation)
        if issubclass(annotation, datetime):
            return DateTimeCodec(datetime_format)
        for kind, factory in _SCALARS.items():
            if annotation is kind:
                return factory()
        if _is_root_model(annotation):
            from dynamoxy._pydantic import _resolve_annotations

            _resolve_annotations(annotation)  # pyrefly: ignore[bad-argument-type]
            root = annotation.model_fields["root"].annotation  # pyrefly: ignore[missing-attribute]
            return RootModelCodec(annotation, resolve_codec(root, where=where, datetime_format=datetime_format))
        if _is_model(annotation):
            return DocumentCodec(annotation)
        if typing.is_typeddict(annotation):
            return DictCodec(AnyCodec())
        for kind, factory in _SCALARS.items():
            if issubclass(annotation, kind):
                return factory()
    raise TypeError(
        f"{where}: dynamoxy cannot store values of type {annotation!r}; "
        "pass codec=... (or Annotated[..., UseCodec(...)]) - see dynamoxy.codecs"
    )
