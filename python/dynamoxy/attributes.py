"""Model attributes: the :class:`Attr` descriptor and the field specifiers that configure it.

Annotate each field of a :class:`dynamoxy.Model` with ``Attr[T]`` and configure it with a field
specifier::

    class User(dynamoxy.Model, table="users"):
        user_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
        email: dynamoxy.Attr[str] = dynamoxy.field(unique=True, max_length=320)
        name: dynamoxy.Attr[str] = dynamoxy.field(min_length=1)
        age: dynamoxy.Attr[int | None] = dynamoxy.field(default=None, ge=0)
        tags: dynamoxy.Attr[set[str]] = dynamoxy.field(default_factory=set)
        created_at: dynamoxy.Attr[datetime] = dynamoxy.created_at()
        version: dynamoxy.Attr[int] = dynamoxy.version()

Type checkers then know the constructor (``User(user_id=..., email=...)``), instance values
(``user.age`` is ``int | None``) and class attributes, which are typed paths for expressions
(``User.age >= 18``).

Pydantic models use the same options as ``Annotated`` markers (:class:`PartitionKey`,
:class:`Unique`, ...); see :class:`dynamoxy.PydanticModel`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self, overload, override

from dynamoxy._utils import MISSING
from dynamoxy.errors import FieldError, validation_error
from dynamoxy.expressions import Operand, Path, Value

if TYPE_CHECKING:
    from collections.abc import Callable

    from dynamoxy.codecs import Codec, DateTimeFormat
    from dynamoxy.schema import FieldSpec

__all__ = [
    "TTL",
    "Attr",
    "AttributeName",
    "CreatedAt",
    "FieldPath",
    "PartitionKey",
    "SortKey",
    "Unique",
    "UpdatedAt",
    "Version",
    "created_at",
    "field",
    "partition_key",
    "sort_key",
    "ttl",
    "updated_at",
    "version",
]


class FieldPath[T](Path[T]):
    """The class attribute of a model field: an expression path whose values use the field's codec.

    On pydantic models the instance value lives in the instance ``__dict__``, so this
    non-data descriptor only answers class-level access.
    """

    def __init__(self, **options: Any) -> None:
        # The attribute name is only known in __set_name__.
        self._options = options
        self._owner: type | None = None
        self._name: str = ""
        self._elements = ("?",)
        self._encoder = None
        self._spec: FieldSpec | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self._owner = owner
        self._name = name
        self._elements = (self._options.get("attribute_name") or name,)

    @property
    def field_name(self) -> str:
        """The Python name of the field."""
        return self._name

    @property
    def model(self) -> type:
        """The model class the field belongs to."""
        if self._owner is None:
            raise TypeError("this attribute is not declared on a model class")
        return self._owner

    @property
    def spec(self) -> FieldSpec:
        """The resolved field (codec, attribute name, constraints, ...)."""
        spec = self._spec
        if spec is None:
            from dynamoxy.schema import schema_of

            # Schemas are built once per class and never change: remember the field.
            spec = self._spec = schema_of(self.model).fields[self._name]
        return spec

    @property
    def encoder(self) -> Codec[Any] | None:  # type: ignore[override]
        """The field's codec (resolved lazily)."""
        return self.spec.codec

    def _value(self, value: Any) -> Any:
        spec = self.spec
        if isinstance(value, Operand):
            return value
        if value is None:
            return Value(None)
        try:
            checked = spec.codec.validate(value)
        except ValueError as error:
            raise validation_error(
                f"{self.model.__name__}.{self._name}: {error}", [FieldError(self._name, str(error), value)]
            ) from None
        return Value(spec.codec.encode(checked))

    @override
    def _none_is_missing(self) -> bool:
        return True

    def __getitem__(self, key: str | int) -> Path[Any]:
        codec = self.spec.codec
        encoder = codec.element() if isinstance(key, int) else codec.member(key)
        return Path(*self._elements, key, encoder=encoder)

    def contains(self, value: Any) -> Any:
        """``contains(path, value)``: substring, set member or list element."""
        element = self.spec.codec.element()
        return Path(*self._elements, encoder=element).contains(value) if element else super().contains(value)

    def __repr__(self) -> str:
        owner = self._owner.__name__ if self._owner is not None else "?"
        return f"<attribute {owner}.{self._name}>"

    __hash__ = Path.__hash__

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, instance: object, owner: type[Any]) -> T: ...
    def __get__(self, instance: object | None, owner: type[Any]) -> Self | T:
        if instance is None:
            return self
        try:
            return instance.__dict__[self._name]
        except KeyError:
            raise AttributeError(f"{type(instance).__name__}.{self._name} is not set") from None


