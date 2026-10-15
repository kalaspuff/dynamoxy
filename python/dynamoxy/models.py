"""Native models: :class:`Model` (items of a table) and :class:`Document` (nested maps).

::

    from datetime import datetime

    import dynamoxy


    class Address(dynamoxy.Document):
        street: dynamoxy.Attr[str]
        city: dynamoxy.Attr[str]


    class User(dynamoxy.Model, table="users"):
        user_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
        email: dynamoxy.Attr[str] = dynamoxy.field(unique=True)
        name: dynamoxy.Attr[str]
        address: dynamoxy.Attr[Address | None] = dynamoxy.field(default=None)
        created_at: dynamoxy.Attr[datetime] = dynamoxy.created_at()

        by_email = dynamoxy.GlobalIndex(partition_key=email)

        @dynamoxy.validator
        def check_name(self) -> None:
            if self.name.strip() != self.name:
                raise ValueError("name must not start or end with spaces")

Models validate on construction and on assignment, and are plain objects: persistence goes
through a :class:`dynamoxy.Repository` (``client.repository(User)``), so models never hold a
connection.
"""

from __future__ import annotations

import copy as _copy
import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar, Self, dataclass_transform

from dynamoxy._utils import MISSING
from dynamoxy.attributes import (
    Attr,
    FieldPath,
    created_at,
    field,
    partition_key,
    sort_key,
    ttl,
    updated_at,
    version,
)
from dynamoxy.errors import FieldError, validation_error
from dynamoxy.schema import (
    ModelOptions,
    ModelSchema,
    ModelState,
    _IndexDeclaration,
    collect_hooks,
    make_field,
    register_entity,
    resolve_annotations,
    schema_of,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from dynamoxy.codecs import DateTimeFormat

__all__ = [
    "Document",
    "Model",
    "after_delete",
    "after_load",
    "after_save",
    "before_delete",
    "before_save",
    "validator",
]

_STATE = "__dynamoxy_state__"


def _mark[F: Callable[..., Any]](function: F, mark: str) -> F:
    marks = getattr(function, "__dynamoxy_hooks__", ())
    function.__dynamoxy_hooks__ = (*marks, mark)  # type: ignore[attr-defined]
    return function


def validator[F: Callable[..., Any]](function: F) -> F:
    """Marks a method as a model validator: ``def check(self) -> None`` raising ``ValueError``.

    Validators run after field validation, on construction and before every write.
    """
    return _mark(function, "validator")


def before_save[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs before the instance is written.

    It runs for put, create, save, batch puts and transactions, may change the instance, and
    aborts the write by raising.
    """
    return _mark(function, "before_save")


def after_save[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs after the instance was written."""
    return _mark(function, "after_save")


def before_delete[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs before the instance is deleted; raising aborts."""
    return _mark(function, "before_delete")


def after_delete[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs after the instance was deleted."""
    return _mark(function, "after_delete")


def after_load[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync) that runs after an instance was read from DynamoDB.

    Useful to migrate old data shapes in memory.
    """
    return _mark(function, "after_load")


class NativeAdapter:
    """Reads and creates instances of native models."""

    #: Field values live in the instance ``__dict__`` (``get`` is ``__dict__.get``).
    dict_backed = True

    def __init__(self, model: type[Document]) -> None:
        self.model = model

    def get(self, instance: Any, name: str) -> Any:
        return instance.__dict__.get(name)

    def set(self, instance: Any, name: str, value: Any) -> None:
        instance.__dict__[name] = value

    def create(self, values: dict[str, Any]) -> Any:
        return self.model(**values)

    def load(self, values: dict[str, Any]) -> Any:
        instance = object.__new__(self.model)
        instance.__dict__.update(values)
        return instance

    def assign(self, instance: Any, source: Any, names: Iterable[str]) -> None:
        """Sets the fields ``names`` of ``instance`` to those of ``source`` (without validation)."""
        values = source.__dict__
        instance.__dict__.update({name: values.get(name) for name in names})

    def load_stored(self, values: dict[str, Any], stored: dict[str, Any]) -> Any:
        """``load`` and record ``stored`` as the instance's stored item, in one step."""
        instance = object.__new__(self.model)
        values[_STATE] = ModelState(stored)
        instance.__dict__.update(values)
        return instance

    def state(self, instance: Any) -> ModelState | None:
        state = instance.__dict__.get(_STATE)
        if state is None:
            state = ModelState()
            instance.__dict__[_STATE] = state
        return state


@dataclass_transform(
    kw_only_default=True,
    # Functions only: with a class among them, type checkers may treat index declarations
    # (``by_email = GlobalIndex(...)``) as fields.
    field_specifiers=(field, partition_key, sort_key, created_at, updated_at, version, ttl),
)
class Document:
    """A structured value stored as a map (``M``): nested in models, never a table item itself.

    Fields are declared exactly like :class:`Model` fields. The ``datetime_format`` class keyword
    works as on :class:`Model`; a document does not take it from the model it is nested in.
    """

    __dynamoxy_fields__: ClassVar[dict[str, FieldPath[Any]]] = {}
    __dynamoxy_options__: ClassVar[ModelOptions] = ModelOptions(abstract=True)

    def __init_subclass__(cls, *, datetime_format: DateTimeFormat | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if datetime_format is not None:
            cls.__dynamoxy_options__ = dataclasses.replace(cls.__dynamoxy_options__, datetime_format=datetime_format)
        cls._collect_fields()

    @classmethod
    def _collect_fields(cls) -> None:
        fields: dict[str, FieldPath[Any]] = {}
        for base in reversed(cls.__mro__[1:]):
            fields.update(base.__dict__.get("__dynamoxy_fields__", {}))
        for name, annotation in own_annotations(cls).items():
            if name.startswith("_") or _is_class_var(annotation):
                continue
            if name in _RESERVED_NAMES:
                raise TypeError(
                    f"{cls.__name__}.{name}: the name is part of the model API ({name}); name the field "
                    f"differently and keep the stored name with attribute_name={name!r}"
                )
            value = cls.__dict__.get(name, MISSING)
            if isinstance(value, FieldPath):
                attribute = value
            else:
                # `name: Attr[int]` without a specifier, or a plain default value.
                attribute = Attr(**({} if value is MISSING else {"default": value}))
                setattr(cls, name, attribute)
                attribute.__set_name__(cls, name)
            fields[name] = attribute
        cls.__dynamoxy_fields__ = fields

    @classmethod
    def __dynamoxy_build_schema__(cls) -> ModelSchema:
        hints = resolve_annotations(cls)
        options = cls.__dynamoxy_options__
        specs = []
        for name, attribute in cls.__dynamoxy_fields__.items():
            annotation = _unwrap_attr(hints.get(name, Any))
            specs.append(
                make_field(
                    cls,
                    name,
                    annotation,
                    options=attribute._options,
                    path=attribute,
                    datetime_format=options.datetime_format,
                )
            )
        validators, hooks = collect_hooks(cls)
        declarations = [value for value in _class_values(cls) if isinstance(value, _IndexDeclaration)]
        return ModelSchema(
            cls,
            options=options,
            fields=specs,
            index_declarations=declarations,
            adapter=NativeAdapter(cls),
            validators=validators,
            hooks=hooks,
        )

    def __init__(self, **values: Any) -> None:
        schema = schema_of(type(self))
        errors: list[FieldError] = []
        settable = schema._settable
        for name in values:
            if name not in settable:
                if name not in schema.fields:
                    raise TypeError(f"{type(self).__name__}() got an unexpected keyword argument {name!r}")
                raise TypeError(f"{type(self).__name__}.{name} is set automatically; do not pass it to the constructor")
        stored = self.__dict__
        for name, settable_in_init, spec, check in schema._init_plan:
            if name in values:
                value = values[name]
            elif not settable_in_init:
                stored[name] = None
                continue
            else:
                value = spec.initial()
                if value is MISSING:
                    errors.append(FieldError(name, "is required"))
                    continue
            try:
                stored[name] = spec.validate(value) if value is None else check(value)
            except ValueError as error:
                errors.append(FieldError(name, str(error), value))
        if errors:
            raise validation_error(
                f"{type(self).__name__} is invalid: " + "; ".join(str(problem) for problem in errors), errors
            )
        for check in schema.validators:
            try:
                check(self)
            except ValueError as error:
                raise validation_error(
                    f"{type(self).__name__} is invalid: {error}", [FieldError("__model__", str(error))]
                ) from None

    def __repr__(self) -> str:
        schema = schema_of(type(self))
        parts = ", ".join(
            f"{spec.name}={self.__dict__.get(spec.name)!r}" for spec in schema.fields.values() if spec.repr
        )
        return f"{type(self).__name__}({parts})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        schema = schema_of(type(self))
        return all(self.__dict__.get(name) == other.__dict__.get(name) for name in schema.fields)

    __hash__ = None  # type: ignore[assignment]  # models are mutable

    def to_dict(self) -> dict[str, Any]:
        """The field values by Python name (nested documents become dicts too)."""
        result: dict[str, Any] = {}
        for name in schema_of(type(self)).fields:
            value = self.__dict__.get(name)
            result[name] = _plain(value)
        return result

    def to_item(self) -> dict[str, Any]:
        """The stored form (attribute names, encoded values, derived keys, type attribute)."""
        return schema_of(type(self)).encode(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Self:
        """An instance from a stored item (the inverse of :meth:`to_item`).

        Raises:
            ItemDecodeError: The item does not fit the model.
        """
        return schema_of(cls).decode(item)

    def validate(self) -> None:
        """Validates every field and the model validators.

        Raises:
            ValidationError: With ``.errors`` listing every problem.
        """
        schema_of(type(self)).validate(self)

    def copy(self, **changes: Any) -> Self:
        """A copy with some fields changed (validated).

        The copy is not "loaded": saving it behaves like saving a new instance.
        """
        clone = _copy.deepcopy(self)
        clone.__dict__.pop(_STATE, None)
        for name, value in changes.items():
            setattr(clone, name, value)
        return clone


def own_annotations(cls: type) -> dict[str, Any]:
    """The annotations a class itself declares, without evaluating them.

    Python 3.14 evaluates annotations lazily (PEP 649); forward references must not fail here.
    """
    try:
        import annotationlib
    except ImportError:
        import inspect

        return dict(inspect.get_annotations(cls))
    return dict(annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF))  # pyrefly: ignore[missing-attribute]


def _is_class_var(annotation: Any) -> bool:
    import typing

    if isinstance(annotation, str):
        return annotation.replace("typing.", "").startswith("ClassVar")
    forward = getattr(annotation, "__forward_arg__", None)
    if isinstance(forward, str):
        return forward.replace("typing.", "").startswith("ClassVar")
    return annotation is typing.ClassVar or typing.get_origin(annotation) is typing.ClassVar


def _plain(value: Any) -> Any:
    if isinstance(value, Document):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(element) for element in value]
    if isinstance(value, dict):
        return {key: _plain(element) for key, element in value.items()}
    return value


def _unwrap_attr(annotation: Any) -> Any:
    """``Attr[X]`` / ``FieldPath[X]`` → ``X`` (other annotations unchanged)."""
    import typing

    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        base, *metadata = typing.get_args(annotation)
        return typing.Annotated[_unwrap_attr(base), *metadata]  # pyrefly: ignore[invalid-annotation]
    if isinstance(origin, type) and issubclass(origin, FieldPath):
        (inner,) = typing.get_args(annotation)
        return inner
    if typing.get_origin(annotation) is typing.ClassVar:
        return annotation
    return annotation


def _class_values(cls: type) -> list[Any]:
    values: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        values.update(vars(klass))
    return list(values.values())


class Model(Document):
    """A DynamoDB item type. Declare its table with class keywords.

    Args (class keywords):
        table: The (logical) table name. Several models may share one table (single-table
            design); the client maps logical names to physical ones (``table_prefix=``).
        entity: The entity name written to the type attribute, so mixed query results can be
            decoded into the right model (default: the class name when ``type_attribute`` is set).
        type_attribute: The attribute holding the entity name (``"_type"`` by default when
            several models share a table; ``None`` to not store one).
        abstract: A base class with shared fields, not a table item type itself.
        datetime_format: How ``datetime`` fields (also in lists, sets and maps, and automatic
            timestamps) are stored unless they choose a codec: ``"iso"``
            (``2026-10-13T08:48:47.966937+00:00``, the default), ``"iso_z"``
            (``2026-10-13T08:48:47.966937Z``), ``"epoch"`` or ``"epoch_ms"``. Inherited.

    Example::

        class Order(dynamoxy.Model, table="app", entity="Order"):
            pk: dynamoxy.Attr[str] = dynamoxy.partition_key(template="CUSTOMER#{customer_id}")
            sk: dynamoxy.Attr[str] = dynamoxy.sort_key(template="ORDER#{order_id}")
            customer_id: dynamoxy.Attr[str]
            order_id: dynamoxy.Attr[str]
    """

    __dynamoxy_options__: ClassVar[ModelOptions] = ModelOptions(abstract=True)

    def __init_subclass__(
        cls,
        *,
        table: str | None = None,
        entity: str | None = None,
        type_attribute: str | None = MISSING,
        abstract: bool = False,
        datetime_format: DateTimeFormat | None = None,
        **kwargs: Any,
    ) -> None:
        inherited = cls.__dynamoxy_options__
        resolved_table = table if table is not None else (inherited.table if not abstract else None)
        resolved_type_attribute = inherited.type_attribute if type_attribute is MISSING else type_attribute
        if resolved_type_attribute is None and entity is not None:
            resolved_type_attribute = "_type"
        cls.__dynamoxy_options__ = ModelOptions(
            table=resolved_table,
            entity=entity if entity is not None else (cls.__name__ if resolved_type_attribute else None),
            type_attribute=resolved_type_attribute,
            abstract=abstract or resolved_table is None,
            datetime_format=inherited.datetime_format if datetime_format is None else datetime_format,
        )
        super().__init_subclass__(**kwargs)
        options = cls.__dynamoxy_options__
        if not options.abstract:
            register_entity(options.table, options.entity, cls)

    def primary_key(self) -> dict[str, Any]:
        """The stored primary key of this instance (attribute names to stored values).

        Raises:
            ValueError: A key component is not set.
        """
        return schema_of(type(self)).key_of(self)

    @classmethod
    def key(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """A stored primary key from values: ``User.key("u1")``, ``Order.key(customer_id="c", order_id="o")``.

        Raises:
            ValueError: Arguments are missing or invalid.
        """
        return schema_of(cls).key_from(args, kwargs)

    @property
    def is_loaded(self) -> bool:
        """Whether this instance was read from or written to DynamoDB."""
        state = self.__dict__.get(_STATE)
        return state is not None and state.loaded


#: Names of the model API that fields must not shadow.
_RESERVED_NAMES = frozenset(name for name in (*vars(Document), *vars(Model)) if not name.startswith("_"))
