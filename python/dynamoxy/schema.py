"""Model schemas: the one description of a model that every operation uses.

Native models (:class:`dynamoxy.Model`) and pydantic models (:class:`dynamoxy.PydanticModel`)
both produce a :class:`ModelSchema`: the fields with their codecs and DynamoDB attribute names,
the primary key (optionally built from :class:`KeyTemplate` strings for single-table design),
secondary indexes, unique attributes, versioning, timestamps, TTL, hooks and validators.

Schemas are built lazily, on first use, so models can refer to classes defined later.
"""

from __future__ import annotations

import copy
import enum
import functools
import operator
import re
import string
import typing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from uuid import UUID

from dynamoxy import _core
from dynamoxy._utils import MISSING, number_text, stored_identity
from dynamoxy.codecs import (
    _UNCHANGED,
    AnyCodec,
    Codec,
    DateTimeCodec,
    DecimalCodec,
    FloatCodec,
    IntCodec,
    OptionalCodec,
    _check_datetime_format,
    _decoder,
    _decodes_unchanged,
    _given_codec,
    _optional,
    resolve_codec,
)
from dynamoxy.errors import FieldError, ItemDecodeError, validation_error
from dynamoxy.expressions import Path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from dynamoxy.attributes import FieldPath
    from dynamoxy.client import Client
    from dynamoxy.codecs import DateTimeFormat
    from dynamoxy.repository import Repository

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
    "bound_repository",
    "schema_of",
]

type KeyRole = Literal["partition", "sort"]
type AutoField = Literal["created_at", "updated_at", "version", "ttl"]
type HookPoint = Literal[
    "before_save", "after_save", "before_update", "after_update", "before_delete", "after_delete", "after_load"
]

HOOK_POINTS: tuple[HookPoint, ...] = (
    "before_save",
    "after_save",
    "before_update",
    "after_update",
    "before_delete",
    "after_delete",
    "after_load",
)


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
        """The key for ``values`` (Python values by field name), or ``None`` if one is missing.

        The text is the same for a new instance and for the item read back: ``Decimal`` values
        are rendered as DynamoDB stores them (``1.5`` for ``Decimal("1.50")``), and datetimes
        formatted with a spec are formatted in UTC, as they are stored.
        """
        text = ""
        for literal, name, spec in self._parts:
            text += literal
            if name is None:
                continue
            value = values.get(name)
            if value is None:
                return None
            if spec:
                if isinstance(value, datetime) and value.tzinfo is not None:
                    value = value.astimezone(UTC)
                text += format(value, spec)
                continue
            codec = codecs.get(name)
            stored = codec.encode(value) if codec is not None else value
            if isinstance(stored, bytes):
                raise ValueError(f"key template {self.template!r}: {name} is binary and cannot be part of a text key")
            inner = _inner_codec(codec) if codec is not None else None
            if isinstance(stored, (float, Decimal)) and isinstance(inner, (DecimalCodec, AnyCodec)):
                # The number as the field reads it back: DynamoDB stores `Decimal("1.50")` as
                # `1.5`, and a union or `Any` field decodes the stored number by its own rules.
                stored = inner.decode(_core.value_from_wire({"N": number_text(stored)}))
                if isinstance(stored, Decimal):
                    text += number_text(stored)
                    continue
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
    # The arguments of include() while one of them is a field not yet bound to its name (a field
    # of the class body that declares the index): the model's schema resolves them.
    _pending: tuple[str | FieldPath[Any], ...] = field(default=(), compare=False, repr=False)

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
        """The keys plus ``attributes``.

        Fields may be given as class attributes, also those of the class body that declares the
        index (their names are resolved with the model's schema).
        """
        if not attributes:
            raise ValueError("Projection.include() needs at least one attribute")
        if any(not isinstance(attribute, str) and not attribute.field_name for attribute in attributes):
            return cls("INCLUDE", (), attributes)
        return cls("INCLUDE", tuple(_include_name(attribute) for attribute in attributes))

    def resolved(self) -> Projection:
        """This projection with every field given to :meth:`include` named.

        Raises:
            TypeError: A field is not declared on a model class.
        """
        if not self._pending:
            return self
        return Projection("INCLUDE", tuple(_include_name(attribute) for attribute in self._pending))