class Attr[T](FieldPath[T]):
    """A field of a :class:`dynamoxy.Model`: the value on instances, a typed path on the class.

    Annotate fields with it (``name: Attr[str] = field()``). Assigning a value validates it
    (type, constraints, validators) and raises :class:`dynamoxy.ValidationError` when invalid.
    """

    def __set__(self, instance: object, value: T) -> None:
        spec = self.spec
        if spec.template is not None:
            owner = type(instance).__name__
            raise AttributeError(
                f"{owner}.{self._name} is derived from {spec.template.template!r}; set its fields instead"
            )
        try:
            checked = spec.validate(value)
        except ValueError as error:
            raise validation_error(
                f"{type(instance).__name__}.{self._name}: {error}", [FieldError(self._name, str(error), value)]
            ) from None
        instance.__dict__[self._name] = checked

    def __delete__(self, instance: object) -> None:
        raise AttributeError(f"cannot delete {type(instance).__name__}.{self._name}; set it to None if it is optional")


def _options(**options: Any) -> dict[str, Any]:
    """The options that were given.

    ``MISSING`` means "not given", and so does ``None``, except for ``default`` (where ``None`` is
    a real default value).
    """
    return {
        name: value
        for name, value in options.items()
        if value is not MISSING and (value is not None or name == "default")
    }


@overload
def field(
    *,
    template: str,
    init: Literal[False] = False,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    description: str | None = None,
    repr: bool = True,
) -> Any: ...
@overload
def field(
    *,
    default: Any = ...,
    default_factory: Callable[[], Any] | None = None,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    unique: bool = False,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    ge: Any = None,
    gt: Any = None,
    le: Any = None,
    lt: Any = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    immutable: bool = False,
    description: str | None = None,
    repr: bool = True,
) -> Any: ...
def field(
    *,
    default: Any = MISSING,
    default_factory: Callable[[], Any] | None = None,
    template: str | None = None,
    init: bool = True,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    unique: bool = False,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    ge: Any = None,
    gt: Any = None,
    le: Any = None,
    lt: Any = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    immutable: bool = False,
    description: str | None = None,
    repr: bool = True,
) -> Any:
    """Configures a field of a :class:`dynamoxy.Model`.

    Args:
        default: Value used when none is given.
        default_factory: Creates the value when none is given (``list``, ``uuid4``, ...).
        template: Derive the value from other fields (``"STATUS#{status}"``); such fields are
            not passed to the constructor and are recomputed on every write. Typical for
            generic index keys in single-table design.
        init: Ignored at runtime (type checkers read it); derived fields are not in ``__init__``.
        attribute_name: The DynamoDB attribute name (default: the field name).
        codec: How the value is stored (default: from the annotation, see ``dynamoxy.codecs``).
        unique: No two items may have the same value (enforced with guard items in
            transactions; see the constraints guide).
        min_length: Minimum length of strings, lists, sets and maps.
        max_length: Maximum length.
        pattern: Regular expression strings must match entirely.
        ge: Values must be greater than or equal to this.
        gt: Values must be greater than this.
        le: Values must be less than or equal to this.
        lt: Values must be less than this.
        validators: Callables ``value -> value`` raising ``ValueError`` for invalid values.
        immutable: The value cannot change once stored: updates of it, and writes of a loaded
            instance whose value changed, raise ``ValueError``.
        description: Documentation of the field.
        repr: Show the field in ``repr()``.
    """
    del init
    if template is not None and (default is not MISSING or default_factory is not None):
        raise TypeError("a templated field cannot have a default; its value comes from the template")
    return Attr(
        **_options(
            default=default,
            default_factory=default_factory,
            template=template,
            attribute_name=attribute_name,
            codec=codec,
            unique=unique or None,
            min_length=min_length,
            max_length=max_length,
            pattern=pattern,
            ge=ge,
            gt=gt,
            le=le,
            lt=lt,
            validators=validators or None,
            immutable=immutable or None,
            description=description,
            repr=repr,
        )
    )


