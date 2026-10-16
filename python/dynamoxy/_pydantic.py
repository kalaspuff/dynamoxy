"""Pydantic models as dynamoxy models (``pip install dynamoxy[pydantic]``).

::

    from typing import Annotated

    import dynamoxy


    class User(dynamoxy.PydanticModel, table="users"):
        user_id: Annotated[str, dynamoxy.PartitionKey()]
        email: Annotated[str, dynamoxy.Unique()]
        name: str
        age: int | None = None

        by_email = dynamoxy.GlobalIndex(partition_key="email")

Pydantic validates the data; dynamoxy adds the table mapping. Field options are ``Annotated``
markers: :class:`dynamoxy.PartitionKey`, :class:`dynamoxy.SortKey`, :class:`dynamoxy.Unique`,
:class:`dynamoxy.AttributeName`, :class:`dynamoxy.Template`, :class:`dynamoxy.CreatedAt`,
:class:`dynamoxy.UpdatedAt`, :class:`dynamoxy.Version`, :class:`dynamoxy.TTL` and
:class:`dynamoxy.UseCodec`. Class attributes are expression paths at runtime (``User.age >= 18``);
wrap them in :func:`dynamoxy.attr` for static typing (``dynamoxy.attr(User.age) >= 18``).

Plain ``pydantic.BaseModel`` classes can be nested in any model; they are stored as maps.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self, override

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticUndefined

from dynamoxy._utils import MISSING
from dynamoxy.attributes import (
    TTL,
    AttributeName,
    CreatedAt,
    FieldPath,
    PartitionKey,
    SortKey,
    Template,
    Unique,
    UpdatedAt,
    Version,
)
from dynamoxy.codecs import DateTimeCodec, UseCodec
from dynamoxy.models import _STATE, _Persistence
from dynamoxy.schema import (
    ModelOptions,
    ModelSchema,
    ModelState,
    _IndexDeclaration,
    collect_hooks,
    make_field,
    register_entity,
    schema_of,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from dynamoxy.codecs import DateTimeFormat

__all__ = ["PydanticModel", "build_plain_schema"]


class PydanticAdapter:
    """Reads and creates instances of pydantic models."""

    #: Field values live in the instance ``__dict__`` (``get`` is ``__dict__.get``).
    dict_backed = True

    def __init__(self, model: type[BaseModel]) -> None:
        self.model = model

    def get(self, instance: Any, name: str) -> Any:
        return instance.__dict__.get(name)

    def set(self, instance: Any, name: str, value: Any) -> None:
        instance.__dict__[name] = value
        instance.__pydantic_fields_set__.add(name)

    def create(self, values: dict[str, Any]) -> Any:
        try:
            return self.model.model_validate(values)
        except PydanticValidationError as error:
            raise _without_inputs(error) from None

    def load(self, values: dict[str, Any]) -> Any:
        try:
            return self.model.model_validate(values)
        except PydanticValidationError as error:
            raise _without_inputs(error) from None

    def state(self, instance: Any) -> ModelState | None:
        if not isinstance(instance, PydanticModel):
            return None
        # In the instance __dict__, as for native models, and not as a pydantic private attribute:
        # pydantic compares private attributes in ==, but ignores non-field __dict__ entries there,
        # in serialization and in repr, and keeps them when it validates an assignment.
        state = instance.__dict__.get(_STATE)
        if state is None:
            state = ModelState()
            instance.__dict__[_STATE] = state
        return state


def _without_inputs(error: PydanticValidationError) -> ValueError:
    """``error`` as a ``ValueError`` whose message leaves out the input values.

    Pydantic quotes the input in its messages; dynamoxy's messages never include values, which
    may be personal data and end up in logs.
    """
    problems = "; ".join(
        f"{'.'.join(str(part) for part in problem['loc']) or '__model__'}: {problem['msg']}"
        for problem in error.errors(include_url=False)
    )
    return ValueError(f"{error.title}: {problems}")


def _field_options(info: FieldInfo) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if info.default is not PydanticUndefined:
        options["default"] = info.default
    if info.default_factory is not None:
        options["default_factory"] = info.default_factory
    if info.description:
        options["description"] = info.description
    for marker in info.metadata:
        if isinstance(marker, PartitionKey):
            options["role"] = "partition"
            if marker.template is not None:
                options["template"] = marker.template
        elif isinstance(marker, SortKey):
            options["role"] = "sort"
            if marker.template is not None:
                options["template"] = marker.template
        elif isinstance(marker, Unique):
            options["unique"] = True
        elif isinstance(marker, AttributeName):
            options["attribute_name"] = marker.name
        elif isinstance(marker, Template):
            options["template"] = marker.template
        elif isinstance(marker, CreatedAt):
            options["auto"] = "created_at"
            if marker.format is not None:  # otherwise the model's datetime_format
                options["codec"] = DateTimeCodec(marker.format)
        elif isinstance(marker, UpdatedAt):
            options["auto"] = "updated_at"
            if marker.format is not None:
                options["codec"] = DateTimeCodec(marker.format)
        elif isinstance(marker, Version):
            options["auto"] = "version"
        elif isinstance(marker, TTL):
            options["auto"] = "ttl"
        elif isinstance(marker, UseCodec):
            options["codec"] = marker.codec
    if options.get("role") is not None:
        options["immutable"] = True
    return options


def _build(model: type[BaseModel], options: ModelOptions) -> ModelSchema:
    specs = []
    for name, info in model.model_fields.items():
        field_options = _field_options(info)
        path = model.__dict__.get(name)
        specs.append(
            make_field(
                model,
                name,
                info.annotation,
                options=field_options,
                path=path if isinstance(path, FieldPath) else None,
                datetime_format=options.datetime_format,
            )
        )
    validators, hooks = collect_hooks(model)
    declarations = []
    for klass in reversed(model.__mro__):
        declarations.extend(value for value in vars(klass).values() if isinstance(value, _IndexDeclaration))
    unique_declarations = {declaration.name: declaration for declaration in declarations}
    return ModelSchema(
        model,
        options=options,
        fields=specs,
        index_declarations=unique_declarations.values(),
        adapter=PydanticAdapter(model),
        validators=validators,
        hooks=hooks,
    )


def build_plain_schema(model: type[BaseModel]) -> ModelSchema:
    """The schema of a plain pydantic model used as a nested document."""
    return _build(model, ModelOptions(abstract=True))


class PydanticModel(BaseModel, _Persistence):
    """A pydantic model stored in DynamoDB. Class keywords are the same as :class:`dynamoxy.Model`'s.

    Indexes are class attributes (``by_email = dynamoxy.GlobalIndex(partition_key="email")``).
    Instances compare like any pydantic model: an instance read from DynamoDB equals a new one
    with the same values. Like native models, instances that a repository read or wrote can
    persist themselves: ``save()``, ``delete()``, ``update(...)``, ``add(...)``,
    ``increment(...)``, ``decrement(...)`` and ``refresh()`` (paths as ``dynamoxy.attr(Model.field)``
    for static typing).
    """

    model_config = ConfigDict(ignored_types=(_IndexDeclaration,), validate_assignment=True)

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

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name, info in cls.model_fields.items():
            path = FieldPath(**{key: value for key, value in _field_options(info).items() if key == "attribute_name"})
            type.__setattr__(cls, name, path)
            path.__set_name__(cls, name)
        options = cls.__dynamoxy_options__
        if not options.abstract:
            register_entity(options.table, options.entity, cls)

    @classmethod
    def __dynamoxy_build_schema__(cls) -> ModelSchema:
        return _build(cls, cls.__dynamoxy_options__)

    @override
    def __copy__(self) -> Self:
        # model_copy() and copy.copy(): a persistence state of its own, as for native models.
        clone = super().__copy__()
        state = self.__dict__.get(_STATE)
        if state is not None:
            clone.__dict__[_STATE] = copy.copy(state)
        return clone

    def to_item(self) -> dict[str, Any]:
        """The stored form (attribute names, encoded values, derived keys, type attribute)."""
        return schema_of(type(self)).encode(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Self:
        """An instance from a stored item.

        Raises:
            ItemDecodeError: The item does not fit the model.
        """
        return schema_of(cls).decode(item)

    def primary_key(self) -> dict[str, Any]:
        """The stored primary key of this instance."""
        return schema_of(type(self)).key_of(self)

    @classmethod
    def key(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """A stored primary key from values (see :meth:`dynamoxy.Model.key`)."""
        return schema_of(cls).key_from(args, kwargs)

    @property
    def is_loaded(self) -> bool:
        """Whether this instance was read from or written to DynamoDB."""
        state = self.__dict__.get(_STATE)
        return state is not None and state.loaded