def _include_name(attribute: str | FieldPath[Any]) -> str:
    if isinstance(attribute, str):
        return attribute
    name = attribute.field_name
    if not name:
        raise TypeError(
            "Projection.include() was given a field that is not declared on a model class: "
            "pass the field name as a string"
        )
    return name


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


@functools.cache
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


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
        min_length, max_length = self.min_length, self.max_length
        if min_length is not None or max_length is not None:
            try:
                length = len(value)
            except TypeError:
                length = None
            if length is not None:
                if min_length is not None and length < min_length:
                    return f"must have at least {min_length} characters/elements, has {length}"
                if max_length is not None and length > max_length:
                    return f"must have at most {max_length} characters/elements, has {length}"
        pattern = self.pattern
        if pattern is not None and isinstance(value, str) and _regex(pattern).fullmatch(value) is None:
            return f"must match the pattern {pattern!r}"
        ge, gt, le, lt = self.ge, self.gt, self.le, self.lt
        if ge is None and gt is None and le is None and lt is None:
            return None
        for bound, check, words in (
            (ge, operator.ge, "greater than or equal to"),
            (gt, operator.gt, "greater than"),
            (le, operator.le, "less than or equal to"),
            (lt, operator.lt, "less than"),
        ):
            if bound is not None:
                try:
                    ok = check(value, bound)
                except TypeError:
                    ok = False
                if not ok:
                    return f"must be {words} {bound!r}"
        return None


# Classes of default values that never change, so that instances can share them (and MISSING).
_SHARED_DEFAULTS: frozenset[type] = frozenset(
    {type(None), type(MISSING), bool, int, float, str, bytes, Decimal, datetime, date, time, timedelta, UUID}
)