@overload
def partition_key(
    *,
    template: str,
    init: Literal[False] = False,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    description: str | None = None,
) -> Any: ...
@overload
def partition_key(
    *,
    default: Any = ...,
    default_factory: Callable[[], Any] | None = None,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    description: str | None = None,
) -> Any: ...
def partition_key(
    *,
    default: Any = MISSING,
    default_factory: Callable[[], Any] | None = None,
    template: str | None = None,
    init: bool = True,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    description: str | None = None,
) -> Any:
    """The partition (hash) key field. With ``template=`` it is built from other fields.

    Example::

        user_id: Attr[str] = partition_key()
        pk: Attr[str] = partition_key(template="USER#{user_id}")
    """
    attribute = field(
        default=default,
        default_factory=default_factory,
        template=template,  # pyrefly: ignore[unexpected-keyword]
        init=init,  # pyrefly: ignore[unexpected-keyword]
        attribute_name=attribute_name,
        codec=codec,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        validators=validators,
        immutable=True,
        description=description,
    )
    attribute._options["role"] = "partition"
    return attribute


@overload
def sort_key(
    *,
    template: str,
    init: Literal[False] = False,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    description: str | None = None,
) -> Any: ...
@overload
def sort_key(
    *,
    default: Any = ...,
    default_factory: Callable[[], Any] | None = None,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    description: str | None = None,
) -> Any: ...
def sort_key(
    *,
    default: Any = MISSING,
    default_factory: Callable[[], Any] | None = None,
    template: str | None = None,
    init: bool = True,
    attribute_name: str | None = None,
    codec: Codec[Any] | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    validators: tuple[Callable[[Any], Any], ...] = (),
    description: str | None = None,
) -> Any:
    """The sort (range) key field. With ``template=`` it is built from other fields.

    Example::

        sk: Attr[str] = sort_key(template="ORDER#{created_at}#{order_id}")
    """
    attribute = field(
        default=default,
        default_factory=default_factory,
        template=template,  # pyrefly: ignore[unexpected-keyword]
        init=init,  # pyrefly: ignore[unexpected-keyword]
        attribute_name=attribute_name,
        codec=codec,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        validators=validators,
        immutable=True,
        description=description,
    )
    attribute._options["role"] = "sort"
    return attribute


def _auto(
    auto: str,
    attribute_name: str | None,
    codec: Codec[Any] | None,
    default: Any = MISSING,
    description: str | None = None,
) -> Any:
    return Attr(
        **_options(auto=auto, attribute_name=attribute_name, codec=codec, default=default, description=description)
    )


def _timestamp_codec(format: DateTimeFormat | None) -> Codec[Any] | None:
    """The codec of an automatic timestamp; ``None`` uses the model's ``datetime_format``."""
    if format is None:
        return None
    from dynamoxy.codecs import DateTimeCodec

    return DateTimeCodec(format)


