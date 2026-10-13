"""Model schemas: the one description of a model that every operation uses.

Native models (:class:`dynamoxy.Model`) and pydantic models (:class:`dynamoxy.PydanticModel`)
both produce a :class:`ModelSchema`: the fields with their codecs and DynamoDB attribute names,
the primary key (optionally built from :class:`KeyTemplate` strings for single-table design),
secondary indexes, unique attributes, versioning, timestamps, TTL, hooks and validators.

Schemas are built lazily, on first use, so models can refer to classes defined later.
"""

from __future__ import annotations

import string
import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from dynamoxy._utils import MISSING
from dynamoxy.codecs import Codec, DateTimeCodec, OptionalCodec, _check_datetime_format, resolve_codec
from dynamoxy.errors import FieldError, ItemDecodeError, validation_error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from dynamoxy.attributes import FieldPath
    from dynamoxy.codecs import DateTimeFormat

__all__ = [
    "FieldSpec",
    "GlobalIndex",
    "IndexSpec",
    "KeyTemplate",
    "LocalIndex",
    "ModelSchema",
    "ModelState",
    "Projection",
    "Throughput",
    "schema_of",
]

type KeyRole = Literal["partition", "sort"]
type AutoField = Literal["created_at", "updated_at", "version", "ttl"]
type HookPoint = Literal["before_save", "after_save", "before_delete", "after_delete", "after_load"]

HOOK_POINTS: tuple[HookPoint, ...] = ("before_save", "after_save", "before_delete", "after_delete", "after_load")


class KeyTemplate:
    """A key built from other attributes: ``KeyTemplate("USER#{user_id}#ORDER#{order_id}")``.

    Placeholders name fields of the model. Values are rendered in their stored form (ISO text for
    datetimes, the value of enums), or with a format spec: ``{created_at:%Y-%m}``, ``{n:08d}``.
    The text before the first placeholder (``"USER#"``) is the template's :attr:`prefix`, which
    queries use to select the items of one entity type in a shared partition.
    """

    def __init__(self, template: str) -> None:
        if not isinstance(template, str) or not template:
            raise ValueError("a key template is a non-empty string such as 'USER#{user_id}'")
        try:
            parsed = list(string.Formatter().parse(template))
        except ValueError as error:
            raise ValueError(f"invalid key template {template!r}: {error}") from None
        parts: list[tuple[str, str | None, str]] = []
        for literal, field_name, spec, conversion in parsed:
            if conversion:
                raise ValueError(f"invalid key template {template!r}: conversions such as !r are not supported")
            if field_name is not None and not field_name.isidentifier():
                raise ValueError(f"invalid key template {template!r}: {{{field_name}}} must name a field")
            parts.append((literal, field_name, spec or ""))
        self.template = template
        self._parts = parts

    @property
    def fields(self) -> list[str]:
        """The fields the template reads, in order."""
        return [name for _, name, _ in self._parts if name is not None]

    @property
    def prefix(self) -> str:
        """The constant text before the first placeholder."""
        prefix = ""
        for literal, name, _ in self._parts:
            prefix += literal
            if name is not None:
                break
        return prefix

    @property
    def is_constant(self) -> bool:
        """Whether the template has no placeholders (``"PROFILE"``)."""
        return not self.fields

    def render(self, values: Mapping[str, Any], codecs: Mapping[str, Codec[Any]]) -> str | None:
        """The key for ``values`` (Python values by field name), or ``None`` if one is missing."""
        text = ""
        for literal, name, spec in self._parts:
            text += literal
            if name is None:
                continue
            value = values.get(name)
            if value is None:
                return None
            if spec:
                text += format(value, spec)
                continue
            codec = codecs.get(name)
            stored = codec.encode(value) if codec is not None else value
            if isinstance(stored, bytes):
                raise ValueError(f"key template {self.template!r}: {name} is binary and cannot be part of a text key")
            text += str(stored)
        return text

    def __repr__(self) -> str:
        return f"KeyTemplate({self.template!r})"