@dataclass(eq=False)
class FieldSpec:
    """One field of a model, resolved.

    Attributes:
        name: The Python name.
        attribute: The DynamoDB attribute name.
        codec: Converts values to and from their stored form.
        annotation: The resolved type annotation.
        default: The default value, or ``MISSING``: of new instances, and of stored items without
            the attribute, unless the field stores a value as no attribute (see :meth:`empty`).
        default_factory: Creates the default value, or ``None``.
        default_factory_takes_data: ``default_factory`` takes the values of the fields declared
            before it (a pydantic ``Field(default_factory=lambda data: ...)``).
        role: ``"partition"`` or ``"sort"`` for primary key fields.
        template: Builds the value from other fields (derived fields are not set by hand).
        unique: Enforce uniqueness across items (see the constraints guide).
        auto: ``"created_at"``, ``"updated_at"``, ``"version"`` or ``"ttl"``.
        constraints: Length, pattern and range constraints.
        validators: Callables ``value -> value`` that raise ``ValueError``.
        outside_validator: For models whose instances validate themselves (pydantic): a callable
            ``value -> value`` that validates a value given outside an instance (keys of reads,
            values of updates) as the model validates the field, raising ``ValueError``.
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
    default_factory: Callable[..., Any] | None = None
    default_factory_takes_data: bool = False
    role: KeyRole | None = None
    template: KeyTemplate | None = None
    unique: bool = False
    auto: AutoField | None = None
    constraints: Constraints = field(default_factory=Constraints)
    validators: tuple[Callable[[Any], Any], ...] = ()
    outside_validator: Callable[[Any], Any] | None = None
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

    def initial(self, data: Mapping[str, Any] | None = None) -> Any:
        """The default value: a new one per call for factories, and a copy of a mutable default.

        Instances never share a mutable default (``field(default=[])``, a document), as in
        pydantic: changing the value of one instance would change it for all of them. A factory
        that takes data gets ``data``, the values of the other fields by Python name.
        """
        if self.default_factory is not None:
            if self.default_factory_takes_data:
                return self.default_factory(dict(data or {}))
            return self.default_factory()
        default = self.default
        if default.__class__ in _SHARED_DEFAULTS or isinstance(default, enum.Enum):
            return default
        return copy.deepcopy(default)

    def empty(self) -> Any:
        """The value that this field stores as no attribute, or ``MISSING`` when there is none.

        ``None`` for fields that can be ``None``, and an empty set for set fields, since DynamoDB
        has no empty sets. A missing attribute reads back as this value, so that reads return
        what was written; fields without one read a missing attribute as their default (items
        written before the field existed). A set field that can be ``None`` stores ``None`` and
        an empty set alike, and reads both back as ``None``.
        """
        if self.optional or self.auto is not None or self.template is not None:
            return None
        if self.codec.dynamo_type in ("SS", "NS", "BS"):
            return self.codec.decode(None)
        return MISSING

    def validate(self, value: Any) -> Any:
        """Checks a value given outside an instance: type (codec), constraints and validators.

        Pydantic models validate their fields with pydantic's validators too
        (``outside_validator``).

        Raises:
            ValueError: The value is invalid (the message says why).
        """
        value = self.check(value)
        if value is not None and self.outside_validator is not None:
            value = self.outside_validator(value)
        return value

    def check(self, value: Any) -> Any:
        """Checks a value of an instance: type (codec), constraints and validators.

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
    """What dynamoxy knows about an instance's stored copy, and where it is stored.

    Each instance has its own state. Copies (``copy.copy``, ``copy.deepcopy``, pydantic's
    ``model_copy``) keep the repository; pickles leave it out (it holds the client), so an
    unpickled instance is loaded but not bound.

    Attributes:
        stored: The item as last read or written (stored form), or ``None`` for a new instance.
        repository: The repository that last read or wrote the instance (``None`` until one
            did). The instance's own ``save()``, ``delete()``, ``update()``, ... use it.
    """

    stored: dict[str, Any] | None = None
    repository: Repository[Any] | None = None

    @property
    def loaded(self) -> bool:
        """Whether the instance was read from or written to DynamoDB."""
        return self.stored is not None

    def __copy__(self) -> ModelState:
        return ModelState(self.stored, self.repository)

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelState:
        return ModelState(copy.deepcopy(self.stored, memo), self.repository)

    def __reduce__(self) -> tuple[type[ModelState], tuple[dict[str, Any] | None]]:
        return ModelState, (self.stored,)


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
    write_ids: bool = True

    def __post_init__(self) -> None:
        _check_datetime_format(self.datetime_format, "datetime_format")