def created_at(
    *,
    init: Literal[False] = False,
    attribute_name: str | None = None,
    format: DateTimeFormat | None = None,
    description: str | None = None,
) -> Any:
    """A ``datetime`` set when the item is first written (not passed to the constructor).

    Args:
        init: Ignored at runtime (type checkers read it).
        attribute_name: The DynamoDB attribute name.
        format: ``"iso"`` or ``"iso_z"`` (sortable text ending in ``+00:00`` or ``Z``), ``"epoch"``
            or ``"epoch_ms"`` (numbers); default: the model's ``datetime_format``.
        description: Documentation of the field.
    """
    del init
    return _auto("created_at", attribute_name, _timestamp_codec(format), description=description)


def updated_at(
    *,
    init: Literal[False] = False,
    attribute_name: str | None = None,
    format: DateTimeFormat | None = None,
    description: str | None = None,
) -> Any:
    """A ``datetime`` set on every write (``put``, ``save``, ``update``), not passed to the constructor.

    Takes the same arguments as :func:`created_at`.
    """
    del init
    return _auto("updated_at", attribute_name, _timestamp_codec(format), description=description)


def version(*, init: Literal[False] = False, attribute_name: str | None = None, description: str | None = None) -> Any:
    """An ``int`` version for optimistic locking, starting at 1 and incremented on every write.

    ``save()``, ``update()`` and ``delete()`` of a loaded instance only succeed if the stored
    version is still the one it was loaded with; otherwise :class:`dynamoxy.VersionConflictError`.
    """
    del init
    return _auto("version", attribute_name, None, description=description)


def ttl(*, default: Any = MISSING, attribute_name: str | None = None, description: str | None = None) -> Any:
    """The Time to Live attribute: when DynamoDB may delete the item.

    A ``datetime`` (or ``int`` epoch seconds) after which DynamoDB deletes the item, typically
    within a few days; reads may still return it until then. Like any field it is required
    unless it has a default: ``expires_at: Attr[datetime | None] = ttl(default=None)``.

    Stored as epoch seconds. ``client.create_tables()`` and ``ensure_tables()`` enable TTL on it.
    """
    return Attr(**_options(auto="ttl", attribute_name=attribute_name, default=default, description=description))


# ---- markers for pydantic models (Annotated[...] metadata) ----------------------------------


class PartitionKey:
    """``Annotated`` marker: the partition key of a pydantic model (``template=`` builds it)."""

    __slots__ = ("template",)

    def __init__(self, *, template: str | None = None) -> None:
        self.template = template

    def __repr__(self) -> str:
        return f"PartitionKey(template={self.template!r})"


class SortKey:
    """``Annotated`` marker: the sort key of a pydantic model (``template=`` builds it)."""

    __slots__ = ("template",)

    def __init__(self, *, template: str | None = None) -> None:
        self.template = template

    def __repr__(self) -> str:
        return f"SortKey(template={self.template!r})"


class Unique:
    """``Annotated`` marker: the attribute's values are unique across items."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Unique()"


class AttributeName:
    """``Annotated`` marker: the DynamoDB attribute name of a field."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"AttributeName({self.name!r})"


class Template:
    """``Annotated`` marker: the field is derived from others (``Template("STATUS#{status}")``)."""

    __slots__ = ("template",)

    def __init__(self, template: str) -> None:
        self.template = template

    def __repr__(self) -> str:
        return f"Template({self.template!r})"


class CreatedAt:
    """``Annotated`` marker: set when the item is first written (``format`` as in :func:`created_at`)."""

    __slots__ = ("format",)

    def __init__(self, format: DateTimeFormat | None = None) -> None:
        self.format = format


class UpdatedAt:
    """``Annotated`` marker: set on every write (``format`` as in :func:`created_at`)."""

    __slots__ = ("format",)

    def __init__(self, format: DateTimeFormat | None = None) -> None:
        self.format = format


class Version:
    """``Annotated`` marker: optimistic locking version."""

    __slots__ = ()


class TTL:
    """``Annotated`` marker: the Time to Live attribute (epoch seconds)."""

    __slots__ = ()
