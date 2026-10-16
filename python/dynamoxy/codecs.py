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

A model chooses the format of all its ``datetime`` fields with the ``datetime_format`` class
keyword (see :class:`DateTimeCodec`). Use ``codec=`` on a field (or
``Annotated[X, dynamoxy.UseCodec(...)]``) for anything else, for example :class:`JsonCodec` to
store a structure as one JSON string. A codec is any object with the :class:`Codec` interface.
"""

from __future__ import annotations

import enum
import json
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
    dict or set).

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
        """Converts a stored value back."""
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


class StrCodec(Codec[str]):
    """``str`` as ``S``."""

    dynamo_type = "S"

    @override
    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise _type_error("str", value)
        return value

    @override
    def decode(self, value: Any) -> str:
        return str(value)

    @override
    def describe(self) -> str:
        return "str"


class IntCodec(Codec[int]):
    """``int`` as ``N``."""

    dynamo_type = "N"

    @override
    def validate(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, Decimal) and value == value.to_integral_value():
                return int(value)
            raise _type_error("int", value)
        return value

    @override
    def decode(self, value: Any) -> int:
        return int(value)

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
        return float(value)

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
        return value if isinstance(value, Decimal) else Decimal(value)

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
        return bool(value)

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
        return bytes(value)

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
        if self.format in ("iso", "iso_z") or isinstance(value, str):
            text = str(value)
            # RFC 3339 allows a lower-case "z"; fromisoformat only accepts "Z".
            parsed = datetime.fromisoformat(text[:-1] + "Z" if text.endswith("z") else text)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        scale = 1_000 if self.format == "epoch_ms" else 1_000_000
        return _EPOCH + timedelta(microseconds=round(Decimal(value) * scale))

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
        return date.fromisoformat(str(value))

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
        return timedelta(seconds=float(value))

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
        return uuid.UUID(str(value))

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
        return value.value

    @override
    def decode(self, value: Any) -> E:
        if self._dynamo_type == "N":
            value = int(value)
        return self.enum_type(value)

    @override
    def describe(self) -> str:
        return self.enum_type.__name__

    @override
    def key_type(self) -> str | None:
        return self._dynamo_type


class LiteralCodec(Codec[Any]):
    """One of a fixed set of values (``Literal["a", "b"]``)."""

    def __init__(self, values: Sequence[Any]) -> None:
        self.values = tuple(values)
        if all(isinstance(value, str) for value in self.values):
            self._dynamo_type: str | None = "S"
        elif all(isinstance(value, int) and not isinstance(value, bool) for value in self.values):
            self._dynamo_type = "N"
        else:
            self._dynamo_type = None

    @property
    def dynamo_type(self) -> str | None:  # type: ignore[override]
        """``S`` or ``N`` depending on the values."""
        return self._dynamo_type

    @override
    def validate(self, value: Any) -> Any:
        # `True == 1`: a bool only matches a bool literal, an int only a non-bool literal.
        same_kind = (isinstance(value, bool) == isinstance(allowed, bool) for allowed in self.values)
        if not any(value == allowed and kind for allowed, kind in zip(self.values, same_kind, strict=True)):
            raise ValueError(f"expected one of {', '.join(repr(v) for v in self.values)} (got {type(value).__name__})")
        return value

    @override
    def decode(self, value: Any) -> Any:
        return int(value) if self._dynamo_type == "N" else value

    @override
    def describe(self) -> str:
        return f"Literal[{', '.join(repr(value) for value in self.values)}]"

    @override
    def key_type(self) -> str | None:
        return self._dynamo_type


class AnyCodec(Codec[Any]):
    """Values stored as given (they must be DynamoDB-compatible); ``allowed`` restricts types."""

    def __init__(self, allowed: tuple[type, ...] = ()) -> None:
        self.allowed = allowed

    @override
    def validate(self, value: Any) -> Any:
        if self.allowed and not isinstance(value, self.allowed):
            raise _type_error(" | ".join(kind.__name__ for kind in self.allowed), value)
        return value

    @override
    def element(self) -> Codec[Any] | None:
        return self

    @override
    def member(self, key: str) -> Codec[Any] | None:
        return self

    @override
    def describe(self) -> str:
        return " | ".join(kind.__name__ for kind in self.allowed) if self.allowed else "Any"


#: Codec classes whose ``encode`` and ``decode`` return values unchanged (exact types: subclasses
#: may override them), so collections of their values are copied without calling them.
_IDENTITY = frozenset({Codec, AnyCodec})

#: Codec classes whose ``encode`` returns values unchanged (exact types).
_ENCODED_UNCHANGED = _IDENTITY | {StrCodec, IntCodec, FloatCodec, DecimalCodec, BoolCodec, BytesCodec, LiteralCodec}

#: The builtin conversion that the ``decode`` of these codec classes is (exact types).
_CONVERSIONS: dict[type, Callable[[Any], Any]] = {StrCodec: str, IntCodec: int, FloatCodec: float, BoolCodec: bool}


def _decoder(codec: Codec[Any]) -> Callable[[Any], Any]:
    """``codec.decode``, or the builtin conversion it is (a cheaper call)."""
    return _CONVERSIONS.get(type(codec)) or codec.decode


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
        if type(inner) in _ENCODED_UNCHANGED:
            return list(value)
        return [inner.encode(element) for element in value]

    @override
    def decode(self, value: Any) -> Any:
        inner = self.inner
        if type(inner) in _IDENTITY:
            return tuple(value) if self.as_tuple else list(value)
        items = [inner.decode(element) for element in value]
        return tuple(items) if self.as_tuple else items

    @override
    def element(self) -> Codec[Any] | None:
        return self.inner

    @override
    def describe(self) -> str:
        return f"{'tuple' if self.as_tuple else 'list'}[{self.inner.describe()}]"


class SetCodec[T](Codec[set[T]]):
    """``set[X]`` as ``SS``, ``NS`` or ``BS``.

    An empty set is not stored (DynamoDB forbids it) and reads back as an empty set.
    """

    def __init__(self, inner: Codec[T], *, frozen: bool = False) -> None:
        if isinstance(inner, OptionalCodec) or inner.key_type() not in ("S", "N", "B"):
            raise TypeError(f"sets can hold str, numbers or bytes, not {inner.describe()}; use a list instead")
        self.inner = inner
        self.frozen = frozen

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
        if type(inner) in _ENCODED_UNCHANGED:
            return set(value) or None
        return {inner.encode(element) for element in value} or None

    @override
    def decode(self, value: Any) -> Any:
        items = {self.inner.decode(element) for element in (value or ())}
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
        if type(inner) in _ENCODED_UNCHANGED:
            return dict(value.items())
        return {key: inner.encode(element) for key, element in value.items()}

    @override
    def decode(self, value: Any) -> dict[str, T]:
        inner = self.inner
        if type(inner) in _IDENTITY:
            return dict(value.items())
        return {key: inner.decode(element) for key, element in value.items()}

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

    def _schema(self) -> Any:
        from dynamoxy.schema import schema_of

        return schema_of(self.model)

    @override
    def validate(self, value: Any) -> Any:
        schema = self._schema()
        if isinstance(value, self.model):
            return value
        if isinstance(value, Mapping):
            return schema.adapter.create(dict(value))
        raise _type_error(self.model.__name__, value)

    @override
    def encode(self, value: Any) -> dict[str, Any]:
        return self._schema().encode(value, include_type=False)

    @override
    def decode(self, value: Any) -> Any:
        return self._schema().decode(value)

    @override
    def member(self, key: str) -> Codec[Any] | None:
        field = self._schema().fields.get(key)
        return field.codec if field is not None else None

    @override
    def describe(self) -> str:
        return self.model.__name__


class UseCodec:
    """``Annotated`` marker choosing the codec of an attribute: ``Annotated[Money, UseCodec(MoneyCodec())]``."""

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
                return marker.codec
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
        return OptionalCodec(inner) if optional else inner
    if origin is Literal:
        return LiteralCodec(typing.get_args(annotation))
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