@dataclass(frozen=True, slots=True)
class Throughput:
    """Provisioned capacity of a table or global secondary index.

    Attributes:
        read: Read capacity units.
        write: Write capacity units.
    """

    read: int
    write: int

    def __post_init__(self) -> None:
        if self.read < 1 or self.write < 1:
            raise ValueError(f"provisioned throughput must be at least 1 read and 1 write unit, got {self}")


@dataclass(frozen=True, slots=True)
class Projection:
    """Which attributes an index copies: ``Projection.all()``, ``.keys_only()`` or ``.include(...)``.

    Attributes:
        type: ``"ALL"``, ``"KEYS_ONLY"`` or ``"INCLUDE"``.
        attributes: Extra attributes copied with ``INCLUDE`` (Python field names or attribute names).
    """

    type: Literal["ALL", "KEYS_ONLY", "INCLUDE"] = "ALL"
    attributes: tuple[str, ...] = ()

    @classmethod
    def all(cls) -> Projection:
        """Every attribute (queries return full items)."""
        return cls("ALL")

    @classmethod
    def keys_only(cls) -> Projection:
        """Only the table and index keys."""
        return cls("KEYS_ONLY")

    @classmethod
    def include(cls, *attributes: str | FieldPath[Any]) -> Projection:
        """The keys plus ``attributes``."""
        names = tuple(attribute if isinstance(attribute, str) else attribute.field_name for attribute in attributes)
        if not names:
            raise ValueError("Projection.include() needs at least one attribute")
        return cls("INCLUDE", names)


class _IndexDeclaration:
    """Shared behaviour of :class:`GlobalIndex` and :class:`LocalIndex`."""

    kind: ClassVar[Literal["global", "local"]]

    def __init__(
        self,
        name: str | None,
        *,
        partition_key: str | FieldPath[Any] | None,
        sort_key: str | FieldPath[Any] | None,
        projection: Projection | Literal["ALL", "KEYS_ONLY"] | None,
        throughput: Throughput | None,
    ) -> None:
        self.name = name
        self.partition_key = partition_key
        self.sort_key = sort_key
        if projection is None or projection == "ALL":
            self.projection = Projection.all()
        elif projection == "KEYS_ONLY":
            self.projection = Projection.keys_only()
        elif isinstance(projection, Projection):
            self.projection = projection
        else:
            raise ValueError(f"projection must be a Projection, 'ALL' or 'KEYS_ONLY', got {projection!r}")
        self.throughput = throughput
        self.owner: type | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        if self.name is None:
            self.name = name
        self.owner = owner

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    def spec(self) -> IndexSpec:
        """The resolved index of the owning model."""
        if self.owner is None or self.name is None:
            raise TypeError(f"{self!r} is not declared on a model class")
        return schema_of(self.owner).indexes[self.name]