#: The attribute holding the id of the write that stored an item (see ``ModelOptions.write_ids``
#: and ADR 0016): a retried write recognises its own earlier attempt by it.
WRITE_ID_ATTRIBUTE = "_wid"


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
        # Items of the table store the id of the write that stored them (documents are not items).
        self.write_ids = options.write_ids and self.is_table
        if self.write_ids and WRITE_ID_ATTRIBUTE in self.attributes:
            raise TypeError(
                f"{model.__name__}: a field is stored as {WRITE_ID_ATTRIBUTE!r}, the attribute of write ids; "
                "store it under another attribute_name=, or pass write_ids=False"
            )
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
            if spec.codec.dynamo_type in _COLLECTION_TYPES:
                # The guard key is the value as text, which has no canonical form for collections
                # (a set's order depends on the hash seed of the process).
                raise TypeError(
                    f"{model.__name__}.{spec.name}: only strings, numbers, binary values and booleans can be "
                    f"unique, not {spec.codec.describe()}; remove unique=True"
                )
        self.version_field = self._single(fields, "version")
        self.created_at_field = self._single(fields, "created_at")
        self.updated_at_field = self._single(fields, "updated_at")
        self.ttl_field = self._single(fields, "ttl")
        self.indexes: dict[str, IndexSpec] = {}
        for declaration in index_declarations:
            self._add_index(declaration)
        self.codecs = {spec.name: spec.codec for spec in fields}
        self._plan(fields)

    def _plan(self, fields: list[FieldSpec]) -> None:
        """Precomputes what every validation, encoding and decoding does (fields never change)."""
        automatic = ("created_at", "updated_at", "version")
        self._key_specs: tuple[FieldSpec, ...] = tuple(self.key_fields())
        self._key_attributes: tuple[str, ...] = tuple(spec.attribute for spec in self._key_specs)
        self._key_attribute_set: frozenset[str] = frozenset(self._key_attributes)
        # The fields a key template uses: part of the key, so updates cannot change them.
        self._key_template_fields: frozenset[str] = frozenset(
            name for spec in self._key_specs if spec.template is not None for name in spec.template.fields
        )
        # A hashable identity of a stored key or of the item that has it (both hold every key
        # attribute): the key values, in key order. Float keys compare as the numbers DynamoDB
        # returns (0.1 is stored and read back as Decimal("0.1")).
        attributes = self._key_attributes
        self._key_identity: Callable[[Mapping[str, Any]], Any] = (
            operator.itemgetter(*attributes) if attributes else lambda _: ()
        )
        if any(isinstance(_inner_codec(spec.codec), FloatCodec) for spec in self._key_specs):
            self._key_identity = lambda key: tuple(_float_key(key[attribute]) for attribute in attributes)
        self._immutable: tuple[FieldSpec, ...] = tuple(
            spec for spec in fields if spec.immutable and spec.role is None and spec.template is None
        )
        # The attributes the model writes itself (its fields and the type attribute).
        self._stored_names: frozenset[str] = frozenset(self.attributes) | (
            {self.type_attribute} if self.type_attribute is not None else set()
        )
        self._derived: tuple[FieldSpec, ...] = tuple(spec for spec in fields if spec.template is not None)
        # (name, check of non-None values, optional) of the fields `validate` checks.
        self._validated: tuple[tuple[str, Callable[[Any], Any], bool], ...] = tuple(
            (spec.name, _value_check(spec), spec.optional)
            for spec in fields
            if spec.template is None and spec.auto not in automatic
        )
        # name -> how `_checked_key` checks the key values callers give: like `validate` does
        # (constraints and validators), or with the codec alone for derived and automatic fields,
        # whose values writes compute.
        self._key_checks: dict[str, Callable[[Any], Any]] = {spec.name: spec.codec.validate for spec in fields}
        self._key_checks.update(
            (name, self.fields[name].validate if self.fields[name].outside_validator is not None else check)
            for name, check, _ in self._validated
        )
        # (name, attribute, encode, spec) in field order.
        self._encoders: tuple[tuple[str, str, Callable[[Any], Any], FieldSpec], ...] = tuple(
            (spec.name, spec.attribute, spec.codec.encode, spec) for spec in fields
        )
        # attribute -> (name, decode (None: values are stored as they are), type the decoder
        # returns unchanged, so values of exactly that type skip it)
        self._decoders: dict[str, tuple[str, Callable[[Any], Any] | None, type | None]] = {
            spec.attribute: (spec.name, *_decoding(spec.codec)) for spec in fields
        }
        self._load_stored: Callable[[dict[str, Any], dict[str, Any], Repository[Any] | None], Any] | None = getattr(
            self.adapter, "load_stored", None
        )
        # (name, settable in the constructor, spec, check of non-None values) in field order.
        self._init_plan: tuple[tuple[str, bool, FieldSpec, Callable[[Any], Any]], ...] = tuple(
            (spec.name, spec.settable_in_init, spec, _value_check(spec)) for spec in fields
        )
        self._timestamps = self.created_at_field is not None or self.updated_at_field is not None
        # Fields dynamoxy sets on writes (empty in new instances and copies).
        self._automatic: tuple[str, ...] = tuple(spec.name for spec in fields if spec.auto in automatic)
        self._settable: frozenset[str] = frozenset(spec.name for spec in fields if spec.settable_in_init)
        self._dict_backed: bool = bool(getattr(self.adapter, "dict_backed", False))
        self._paths: dict[str, Path[Any]] = {}

    def _path(self, attribute: str) -> Path[Any]:
        """The expression path of a top-level attribute (paths are immutable: one per attribute)."""
        path = self._paths.get(attribute)
        if path is None:
            path = self._paths[attribute] = Path(attribute)
        return path

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

    def _resolve_projection(self, projection: Projection, index: str) -> Projection:
        try:
            return projection.resolved()
        except TypeError as error:
            raise TypeError(f"{self.model.__name__}: index {index!r}: {error}") from None

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
            projection=self._resolve_projection(declaration.projection, name),
            throughput=declaration.throughput,
        )

    # ---- values ---------------------------------------------------------------------------

    def values(self, instance: Any) -> dict[str, Any]:
        """Python values of every field."""
        if self._dict_backed:
            stored = instance.__dict__
            return {name: stored.get(name) for name in self.fields}
        get = self.adapter.get
        return {name: get(instance, name) for name in self.fields}

    def state(self, instance: Any) -> ModelState | None:
        """The persistence state of an instance."""
        return self.adapter.state(instance)

    def render_derived(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Values of derived (templated) fields for ``values``."""
        codecs = self.codecs
        return {spec.name: spec.template.render(values, codecs) for spec in self._derived}  # type: ignore[union-attr]

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
        if self._derived:
            values = {**values, **self.render_derived(values)}
        return self._encode_rendered(values, include_type=include_type)

    def _encode_rendered(self, values: Mapping[str, Any], *, include_type: bool = True) -> dict[str, Any]:
        """:meth:`encode_values` for values that already hold their derived fields."""
        item: dict[str, Any] = {}
        for name, attribute, encode, spec in self._encoders:
            value = values.get(name)
            if value is None:
                if spec.role is not None:
                    self._missing_key(spec, values)
                continue
            encoded = encode(value)
            if encoded is not None:
                item[attribute] = encoded
        if include_type and self.type_attribute is not None and self.entity is not None:
            item[self.type_attribute] = self.entity
        return item

    def _missing_key(self, spec: FieldSpec, values: Mapping[str, Any]) -> typing.NoReturn:
        missing = [name for name in (spec.template.fields if spec.template else ()) if values.get(name) is None]
        detail = f" ({', '.join(missing)} not set)" if missing else ""
        raise ValueError(f"{self.model.__name__}.{spec.name} (a key attribute) has no value{detail}")

    def decode_values(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Python values by field name of the attributes present in a stored item."""
        values: dict[str, Any] = {}
        decoders = self._decoders
        for attribute, raw in item.items():
            entry = decoders.get(attribute)
            if entry is not None:
                name, decode, unchanged = entry
                values[name] = raw if decode is None or raw.__class__ is unchanged else decode(raw)
        return values

    def decode(self, item: Mapping[str, Any]) -> Any:
        """An instance from a stored item.

        A missing attribute reads as ``None`` or an empty set, which are stored as no attribute
        (see :meth:`FieldSpec.empty`), and as the default in fields that can hold neither.

        Raises:
            ItemDecodeError: The item does not fit the model.
        """
        return self._decode(item, dict(item))

    def _decode(
        self, item: Mapping[str, Any], stored: dict[str, Any], repository: Repository[Any] | None = None
    ) -> Any:
        """:meth:`decode`, recording ``stored`` (``item`` or a copy of it) as the stored item.

        ``repository`` is the repository that read the item (``None`` for :meth:`decode`).
        """
        values: dict[str, Any] = {}
        errors: list[FieldError] = []
        decoders = self._decoders
        for attribute, raw in item.items():
            entry = decoders.get(attribute)
            if entry is None:
                continue
            name, decode, unchanged = entry
            if decode is None or raw.__class__ is unchanged:
                values[name] = raw
                continue
            try:
                values[name] = decode(raw)
            except (ValueError, TypeError, ArithmeticError):
                # Not the error's text, which may quote the value (int("x"), Enum("x")): values may be
                # personal data, and messages end up in logs.
                expected = self.fields[name].codec.describe()
                errors.append(FieldError(name, f"cannot decode a stored {type(raw).__name__} as {expected}", raw))
        if len(values) < len(self._encoders):
            for name, _, _, spec in self._encoders:
                if name in values:
                    continue
                # None and empty sets are stored as no attribute, and read back as written; other
                # fields read their default (items written before the field existed).
                value = spec.empty()
                if value is MISSING:
                    if spec.default_factory_takes_data:
                        continue  # pydantic calls it with the validated data, in declaration order
                    value = spec.initial()
                    if value is MISSING:
                        errors.append(FieldError(name, f"the stored item has no {spec.attribute!r} attribute"))
                        continue
                values[name] = value
        if errors:
            key = self.key_values(item) if self.is_table else None
            error = ItemDecodeError(
                f"cannot decode {self.model.__name__} from a stored item{' (see .key)' if key else ''}: "
                + "; ".join(str(problem) for problem in errors),
                model=self.model,
                key=key,
            )
            error.errors = tuple(errors)
            raise error
        load_stored = self._load_stored
        if load_stored is not None:
            return load_stored(values, stored, repository)
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
            state.stored = stored
            state.repository = repository
        return instance

    def validate(self, instance: Any) -> None:
        """Validates every field and the model validators.

        Raises:
            ValidationError: With ``.errors`` listing every problem.
        """
        errors: list[FieldError] = []
        get = instance.__dict__.get if self._dict_backed else functools.partial(self.adapter.get, instance)
        for name, check, optional in self._validated:
            value = get(name)
            if value is None:
                if not optional:
                    errors.append(FieldError(name, "is required"))
                continue
            try:
                check(value)
            except ValueError as error:
                errors.append(FieldError(name, str(error), value))
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
        return {attribute: item[attribute] for attribute in self._key_attributes if attribute in item}

    def key_of(self, instance: Any) -> dict[str, Any]:
        """The stored primary key of an instance, as its values render it.

        Raises:
            ValueError: A key component is missing.
        """
        return self._key_from_values(self.values(instance))

    def loaded_key(self, instance: Any) -> dict[str, Any] | None:
        """The key of the item ``instance`` was loaded from, as stored, while its values still address it.

        ``None`` when the instance was not loaded, or a key field (or a field a key template
        uses) changed since. See :meth:`addresses`.

        Raises:
            ValueError: A key component is missing.
        """
        state = self.state(instance)
        if state is None or not state.loaded or state.stored is None:
            return None
        return self.addresses(state.stored, self.key_of(instance))

    def addresses(self, stored: Mapping[str, Any], key: Mapping[str, Any]) -> dict[str, Any] | None:
        """The key of the ``stored`` item when ``key`` (rendered from field values) addresses it, else ``None``.

        Another writer may have stored a key in a form the field's codec reads but does not
        write: an upper-case UUID, a date as ``20260102``. Its instances render the canonical
        form, which addresses no item; what decodes to the same values is the same key, and
        writes use it as stored.
        """
        stored_key = self.key_values(stored)
        if stored_identity(stored_key) == stored_identity(self.key_values(key)):
            return stored_key
        try:
            rendered = self._key_from_values(self.decode_values(stored))
        except (ValueError, TypeError, ArithmeticError):
            return None
        return stored_key if stored_identity(rendered) == stored_identity(self.key_values(key)) else None

    def _key_from_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        key: dict[str, Any] = {}
        for spec in self._key_specs:
            template = spec.template
            value = template.render(values, self.codecs) if template is not None else values.get(spec.name)
            if value is None:
                self._missing_key(spec, values)
            encoded = spec.codec.encode(value)
            if encoded is not None:
                key[spec.attribute] = encoded
        return key

    def _checked_key(self, spec: FieldSpec, value: Any) -> Any:
        """A key value from the caller (or a value a key template uses), checked as writes check it.

        Constraints and validators run too, so a validator that normalizes values (``str.lower``)
        finds the item that a write stored with the normalized value.

        Raises:
            ValueError: The value is ``None`` or invalid (the message names the field).
        """
        if value is None:
            raise ValueError(f"{self.model.__name__}.{spec.name}: must not be None")
        try:
            return self._key_checks[spec.name](value)
        except ValueError as error:
            raise ValueError(f"{self.model.__name__}.{spec.name}: {error}") from None

    def _key_value(self, spec: FieldSpec, kwargs: dict[str, Any], positional: list[Any], used: set[str]) -> Any:
        if spec.name in kwargs:
            used.add(spec.name)
            value = kwargs[spec.name]
        elif spec.attribute in kwargs and spec.attribute != spec.name:
            used.add(spec.attribute)
            value = kwargs[spec.attribute]
        elif spec.template is not None and all(name in kwargs for name in spec.template.fields):
            components = {name: self._checked_key(self.fields[name], kwargs[name]) for name in spec.template.fields}
            used.update(spec.template.fields)
            value = spec.template.render(components, self.codecs)
        elif positional:
            value = positional.pop(0)
        else:
            needs = f"{spec.name}=..."
            if spec.template is not None and spec.template.fields:
                needs += f" (or {', '.join(f'{name}=...' for name in spec.template.fields)})"
            raise ValueError(f"{self.model.__name__}: the key needs {needs}")
        return spec.codec.encode(self._checked_key(spec, value))

    def key_from(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The stored primary key from user arguments.

        Positional arguments are the partition and sort key values; keyword arguments name key
        fields (``pk="USER#1"``) or the fields their templates use (``user_id="1"``). Values are
        checked like the fields' values on writes (constraints and validators included), so a
        validator that normalizes (``str.lower``) gives the key that writes stored.

        Raises:
            ValueError: Arguments are missing, unexpected or invalid.
        """
        if not self.is_table:
            raise TypeError(f"{self.model.__name__} is not stored in a table")
        if not kwargs and len(args) == len(self._key_specs):
            # Only positional values (the common case): each key field takes the next one.
            checked = self._checked_key
            return {
                spec.attribute: spec.codec.encode(checked(spec, value))
                for spec, value in zip(self._key_specs, args, strict=True)
            }
        positional = list(args)
        remaining = dict(kwargs)
        used: set[str] = set()
        key = {spec.attribute: self._key_value(spec, remaining, positional, used) for spec in self.key_fields()}
        unexpected = [name for name in remaining if name not in used]
        if positional or unexpected:
            extra = [f"{len(positional)} more positional value(s)"] if positional else []
            extra += [f"{name}=" for name in unexpected]
            raise ValueError(f"{self.model.__name__}: unexpected key arguments {', '.join(extra)}")
        return key

    def index_key(self, index: IndexSpec, kwargs: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
        """The stored partition key value of an index query from keyword arguments.

        The value is checked like the field's values on writes, as in :meth:`key_from`.

        Returns:
            The partition value and the arguments that were not used.
        """
        remaining = dict(kwargs)
        used: set[str] = set()
        value = self._key_value(index.partition_key, remaining, [], used)
        for name in used:
            remaining.pop(name, None)
        return value, remaining


#: DynamoDB types of values that cannot be unique.
_COLLECTION_TYPES = frozenset({"SS", "NS", "BS", "L", "M"})


def _inner_codec(codec: Codec[Any]) -> Codec[Any]:
    return codec.inner if isinstance(codec, OptionalCodec) else codec


def _float_key(value: Any) -> Any:
    """A float key value as the number DynamoDB stores (equal and hashed like ``Decimal`` and ``int``)."""
    return Decimal(number_text(value)) if type(value) is float else value


def _value_check(spec: FieldSpec) -> Callable[[Any], Any]:
    """:meth:`FieldSpec.check` for values that are not ``None``.

    That is the codec's ``validate`` alone when the field has neither constraints nor validators.
    """
    constraints = spec.constraints
    bounded = (
        constraints.min_length,
        constraints.max_length,
        constraints.pattern,
        constraints.ge,
        constraints.gt,
        constraints.le,
        constraints.lt,
    )
    if spec.validators or any(bound is not None for bound in bounded):
        return spec.check
    return spec.codec.validate


def _decoding(codec: Codec[Any]) -> tuple[Callable[[Any], Any] | None, type | None]:
    """How the schema decodes a codec's values: ``(decode, unchanged type)``.

    ``decode`` is ``None`` for codecs that return stored values as they are; values of exactly
    the unchanged type are returned as they are by ``decode`` too (``str`` for ``StrCodec``), so
    the call is skipped for them. Only the built-in codecs are recognized (exact types: a
    subclass may decode differently).
    """
    inner = codec.inner if type(codec) is OptionalCodec else codec
    if _decodes_unchanged(inner):  # also when optional: None decodes to None
        return None, None
    return _decoder(codec), _UNCHANGED.get(type(inner))


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


def bound_repository(target: Any, model: type, client: Client) -> Repository[Any] | None:
    """The repository an instance of ``model`` is bound to, when it belongs to ``client``.

    Transactions and batch writers write instances through it, so an instance stays bound to
    the repository (possibly a subclass) that read it. ``None`` for keys and unbound instances.
    """
    if not isinstance(target, model):
        return None
    state = schema_of(model).state(target)
    repository = state.repository if state is not None else None
    if repository is None or repository.client is not client or repository.model_class is not model:
        return None
    return repository


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
    else:  # the field's own: codec=, a pydantic UseCodec marker, created_at(format=...)
        codec = _given_codec(codec, annotation)
    if auto == "ttl" and options.get("codec") is None:
        codec = _ttl_codec(codec, where)
    if auto == "version":
        _check_version_codec(codec, where)
    if auto in ("created_at", "updated_at", "version") or template is not None:
        codec = _optional(codec)
    return FieldSpec(
        name=name,
        attribute=options.get("attribute_name") or name,
        codec=codec,
        annotation=annotation,
        default=options.get("default", MISSING),
        default_factory=options.get("default_factory"),
        default_factory_takes_data=bool(options.get("default_factory_takes_data", False)),
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
        outside_validator=options.get("outside_validator"),
        immutable=bool(options.get("immutable", False)),
        repr=bool(options.get("repr", True)),
        description=options.get("description"),
        path=path,
    )


def _check_version_codec(codec: Codec[Any], where: str) -> None:
    """Version fields hold a random token (ADR 0022): text, stored as a string."""
    from dynamoxy.codecs import StrCodec

    inner = codec.inner if isinstance(codec, OptionalCodec) else codec
    if not isinstance(inner, StrCodec):
        raise TypeError(
            f"{where}: a version() field holds a random token, which changes on every write: declare it as "
            f"Attr[str] (Annotated[str | None, Version()] in pydantic models), not {codec.describe()}"
        )


def _ttl_codec(codec: Codec[Any], where: str) -> Codec[Any]:
    """TTL attributes are epoch seconds: ``datetime`` fields switch to the epoch format."""
    from dynamoxy.codecs import DateTimeCodec

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
