"""Expressions: conditions, updates and projections, built from attributes instead of strings.

Model attributes are paths (``User.age``, ``User.address["city"]``); :func:`F` makes a path from a
name for dict-level work. Comparing a path builds a :class:`Condition`; combine conditions with
``&``, ``|`` and ``~`` (not ``and``/``or``/``not``)::

    (User.status == "active") & (User.age >= 18) & User.email.exists()
    User.tags.contains("admin") | ~User.banned.exists()

Paths also build update actions::

    User.visits.add(1)
    User.name.set("Ada")
    User.score.set(User.score + 10)
    User.nickname.remove()
    User.history.append(["login"])

Values are encoded with the attribute's codec (a ``datetime`` attribute compares ISO strings),
and every name and value becomes a placeholder, so reserved words and special characters never
need escaping. :class:`ExpressionBuilder` compiles everything for one request.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload, override

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from decimal import Decimal

__all__ = [
    "Condition",
    "ExpressionBuilder",
    "F",
    "Operand",
    "Path",
    "UpdateAction",
    "and_",
    "attr",
    "condition",
    "not_",
    "or_",
]


class ValueEncoder(Protocol):
    """What a path needs from a codec: turn a Python value into a storable value."""

    def encode(self, value: Any) -> Any:
        """Encodes ``value`` for storage."""
        ...

    def element(self) -> ValueEncoder | None:
        """The encoder of collection elements (lists and sets), if known."""
        ...

    def member(self, key: str) -> ValueEncoder | None:
        """The encoder of a map member, if known."""
        ...


class ExpressionBuilder:
    """Allocates placeholders and compiles the expressions of one request.

    Example::

        builder = ExpressionBuilder()
        condition = builder.condition(User.age >= 18)  # "#a0 >= :v0"
        builder.names  # {"#a0": "age"}
        builder.values  # {":v0": 18}
    """

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.values: dict[str, Any] = {}
        self._name_ids: dict[str, str] = {}

    def name(self, attribute: str) -> str:
        """The placeholder for an attribute name (the same name reuses its placeholder)."""
        placeholder = self._name_ids.get(attribute)
        if placeholder is None:
            placeholder = f"#a{len(self._name_ids)}"
            self._name_ids[attribute] = placeholder
            self.names[placeholder] = attribute
        return placeholder

    def path(self, path: Path[Any]) -> str:
        """A document path with placeholders (``#a0.#a1[2]``)."""
        elements = path._elements
        if len(elements) == 1:  # a top-level attribute (always a name)
            return self.name(elements[0])  # type: ignore[arg-type]
        parts: list[str] = []
        for element in elements:
            if isinstance(element, int):
                parts.append(f"[{element}]")
            else:
                parts.append(("." if parts else "") + self.name(element))
        return "".join(parts)

    def value(self, value: Any) -> str:
        """The placeholder for a value."""
        placeholder = f":v{len(self.values)}"
        self.values[placeholder] = value
        return placeholder

    def condition(self, condition: Condition | None) -> str | None:
        """Compiles a condition (``None`` stays ``None``)."""
        return None if condition is None else condition.compile(self)

    def update(self, actions: Sequence[UpdateAction]) -> str | None:
        """Compiles update actions into an update expression (``None`` when there are none).

        Raises:
            ValueError: Two actions write the same path, or one path is inside another.
        """
        if not actions:
            return None
        seen: list[Path[Any]] = []
        for action in actions:
            path = action.path
            for other in seen:
                if path.overlaps(other):
                    raise ValueError(
                        f"two update actions write overlapping paths: {other} and {path}; combine them into one action"
                    )
            seen.append(path)
        sections: dict[str, list[str]] = {"SET": [], "REMOVE": [], "ADD": [], "DELETE": []}
        for action in actions:
            sections[action.kind].append(action.compile(self))
        return " ".join(f"{kind} {', '.join(parts)}" for kind, parts in sections.items() if parts)

    def projection(self, paths: Iterable[Path[Any] | str]) -> str | None:
        """Compiles a projection expression (``None`` when empty)."""
        compiled = [self.path(path if isinstance(path, Path) else F(path)) for path in paths]
        return ", ".join(compiled) if compiled else None

    def raw(self, text: str, names: Mapping[str, str], values: Mapping[str, Any]) -> str:
        """Includes a hand-written expression, renaming its placeholders to avoid clashes."""

        def rename(match: re.Match[str]) -> str:
            token = match.group(0)
            if token.startswith("#"):
                if token not in names:
                    raise ValueError(f"expression {text!r} uses {token} but names= does not define it")
                return self.name(names[token])
            if token not in values:
                raise ValueError(f"expression {text!r} uses {token} but values= does not define it")
            return self.value(values[token])

        return re.sub(r"[#:][A-Za-z0-9_]+", rename, text)

    def arguments(self) -> dict[str, Any]:
        """``expression_attribute_names`` / ``expression_attribute_values`` keyword arguments."""
        arguments: dict[str, Any] = {}
        if self.names:
            arguments["expression_attribute_names"] = dict(self.names)
        if self.values:
            arguments["expression_attribute_values"] = dict(self.values)
        return arguments


class Operand:
    """A value in an expression: a path, a literal, ``size()``, arithmetic or a function."""

    __slots__ = ()

    def compile(self, builder: ExpressionBuilder) -> str:
        """Renders the operand."""
        raise NotImplementedError

    def __add__(self, other: Any) -> Operand:
        return Arithmetic(self, "+", _operand(other, None))

    def __sub__(self, other: Any) -> Operand:
        return Arithmetic(self, "-", _operand(other, None))


class Value(Operand):
    """A literal value (already encoded)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def compile(self, builder: ExpressionBuilder) -> str:
        return builder.value(self.value)

    def __repr__(self) -> str:
        return f"Value({self.value!r})"


class Size(Operand):
    """``size(path)``."""

    __slots__ = ("path",)

    def __init__(self, path: Path[Any]) -> None:
        self.path = path

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"size({builder.path(self.path)})"

    def _compare(self, operator: str, other: Any) -> Condition:
        return Compare(self, operator, _operand(other, None))

    def __eq__(self, other: object) -> Condition:  # type: ignore[override]
        return self._compare("=", other)

    def __ne__(self, other: object) -> Condition:  # type: ignore[override]
        return self._compare("<>", other)

    def __lt__(self, other: int) -> Condition:
        return self._compare("<", other)

    def __le__(self, other: int) -> Condition:
        return self._compare("<=", other)

    def __gt__(self, other: int) -> Condition:
        return self._compare(">", other)

    def __ge__(self, other: int) -> Condition:
        return self._compare(">=", other)

    __hash__ = object.__hash__

    def between(self, low: int, high: int) -> Condition:
        """``size(path) BETWEEN low AND high``."""
        return Between(self, Value(low), Value(high))


class Arithmetic(Operand):
    """``a + b`` or ``a - b`` (``SET`` values only)."""

    __slots__ = ("left", "operator", "right")

    def __init__(self, left: Operand, operator: Literal["+", "-"], right: Operand) -> None:
        self.left = left
        self.operator = operator
        self.right = right

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"{self.left.compile(builder)} {self.operator} {self.right.compile(builder)}"


class IfNotExists(Operand):
    """``if_not_exists(path, default)`` (``SET`` values only)."""

    __slots__ = ("default", "path")

    def __init__(self, path: Path[Any], default: Operand) -> None:
        self.path = path
        self.default = default

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"if_not_exists({builder.path(self.path)}, {self.default.compile(builder)})"


class ListAppend(Operand):
    """``list_append(a, b)`` (``SET`` values only)."""

    __slots__ = ("first", "second")

    def __init__(self, first: Operand, second: Operand) -> None:
        self.first = first
        self.second = second

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"list_append({self.first.compile(builder)}, {self.second.compile(builder)})"


def _operand(value: Any, encoder: ValueEncoder | None) -> Operand:
    if isinstance(value, Operand):
        return value
    if isinstance(value, Condition):
        raise TypeError(f"a condition ({value!r}) cannot be used as a value")
    return Value(encoder.encode(value) if encoder is not None else value)


class Path[T](Operand):
    """A document path: a top-level attribute, a map member or a list element.

    ``T`` is the Python type of the value at the path: model attributes (``User.age`` is a
    ``Path[int]``) type-check what they are compared with and assigned. Values are encoded with
    the attribute's codec. ``F("name")`` builds an untyped ``Path[Any]``.

    Example::

        F("address")["city"] == "Oslo"
        F("tags")[0].exists()
    """

    __slots__ = ("_elements", "_encoder")

    def __init__(self, *elements: str | int, encoder: ValueEncoder | None = None) -> None:
        if not elements or not isinstance(elements[0], str):
            raise ValueError("a path starts with an attribute name")
        for element in elements:
            if isinstance(element, str) and not element:
                raise ValueError("attribute names in paths must not be empty")
            if isinstance(element, int) and (isinstance(element, bool) or element < 0):
                raise ValueError(f"list indexes must be non-negative integers, got {element!r}")
        self._elements: tuple[str | int, ...] = tuple(elements)
        self._encoder = encoder

    @property
    def elements(self) -> tuple[str | int, ...]:
        """The steps of the path."""
        return self._elements

    @property
    def encoder(self) -> ValueEncoder | None:
        """The codec that encodes values compared with or assigned to this path."""
        return self._encoder

    def overlaps(self, other: Path[Any]) -> bool:
        """Whether one path is the other or contains it."""
        mine, theirs = self._elements, other._elements
        if mine[0] != theirs[0]:  # most paths differ in their attribute
            return False
        shorter = min(len(mine), len(theirs))
        return mine[:shorter] == theirs[:shorter]

    def __getitem__(self, key: str | int) -> Path[Any]:
        """A map member (``path["key"]``) or a list element (``path[0]``)."""
        encoder = None
        if self._encoder is not None:
            encoder = self._encoder.element() if isinstance(key, int) else self._encoder.member(key)
        return Path(*self._elements, key, encoder=encoder)

    @override
    def compile(self, builder: ExpressionBuilder) -> str:
        return builder.path(self)

    def __str__(self) -> str:
        text = ""
        for element in self._elements:
            text += f"[{element}]" if isinstance(element, int) else (f".{element}" if text else element)
        return text

    def __repr__(self) -> str:
        return f"Path({str(self)!r})"

    def __hash__(self) -> int:
        return hash(self._elements)

    def _value(self, value: Any) -> Operand:
        return _operand(value, self._encoder)

    # ---- conditions ---------------------------------------------------------------------

    # Decimal attributes also accept ints (``Account.balance >= 100``); the overloads say so.

    @overload
    def __eq__(  # type: ignore[override]
        self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand | None
    ) -> Condition: ...
    @overload
    def __eq__(self, other: T | Operand) -> Condition: ...
    def __eq__(self, other: object) -> Condition:  # type: ignore[override]
        return Compare(self, "=", self._value(other))

    @overload
    def __ne__(  # type: ignore[override]
        self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand | None
    ) -> Condition: ...
    @overload
    def __ne__(self, other: T | Operand) -> Condition: ...
    def __ne__(self, other: object) -> Condition:  # type: ignore[override]
        return Compare(self, "<>", self._value(other))

    @overload
    def __lt__(self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand) -> Condition: ...
    @overload
    def __lt__(self, other: T | Operand) -> Condition: ...
    def __lt__(self, other: Any) -> Condition:
        return Compare(self, "<", self._value(other))

    @overload
    def __le__(self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand) -> Condition: ...
    @overload
    def __le__(self, other: T | Operand) -> Condition: ...
    def __le__(self, other: Any) -> Condition:
        return Compare(self, "<=", self._value(other))

    @overload
    def __gt__(self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand) -> Condition: ...
    @overload
    def __gt__(self, other: T | Operand) -> Condition: ...
    def __gt__(self, other: Any) -> Condition:
        return Compare(self, ">", self._value(other))

    @overload
    def __ge__(self: Path[Decimal] | Path[Decimal | None], other: Decimal | int | Operand) -> Condition: ...
    @overload
    def __ge__(self, other: T | Operand) -> Condition: ...
    def __ge__(self, other: Any) -> Condition:
        return Compare(self, ">=", self._value(other))

    @overload
    def between(self: Path[Decimal] | Path[Decimal | None], low: Decimal | int, high: Decimal | int) -> Condition: ...
    @overload
    def between(self, low: T, high: T) -> Condition: ...
    def between(self, low: Any, high: Any) -> Condition:
        """``path BETWEEN low AND high`` (inclusive)."""
        return Between(self, self._value(low), self._value(high))

    def is_in(self, values: Iterable[T]) -> Condition:
        """``path IN (...)`` (at most 100 values).

        Raises:
            ValueError: ``values`` is empty or has more than 100 values.
        """
        operands = [self._value(value) for value in values]
        if not operands or len(operands) > 100:
            raise ValueError(f"is_in() takes 1 to 100 values, got {len(operands)}")
        return In(self, operands)

    def begins_with(self, prefix: str | bytes) -> Condition:
        """``begins_with(path, prefix)`` (strings and binaries)."""
        return Function("begins_with", self, Value(prefix))

    def contains(self, value: Any) -> Condition:
        """``contains(path, value)``: substring, set member or list element."""
        encoder = self._encoder.element() if self._encoder is not None else None
        operand = value if isinstance(value, Operand) else Value(encoder.encode(value) if encoder else value)
        return Function("contains", self, operand)

    def exists(self) -> Condition:
        """``attribute_exists(path)``."""
        return Function("attribute_exists", self, None)

    def not_exists(self) -> Condition:
        """``attribute_not_exists(path)``."""
        return Function("attribute_not_exists", self, None)

    def is_type(self, type_name: Literal["S", "SS", "N", "NS", "B", "BS", "BOOL", "NULL", "L", "M"]) -> Condition:
        """``attribute_type(path, type)`` with a DynamoDB type descriptor."""
        return Function("attribute_type", self, Value(type_name))

    def size(self) -> Size:
        """``size(path)``, comparable with numbers (``User.tags.size() > 3``)."""
        return Size(self)

    # ---- update actions -------------------------------------------------------------------

    @overload
    def set(self: Path[Decimal] | Path[Decimal | None], value: Decimal | int | Operand) -> UpdateAction: ...
    @overload
    def set(self, value: T | Operand) -> UpdateAction: ...
    def set(self, value: Any) -> UpdateAction:
        """``SET path = value`` (a value, another path or arithmetic such as ``path + 1``)."""
        return UpdateAction("SET", self, self._value(value))

    @overload
    def set_if_not_exists(self: Path[Decimal] | Path[Decimal | None], value: Decimal | int) -> UpdateAction: ...
    @overload
    def set_if_not_exists(self, value: T) -> UpdateAction: ...
    def set_if_not_exists(self, value: Any) -> UpdateAction:
        """``SET path = if_not_exists(path, value)``: set only when missing."""
        return UpdateAction("SET", self, IfNotExists(self, self._value(value)))

    @overload
    def if_not_exists(self: Path[Decimal] | Path[Decimal | None], default: Decimal | int) -> Operand: ...
    @overload
    def if_not_exists(self, default: T) -> Operand: ...
    def if_not_exists(self, default: Any) -> Operand:
        """``if_not_exists(path, default)`` as a value (``User.n.set(User.n.if_not_exists(0) + 1)``)."""
        return IfNotExists(self, self._value(default))

    @overload
    def add(self: Path[Decimal] | Path[Decimal | None], value: Decimal | int) -> UpdateAction: ...
    @overload
    def add(self, value: T) -> UpdateAction: ...
    def add(self, value: Any) -> UpdateAction:
        """``ADD path value``: adds to a number (missing counts as 0) or a set."""
        return UpdateAction("ADD", self, self._value(value))

    def increment(self, amount: float = 1) -> UpdateAction:
        """Adds ``amount`` to a number attribute (missing counts as 0)."""
        return UpdateAction("ADD", self, Value(amount))

    def decrement(self, amount: float = 1) -> UpdateAction:
        """Subtracts ``amount`` from a number attribute (missing counts as 0)."""
        return UpdateAction("ADD", self, Value(-amount))

    def delete(self, values: T) -> UpdateAction:
        """``DELETE path values``: removes elements from a set."""
        return UpdateAction("DELETE", self, self._value(values))

    def discard(self, values: T) -> UpdateAction:
        """Alias of :meth:`delete` (set semantics: missing elements are ignored)."""
        return self.delete(values)

    def remove(self) -> UpdateAction:
        """``REMOVE path``."""
        return UpdateAction("REMOVE", self, None)

    def append(self, values: T) -> UpdateAction:
        """``SET path = list_append(path, values)`` (the list must exist)."""
        return UpdateAction("SET", self, ListAppend(self, self._value(values)))

    def append_or_create(self, values: T) -> UpdateAction:
        """Appends to the list, creating it when missing."""
        return UpdateAction("SET", self, ListAppend(IfNotExists(self, Value([])), self._value(values)))

    def prepend(self, values: T) -> UpdateAction:
        """``SET path = list_append(values, path)`` (the list must exist)."""
        return UpdateAction("SET", self, ListAppend(self._value(values), self))


def F(name: str, *elements: str | int) -> Path[Any]:  # noqa: N802 - mirrors common query-builder APIs
    """An untyped path: ``F("address", "city")`` or ``F("address")["city"]``.

    Values compared with untyped paths are stored as given (they must be DynamoDB-compatible:
    str, int, float, Decimal, bytes, bool, None, list, dict or set).
    """
    return Path(name, *elements)


class Condition:
    """A condition: combine with ``&``, ``|`` and ``~``.

    Raises:
        TypeError: When used as a boolean (``and``/``or``/``if``), which is always a mistake.
    """

    __slots__ = ()

    def compile(self, builder: ExpressionBuilder) -> str:
        """Renders the condition."""
        raise NotImplementedError

    def __and__(self, other: Condition | bool) -> Condition:
        return And((self, _as_condition(other)))

    def __rand__(self, other: Condition | bool) -> Condition:
        return And((_as_condition(other), self))

    def __or__(self, other: Condition | bool) -> Condition:
        return Or((self, _as_condition(other)))

    def __ror__(self, other: Condition | bool) -> Condition:
        return Or((_as_condition(other), self))

    def __invert__(self) -> Condition:
        return Not(self)

    def __bool__(self) -> bool:
        raise TypeError(
            "a dynamoxy condition cannot be used as a boolean; combine conditions with & | ~ "
            "(not `and`, `or`, `not`), and pass them to filter=/condition=/where()"
        )

    def __repr__(self) -> str:
        builder = ExpressionBuilder()
        return f"Condition({self.compile(builder)!r}, names={builder.names!r}, values={builder.values!r})"


def _as_condition(value: object) -> Condition:
    if isinstance(value, Condition):
        return value
    raise TypeError(
        f"expected a dynamoxy condition, got {type(value).__name__} ({value!r}); "
        "did you compare two plain values instead of an attribute?"
    )


def as_condition(value: Condition | bool | None) -> Condition | None:
    """Checks a ``condition=`` / ``filter=`` argument (``bool`` only appears in annotations)."""
    if value is None:
        return None
    return _as_condition(value)


class Compare(Condition):
    """``a <op> b``."""

    __slots__ = ("left", "operator", "right")

    def __init__(self, left: Operand, operator: str, right: Operand) -> None:
        self.left = left
        self.operator = operator
        self.right = right

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"{self.left.compile(builder)} {self.operator} {self.right.compile(builder)}"


class Between(Condition):
    """``a BETWEEN low AND high``."""

    __slots__ = ("high", "low", "operand")

    def __init__(self, operand: Operand, low: Operand, high: Operand) -> None:
        self.operand = operand
        self.low = low
        self.high = high

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"{self.operand.compile(builder)} BETWEEN {self.low.compile(builder)} AND {self.high.compile(builder)}"


class In(Condition):
    """``a IN (b, c, ...)``."""

    __slots__ = ("candidates", "operand")

    def __init__(self, operand: Operand, candidates: Sequence[Operand]) -> None:
        self.operand = operand
        self.candidates = tuple(candidates)

    def compile(self, builder: ExpressionBuilder) -> str:
        values = ", ".join(candidate.compile(builder) for candidate in self.candidates)
        return f"{self.operand.compile(builder)} IN ({values})"


class Function(Condition):
    """A condition function: ``attribute_exists``, ``begins_with``, ``contains``, ..."""

    __slots__ = ("argument", "name", "path")

    def __init__(self, name: str, path: Path[Any], argument: Operand | None) -> None:
        self.name = name
        self.path = path
        self.argument = argument

    def compile(self, builder: ExpressionBuilder) -> str:
        if self.argument is None:
            return f"{self.name}({builder.path(self.path)})"
        return f"{self.name}({builder.path(self.path)}, {self.argument.compile(builder)})"


class And(Condition):
    """Every part holds."""

    __slots__ = ("parts",)

    def __init__(self, parts: Iterable[Condition]) -> None:
        flat: list[Condition] = []
        for part in parts:
            flat.extend(part.parts if isinstance(part, And) else (part,))
        self.parts = tuple(flat)

    def compile(self, builder: ExpressionBuilder) -> str:
        return " AND ".join(f"({part.compile(builder)})" for part in self.parts)


class Or(Condition):
    """Some part holds."""

    __slots__ = ("parts",)

    def __init__(self, parts: Iterable[Condition]) -> None:
        flat: list[Condition] = []
        for part in parts:
            flat.extend(part.parts if isinstance(part, Or) else (part,))
        self.parts = tuple(flat)

    def compile(self, builder: ExpressionBuilder) -> str:
        return " OR ".join(f"({part.compile(builder)})" for part in self.parts)


class Not(Condition):
    """The condition does not hold."""

    __slots__ = ("inner",)

    def __init__(self, inner: Condition) -> None:
        self.inner = inner

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"NOT ({self.inner.compile(builder)})"


class RawCondition(Condition):
    """A hand-written condition with its own placeholders."""

    __slots__ = ("names", "text", "values")

    def __init__(self, text: str, names: Mapping[str, str], values: Mapping[str, Any]) -> None:
        self.text = text
        self.names = dict(names)
        self.values = dict(values)

    def compile(self, builder: ExpressionBuilder) -> str:
        return f"({builder.raw(self.text, self.names, self.values)})"


def condition(
    text: str, *, names: Mapping[str, str] | None = None, values: Mapping[str, Any] | None = None
) -> Condition:
    """A hand-written condition: ``condition("#s = :s", names={"#s": "status"}, values={":s": "x"})``.

    Placeholders are renamed when the condition is combined with others, so they never clash.
    """
    return RawCondition(text, names or {}, values or {})


def and_(*conditions: Condition | bool) -> Condition:
    """All conditions hold.

    Raises:
        ValueError: No condition is given.
    """
    if not conditions:
        raise ValueError("and_() needs at least one condition")
    parts = [_as_condition(item) for item in conditions]
    return parts[0] if len(parts) == 1 else And(parts)


def or_(*conditions: Condition | bool) -> Condition:
    """Some condition holds.

    Raises:
        ValueError: No condition is given.
    """
    if not conditions:
        raise ValueError("or_() needs at least one condition")
    parts = [_as_condition(item) for item in conditions]
    return parts[0] if len(parts) == 1 else Or(parts)


def not_(inner: Condition | bool) -> Condition:
    """The condition does not hold."""
    return Not(_as_condition(inner))


class UpdateAction:
    """One action of an update expression (``SET``, ``REMOVE``, ``ADD`` or ``DELETE``)."""

    __slots__ = ("kind", "operand", "path")

    def __init__(
        self, kind: Literal["SET", "REMOVE", "ADD", "DELETE"], path: Path[Any], operand: Operand | None
    ) -> None:
        self.kind = kind
        self.path = path
        self.operand = operand

    def compile(self, builder: ExpressionBuilder) -> str:
        """Renders the action (without its section keyword)."""
        target = builder.path(self.path)
        if self.kind == "SET":
            assert self.operand is not None
            return f"{target} = {self.operand.compile(builder)}"
        if self.kind == "REMOVE":
            return target
        assert self.operand is not None
        return f"{target} {self.operand.compile(builder)}"

    def literal(self) -> tuple[bool, Any]:
        """``(True, value)`` when the action sets a plain value, else ``(False, None)``."""
        if self.kind == "SET" and isinstance(self.operand, Value):
            return True, self.operand.value
        return False, None

    def __repr__(self) -> str:
        builder = ExpressionBuilder()
        return (
            f"UpdateAction({self.kind} {self.compile(builder)!r}, names={builder.names!r}, values={builder.values!r})"
        )


def path_of(value: Path[Any] | str) -> Path[Any]:
    """A path from a path or an attribute name."""
    return value if isinstance(value, Path) else F(value)


def attr[T](value: T) -> Path[T]:
    """Types an attribute of a pydantic (or plain-annotated) model as a path.

    At runtime ``User.age`` already is a path; type checkers see the annotated type (``int``).
    ``attr(User.age)`` gives them ``Path[int]``, so comparisons and updates type-check::

        repo.query(...).filter(dynamoxy.attr(User.age) >= 18)

    Raises:
        TypeError: ``value`` is not an attribute of a model (a plain value was passed).
    """
    if not isinstance(value, Path):
        raise TypeError(f"attr() expects a model attribute such as User.age, got {type(value).__name__}")
    return value


def describe(value: Condition | UpdateAction) -> str:
    """A readable rendering with attribute names substituted (for debugging)."""
    builder = ExpressionBuilder()
    text = value.compile(builder)
    for placeholder, name in builder.names.items():
        text = text.replace(placeholder, name)
    return text