def _field_name(value: str | FieldPath[Any] | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.field_name


class GlobalIndex(_IndexDeclaration):
    """A global secondary index, declared as a model class attribute.

    Example::

        class Order(dynamoxy.Model, table="orders"):
            order_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
            status: dynamoxy.Attr[str] = dynamoxy.field()
            created_at: dynamoxy.Attr[datetime] = dynamoxy.created_at()

            by_status = dynamoxy.GlobalIndex(partition_key=status, sort_key=created_at)

    The index name defaults to the class attribute name (``"by_status"``). Items without the
    index key attributes are not in the index (sparse indexes).

    Args:
        name: Index name (default: the attribute name).
        partition_key: The field (or its name) of the index partition key.
        sort_key: The field (or its name) of the index sort key.
        projection: Copied attributes (default ``Projection.all()``).
        throughput: Capacity, for provisioned tables.
    """

    kind = "global"

    def __init__(
        self,
        name: str | None = None,
        *,
        partition_key: str | FieldPath[Any],
        sort_key: str | FieldPath[Any] | None = None,
        projection: Projection | Literal["ALL", "KEYS_ONLY"] | None = None,
        throughput: Throughput | None = None,
    ) -> None:
        super().__init__(
            name, partition_key=partition_key, sort_key=sort_key, projection=projection, throughput=throughput
        )


class LocalIndex(_IndexDeclaration):
    """A local secondary index (same partition key as the table, another sort key).

    Local indexes can only be created with the table, allow strongly consistent reads, and
    limit each partition (item collection) to 10 GB.

    Args:
        name: Index name (default: the attribute name).
        sort_key: The field (or its name) of the index sort key.
        projection: Copied attributes (default ``Projection.all()``).
    """

    kind = "local"

    def __init__(
        self,
        name: str | None = None,
        *,
        sort_key: str | FieldPath[Any],
        projection: Projection | Literal["ALL", "KEYS_ONLY"] | None = None,
    ) -> None:
        super().__init__(name, partition_key=None, sort_key=sort_key, projection=projection, throughput=None)


@dataclass(eq=False)
class Constraints:
    """Validation constraints of a field (``None`` means unconstrained)."""

    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    ge: Any = None
    gt: Any = None
    le: Any = None
    lt: Any = None

    def check(self, value: Any) -> str | None:
        """A message describing the violated constraint, or ``None``."""
        if value is None:
            return None
        if self.min_length is not None or self.max_length is not None:
            try:
                length = len(value)
            except TypeError:
                length = None
            if length is not None:
                if self.min_length is not None and length < self.min_length:
                    return f"must have at least {self.min_length} characters/elements, has {length}"
                if self.max_length is not None and length > self.max_length:
                    return f"must have at most {self.max_length} characters/elements, has {length}"
        if self.pattern is not None and isinstance(value, str):
            import re

            if re.fullmatch(self.pattern, value) is None:
                return f"must match the pattern {self.pattern!r}"
        for bound, check, words in (
            (self.ge, lambda v, b: v >= b, "greater than or equal to"),
            (self.gt, lambda v, b: v > b, "greater than"),
            (self.le, lambda v, b: v <= b, "less than or equal to"),
            (self.lt, lambda v, b: v < b, "less than"),
        ):
            if bound is not None:
                try:
                    ok = check(value, bound)
                except TypeError:
                    ok = False
                if not ok:
                    return f"must be {words} {bound!r}"
        return None


@dataclass(eq=False)
class FieldSpec:
    """One field of a model, resolved.

    Attributes:
        name: The Python name.
        attribute: The DynamoDB attribute name.
        codec: Converts values to and from their stored form.
        annotation: The resolved type annotation.
        default: The default value, or ``MISSING``.
        default_factory: Creates the default value, or ``None``.
        role: ``"partition"`` or ``"sort"`` for primary key fields.
        template: Builds the value from other fields (derived fields are not set by hand).
        unique: Enforce uniqueness across items (see the constraints guide).
        auto: ``"created_at"``, ``"updated_at"``, ``"version"`` or ``"ttl"``.
        constraints: Length, pattern and range constraints.
        validators: Callables ``value -> value`` that raise ``ValueError``.
        immutable: The value cannot change once the item is stored.
        repr: Shown in ``repr()``.
        description: Documentation.
        path: The class attribute (expression path) of the field.
    """

    name: str
    attribute: str
    codec: Codec[Any]
    annotation: Any = None
    default: Any = MISSING
    default_factory: Callable[[], Any] | None = None
    role: KeyRole | None = None
    template: KeyTemplate | None = None
    unique: bool = False
    auto: AutoField | None = None
    constraints: Constraints = field(default_factory=Constraints)
    validators: tuple[Callable[[Any], Any], ...] = ()
    immutable: bool = False
    repr: bool = True
    description: str | None = None
    path: FieldPath[Any] | None = None

    @property
    def has_default(self) -> bool:
        """Whether a value is supplied when none is given."""
        return self.default is not MISSING or self.default_factory is not None

    @property
    def optional(self) -> bool:
        """Whether ``None`` is an allowed value."""
        return isinstance(self.codec, OptionalCodec)

    @property
    def derived(self) -> bool:
        """Whether the value is computed (key templates)."""
        return self.template is not None

    @property
    def settable_in_init(self) -> bool:
        """Whether the constructor accepts this field."""
        return self.template is None and self.auto not in ("created_at", "updated_at", "version")

    def initial(self) -> Any:
        """The default value (a new one per call for factories)."""
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is not MISSING:
            return self.default
        return MISSING

    def validate(self, value: Any) -> Any:
        """Checks a value for this field: type (codec), constraints and validators.

        Raises:
            ValueError: The value is invalid (the message says why).
        """
        if value is None:
            if not self.optional and self.auto is None and self.template is None:
                raise ValueError("must not be None")
            return None
        value = self.codec.validate(value)
        problem = self.constraints.check(value)
        if problem is not None:
            raise ValueError(problem)
        for validator in self.validators:
            value = validator(value)
        return value


@dataclass(eq=False)
class IndexSpec:
    """A resolved secondary index.

    Attributes:
        name: Index name.
        kind: ``"global"`` or ``"local"``.
        partition_key: Field of the index partition key.
        sort_key: Field of the index sort key, if any.
        projection: Copied attributes.
        throughput: Capacity, for provisioned tables.
    """

    name: str
    kind: Literal["global", "local"]
    partition_key: FieldSpec
    sort_key: FieldSpec | None
    projection: Projection
    throughput: Throughput | None = None


@dataclass(slots=True)
class ModelState:
    """What dynamoxy knows about an instance's stored copy.

    Attributes:
        stored: The item as last read or written (stored form), or ``None`` for a new instance.
    """

    stored: dict[str, Any] | None = None

    @property
    def loaded(self) -> bool:
        """Whether the instance was read from or written to DynamoDB."""
        return self.stored is not None


class ModelAdapter(Protocol):
    """How the schema reads, writes and creates instances of one kind of model class."""

    def get(self, instance: Any, name: str) -> Any:
        """The value of a field (``None`` when unset)."""
        ...

    def set(self, instance: Any, name: str, value: Any) -> None:
        """Sets a field without validation (the schema validated it)."""
        ...

    def create(self, values: dict[str, Any]) -> Any:
        """A validated instance from Python values (user input)."""
        ...

    def load(self, values: dict[str, Any]) -> Any:
        """An instance from decoded stored values."""
        ...

    def state(self, instance: Any) -> ModelState | None:
        """The instance's persistence state (``None`` for classes that do not track it)."""
        ...


@dataclass(eq=False)
class ModelOptions:
    """Class keyword options (``class User(Model, table="users", entity="User")``)."""

    table: str | None = None
    entity: str | None = None
    type_attribute: str | None = None
    abstract: bool = False
    datetime_format: DateTimeFormat = "iso"

    def __post_init__(self) -> None:
        _check_datetime_format(self.datetime_format, "datetime_format")


_REGISTRY: dict[tuple[str, str], type] = {}


def models_for_table(table: str) -> dict[str, type]:
    """Models registered for a (logical) table, by entity name."""
    return {entity: model for (name, entity), model in _REGISTRY.items() if name == table}


def register_entity(table: str | None, entity: str | None, model: type) -> None:
    """Registers a model under its table and entity name (for decoding mixed collections)."""
    if table is None or entity is None:
        return
    _REGISTRY[(table, entity)] = model


class ModelSchema:
    """Everything dynamoxy needs to persist one model class."""

    def __init__(
        self,
        model: type,
        *,
        options: ModelOptions,
        fields: list[FieldSpec],
        index_declarations: Iterable[_IndexDeclaration],
        adapter: ModelAdapter,
        validators: list[Callable[[Any], None]],
        hooks: dict[HookPoint, list[Callable[..., Any]]],
    ) -> None:
        self.model = model
        self.options = options
        self.adapter = adapter
        self.validators = validators
        self.hooks = hooks
        self.fields: dict[str, FieldSpec] = {spec.name: spec for spec in fields}
        self.attributes: dict[str, FieldSpec] = {}
        for spec in fields:
            if spec.attribute in self.attributes:
                other = self.attributes[spec.attribute]
                raise TypeError(f"{model.__name__}: {other.name} and {spec.name} are both stored as {spec.attribute!r}")
            self.attributes[spec.attribute] = spec
        self.table = options.table
        self.entity = options.entity
        self.type_attribute = options.type_attribute
        if self.type_attribute is not None and self.type_attribute in self.attributes:
            raise TypeError(
                f"{model.__name__}: the type attribute {self.type_attribute!r} clashes with a field; "
                "choose another type_attribute="
            )
        partitions = [spec for spec in fields if spec.role == "partition"]
        sorts = [spec for spec in fields if spec.role == "sort"]
        self.is_table = self.table is not None and not options.abstract
        if self.is_table:
            if len(partitions) != 1:
                raise TypeError(
                    f"{model.__name__} (table {self.table!r}) needs exactly one partition key field, "
                    f"found {len(partitions)}; declare one with dynamoxy.partition_key()"
                )
            if len(sorts) > 1:
                raise TypeError(f"{model.__name__} has {len(sorts)} sort key fields; at most one is allowed")
        self.partition_key: FieldSpec | None = partitions[0] if partitions else None
        self.sort_key: FieldSpec | None = sorts[0] if sorts else None
        for spec in (self.partition_key, self.sort_key):
            if spec is not None and spec.codec.key_type() is None:
                raise TypeError(
                    f"{model.__name__}.{spec.name}: key attributes must be stored as text, numbers or bytes, "
                    f"not {spec.codec.describe()}"
                )
            if spec is not None and spec.optional and spec.template is None:
                raise TypeError(f"{model.__name__}.{spec.name}: key attributes cannot be optional")
        for spec in fields:
            if spec.template is not None:
                for name in spec.template.fields:
                    if name not in self.fields:
                        raise TypeError(
                            f"{model.__name__}.{spec.name}: the template {spec.template.template!r} refers to "
                            f"{name!r}, which is not a field"
                        )
                    if self.fields[name].template is not None:
                        raise TypeError(
                            f"{model.__name__}.{spec.name}: templates cannot refer to derived fields ({name})"
                        )
        self.unique_fields = [spec for spec in fields if spec.unique]
        for spec in self.unique_fields:
            if spec.role is not None:
                raise TypeError(f"{model.__name__}.{spec.name}: key attributes are unique already; remove unique=True")
        self.version_field = self._single(fields, "version")
        self.created_at_field = self._single(fields, "created_at")
        self.updated_at_field = self._single(fields, "updated_at")
        self.ttl_field = self._single(fields, "ttl")
        self.indexes: dict[str, IndexSpec] = {}
        for declaration in index_declarations:
            self._add_index(declaration)
        self.codecs = {spec.name: spec.codec for spec in fields}

    def _single(self, fields: list[FieldSpec], auto: AutoField) -> FieldSpec | None:
        matching = [spec for spec in fields if spec.auto == auto]
        if len(matching) > 1:
            raise TypeError(f"{self.model.__name__} declares {len(matching)} {auto} fields; at most one is allowed")
        return matching[0] if matching else None

    def _resolve_field(self, value: str | FieldPath[Any] | None, what: str) -> FieldSpec:
        name = _field_name(value)
        if name is None:
            raise TypeError(f"{self.model.__name__}: {what} is required")
        spec = self.fields.get(name) or self.attributes.get(name)
        if spec is None:
            raise TypeError(f"{self.model.__name__}: {what} {name!r} is not a field")
        if spec.codec.key_type() is None:
            kind = spec.codec.describe()
            raise TypeError(
                f"{self.model.__name__}: {what} {name!r} must be stored as text, numbers or bytes, not {kind}"
            )
        return spec

    def _add_index(self, declaration: _IndexDeclaration) -> None:
        name = declaration.name
        if name is None:
            raise TypeError(f"{self.model.__name__}: an index has no name")
        if name in self.indexes:
            raise TypeError(f"{self.model.__name__}: two indexes are named {name!r}")
        if declaration.kind == "local":
            if self.partition_key is None or self.sort_key is None:
                raise TypeError(f"{self.model.__name__}: local index {name!r} needs a table with a sort key")
            partition = self.partition_key
        else:
            partition = self._resolve_field(declaration.partition_key, f"index {name!r} partition key")
        sort = None
        if declaration.sort_key is not None:
            sort = self._resolve_field(declaration.sort_key, f"index {name!r} sort key")
        self.indexes[name] = IndexSpec(
            name=name,
            kind=declaration.kind,
            partition_key=partition,
            sort_key=sort,
            projection=declaration.projection,
            throughput=declaration.throughput,
        )

    # ---- values ---------------------------------------------------------------------------

    def values(self, instance: Any) -> dict[str, Any]:
        """Python values of every field."""
        return {name: self.adapter.get(instance, name) for name in self.fields}

    def state(self, instance: Any) -> ModelState | None:
        """The persistence state of an instance."""
        return self.adapter.state(instance)

    def render_derived(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Values of derived (templated) fields for ``values``."""
        derived: dict[str, Any] = {}
        for spec in self.fields.values():
            if spec.template is not None:
                derived[spec.name] = spec.template.render(values, self.codecs)
        return derived

    def encode(self, instance: Any, *, include_type: bool = True) -> dict[str, Any]:
        """The stored form of an instance (``None`` values and empty sets are left out).

        Raises:
            ValueError: A key component is missing.
        """
        return self.encode_values(self.values(instance), include_type=include_type)

    def encode_values(self, values: Mapping[str, Any], *, include_type: bool = True) -> dict[str, Any]:
        """The stored form of field values (Python values by field name).

        Derived fields are rendered from the others.

        Raises:
            ValueError: A key component is missing.
        """
        values = {**values, **self.render_derived(values)}
        item: dict[str, Any] = {}
        for spec in self.fields.values():
            value = values.get(spec.name)
            if value is None:
                if spec.role is not None:
                    missing = [
                        name for name in (spec.template.fields if spec.template else ()) if values.get(name) is None
                    ]
                    detail = f" ({', '.join(missing)} not set)" if missing else ""
                    raise ValueError(f"{self.model.__name__}.{spec.name} (a key attribute) has no value{detail}")
                continue
            encoded = spec.codec.encode(value)
            if encoded is not None:
                item[spec.attribute] = encoded
        if include_type and self.type_attribute is not None and self.entity is not None:
            item[self.type_attribute] = self.entity
        return item

    def decode_values(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Python values by field name of the attributes present in a stored item."""
        values: dict[str, Any] = {}
        for attribute, raw in item.items():
            spec = self.attributes.get(attribute)
            if spec is not None:
                values[spec.name] = spec.codec.decode(raw)
        return values

    def decode(self, item: Mapping[str, Any]) -> Any:
        """An instance from a stored item.

        Raises:
            ItemDecodeError: The item does not fit the model.
        """
        values: dict[str, Any] = {}
        errors: list[FieldError] = []
        for attribute, raw in item.items():
            spec = self.attributes.get(attribute)
            if spec is None:
                continue
            try:
                values[spec.name] = spec.codec.decode(raw)
            except (ValueError, TypeError, ArithmeticError) as error:
                errors.append(FieldError(spec.name, f"cannot decode {raw!r:.60}: {error}"))
        for spec in self.fields.values():
            if spec.name in values:
                continue
            initial = spec.initial()
            if initial is not MISSING:
                values[spec.name] = initial
            elif spec.optional or spec.auto is not None or spec.template is not None:
                values[spec.name] = None
            elif spec.codec.dynamo_type in ("SS", "NS", "BS"):
                values[spec.name] = spec.codec.decode(None)
            else:
                errors.append(FieldError(spec.name, f"the stored item has no {spec.attribute!r} attribute"))
        if errors:
            key = self.key_values(item) if self.is_table else None
            error = ItemDecodeError(
                f"cannot decode {self.model.__name__} from the stored item {key or ''}: "
                + "; ".join(str(problem) for problem in errors),
                model=self.model,
                key=key,
            )
            error.errors = tuple(errors)
            raise error
        try:
            instance = self.adapter.load(values)
        except Exception as cause:
            if isinstance(cause, ItemDecodeError):
                raise
            raise ItemDecodeError(
                f"cannot decode {self.model.__name__}: {cause}",
                model=self.model,
                key=self.key_values(item) if self.is_table else None,
            ) from cause
        state = self.adapter.state(instance)
        if state is not None:
            state.stored = dict(item)
        return instance

    def validate(self, instance: Any) -> None:
        """Validates every field and the model validators.

        Raises:
            ValidationError: With ``.errors`` listing every problem.
        """
        errors: list[FieldError] = []
        for spec in self.fields.values():
            value = self.adapter.get(instance, spec.name)
            if spec.template is not None or spec.auto in ("created_at", "updated_at", "version"):
                continue
            if value is None and not spec.optional:
                errors.append(FieldError(spec.name, "is required"))
                continue
            try:
                spec.validate(value)
            except ValueError as error:
                errors.append(FieldError(spec.name, str(error), value))
        if not errors:
            for validator in self.validators:
                try:
                    validator(instance)
                except ValueError as error:
                    errors.append(FieldError("__model__", str(error)))
        if errors:
            raise validation_error(
                f"{self.model.__name__} is invalid: " + "; ".join(str(problem) for problem in errors), errors
            )

    # ---- keys -----------------------------------------------------------------------------

    def key_fields(self) -> list[FieldSpec]:
        """The primary key fields (partition first)."""
        return [spec for spec in (self.partition_key, self.sort_key) if spec is not None]

    def key_values(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """The primary key attributes of a stored item."""
        return {spec.attribute: item[spec.attribute] for spec in self.key_fields() if spec.attribute in item}

    def key_of(self, instance: Any) -> dict[str, Any]:
        """The stored primary key of an instance."""
        return self.key_values(self.encode(instance, include_type=False))

    def _key_value(self, spec: FieldSpec, kwargs: dict[str, Any], positional: list[Any], used: set[str]) -> Any:
        if spec.name in kwargs:
            used.add(spec.name)
            value = kwargs[spec.name]
        elif spec.attribute in kwargs and spec.attribute != spec.name:
            used.add(spec.attribute)
            value = kwargs[spec.attribute]
        elif spec.template is not None and all(name in kwargs for name in spec.template.fields):
            components = {name: self.fields[name].validate(kwargs[name]) for name in spec.template.fields}
            used.update(spec.template.fields)
            value = spec.template.render(components, self.codecs)
        elif positional:
            value = positional.pop(0)
        else:
            needs = f"{spec.name}=..."
            if spec.template is not None and spec.template.fields:
                needs += f" (or {', '.join(f'{name}=...' for name in spec.template.fields)})"
            raise ValueError(f"{self.model.__name__}: the key needs {needs}")
        try:
            value = spec.codec.validate(value)
        except ValueError as error:
            raise ValueError(f"{self.model.__name__}.{spec.name}: {error}") from None
        return spec.codec.encode(value)

    def key_from(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The stored primary key from user arguments.

        Positional arguments are the partition and sort key values; keyword arguments name key
        fields (``pk="USER#1"``) or the fields their templates use (``user_id="1"``).

        Raises:
            ValueError: Arguments are missing, unexpected or invalid.
        """
        if not self.is_table:
            raise TypeError(f"{self.model.__name__} is not stored in a table")
        positional = list(args)
        remaining = dict(kwargs)
        used: set[str] = set()
        key = {spec.attribute: self._key_value(spec, remaining, positional, used) for spec in self.key_fields()}
        unexpected = [name for name in remaining if name not in used]
        if positional or unexpected:
            extra = [repr(value) for value in positional] + [f"{name}=" for name in unexpected]
            raise ValueError(f"{self.model.__name__}: unexpected key arguments {', '.join(extra)}")
        return key

    def index_key(self, index: IndexSpec, kwargs: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
        """The stored partition key value of an index query from keyword arguments.

        Returns:
            The partition value and the arguments that were not used.
        """
        remaining = dict(kwargs)
        used: set[str] = set()
        value = self._key_value(index.partition_key, remaining, [], used)
        for name in used:
            remaining.pop(name, None)
        return value, remaining


def schema_of(model: type) -> ModelSchema:
    """The schema of a model class (built on first use, then cached).

    Raises:
        TypeError: ``model`` is not a dynamoxy model, or its declaration is invalid.
    """
    cached = model.__dict__.get("__dynamoxy_schema__")
    if cached is not None:
        return cached
    builder = getattr(model, "__dynamoxy_build_schema__", None)
    if builder is None:
        if any(base.__name__ == "BaseModel" and base.__module__.startswith("pydantic") for base in model.__mro__):
            from dynamoxy._pydantic import build_plain_schema

            schema = build_plain_schema(model)
        else:
            raise TypeError(f"{model!r} is not a dynamoxy model (subclass dynamoxy.Model or dynamoxy.PydanticModel)")
    else:
        schema = builder()
    type.__setattr__(model, "__dynamoxy_schema__", schema)
    return schema


def resolve_annotations(model: type) -> dict[str, Any]:
    """Type hints of a class with ``Annotated`` metadata (resolving string annotations).

    Raises:
        TypeError: An annotation refers to a name that is not defined.
    """
    try:
        return typing.get_type_hints(model, include_extras=True)
    except NameError as error:
        raise TypeError(
            f"cannot resolve the annotations of {model.__name__}: {error}; "
            "define referenced classes at module level before the model is used"
        ) from None


def make_field(
    model: type,
    name: str,
    annotation: Any,
    *,
    options: Mapping[str, Any],
    path: FieldPath[Any] | None,
    datetime_format: DateTimeFormat = "iso",
) -> FieldSpec:
    """Builds a :class:`FieldSpec` from an annotation and field options.

    ``datetime_format`` is the model's format of ``datetime`` values, used unless the field
    chooses a codec (``codec=``, ``UseCodec``, ``created_at(format=...)``).
    """
    where = f"{model.__name__}.{name}"
    template = options.get("template")
    auto = options.get("auto")
    codec = options.get("codec")
    if codec is None and auto in ("created_at", "updated_at"):
        codec = DateTimeCodec(datetime_format)
    elif codec is None:
        codec = resolve_codec(annotation, where=where, datetime_format=datetime_format)
    if auto == "ttl" and options.get("codec") is None:
        codec = _ttl_codec(codec, where)
    if auto in ("created_at", "updated_at") and not isinstance(codec, OptionalCodec):
        codec = OptionalCodec(codec)
    if auto == "version" and not isinstance(codec, OptionalCodec):
        codec = OptionalCodec(codec)
    if template is not None and not isinstance(codec, OptionalCodec):
        codec = OptionalCodec(codec)
    return FieldSpec(
        name=name,
        attribute=options.get("attribute_name") or name,
        codec=codec,
        annotation=annotation,
        default=options.get("default", MISSING),
        default_factory=options.get("default_factory"),
        role=options.get("role"),
        template=KeyTemplate(template) if isinstance(template, str) else template,
        unique=bool(options.get("unique", False)),
        auto=auto,
        constraints=Constraints(
            min_length=options.get("min_length"),
            max_length=options.get("max_length"),
            pattern=options.get("pattern"),
            ge=options.get("ge"),
            gt=options.get("gt"),
            le=options.get("le"),
            lt=options.get("lt"),
        ),
        validators=tuple(options.get("validators") or ()),
        immutable=bool(options.get("immutable", False)),
        repr=bool(options.get("repr", True)),
        description=options.get("description"),
        path=path,
    )


def _ttl_codec(codec: Codec[Any], where: str) -> Codec[Any]:
    """TTL attributes are epoch seconds: ``datetime`` fields switch to the epoch format."""
    from dynamoxy.codecs import DateTimeCodec, IntCodec

    inner = codec.inner if isinstance(codec, OptionalCodec) else codec
    if isinstance(inner, DateTimeCodec):
        return OptionalCodec(DateTimeCodec("epoch"))
    if isinstance(inner, IntCodec):
        return OptionalCodec(inner)
    raise TypeError(f"{where}: a TTL field must be a datetime or an int (epoch seconds), not {codec.describe()}")


def collect_hooks(model: type) -> tuple[list[Callable[[Any], None]], dict[HookPoint, list[Callable[..., Any]]]]:
    """Validators and lifecycle hooks declared on a model class (and its bases)."""
    import inspect

    validators: list[Callable[[Any], None]] = []
    hooks: dict[HookPoint, list[Callable[..., Any]]] = {point: [] for point in HOOK_POINTS}
    # Every attribute name, base classes first; the most derived definition wins (like lookup).
    names = dict.fromkeys(name for klass in reversed(model.__mro__) for name in vars(klass))
    for name in names:
        value = inspect.getattr_static(model, name)
        function = value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
        for mark in getattr(function, "__dynamoxy_hooks__", None) or ():
            if mark == "validator":
                validators.append(function)
            else:
                hooks[mark].append(function)
    return validators, hooks
