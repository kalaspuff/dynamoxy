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
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, Self, overload, override

from pydantic import BaseModel, ConfigDict, PydanticUndefinedAnnotation
from pydantic import ValidationError as PydanticValidationError
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import PydanticUndefined, SchemaValidator

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


#: Whether pydantic is building a ``PydanticModel`` class in this context (see ``_ModelMetaclass``).
_BUILDING: ContextVar[bool] = ContextVar("dynamoxy_pydantic_building", default=False)


class _ModelMetaclass(ModelMetaclass):
    """Pydantic's metaclass, with the field paths of base classes hidden while it builds a class.

    Pydantic reads ``getattr(cls, name)`` for every field while it collects the fields of a new
    class, inherited ones included, and takes what it finds as the field's default (and warns that
    a redeclared field "shadows an attribute in parent"). The class attributes of fields are
    dynamoxy's paths, so a subclass of a ``PydanticModel`` would get the base's paths as defaults,
    and its required inherited fields would become optional. While a class is built, paths answer
    class access with ``AttributeError``, as the deleted class attributes of a plain pydantic
    model do, so pydantic copies the fields of the base, as it does for plain models.
    """

    def __new__(mcs, *args: Any, **kwargs: Any) -> type:
        token = _BUILDING.set(True)
        try:
            return super().__new__(mcs, *args, **kwargs)
        finally:
            _BUILDING.reset(token)


class _PydanticFieldPath[T](FieldPath[T]):
    """The class attribute of a ``PydanticModel`` field (hidden while pydantic builds a class)."""

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, instance: object, owner: type[Any]) -> T: ...
    @override
    def __get__(self, instance: object | None, owner: type[Any]) -> Self | T:
        if instance is None and _BUILDING.get():
            raise AttributeError(self.field_name)
        return super().__get__(instance, owner)


def _field_options(info: FieldInfo) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if info.default is not PydanticUndefined:
        options["default"] = info.default
    if info.default_factory is not None:
        options["default_factory"] = info.default_factory
        # pydantic 2.10 and later: a factory may take the validated data of the fields before it.
        takes_data = getattr(info, "default_factory_takes_validated_data", None)
        if takes_data is None:
            takes_data = getattr(info, "default_factory_takes_data", False)
        options["default_factory_takes_data"] = bool(takes_data)
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


def _resolve_annotations(model: type[BaseModel]) -> None:
    """Resolves annotations that name classes declared after the model (``shipping: Address | None``).

    Pydantic leaves them unresolved until the model is rebuilt, which it does when the first
    instance is created; the schema is built earlier (``create_tables``) and needs them.
    """
    if model.__pydantic_complete__:
        return
    try:
        # The module's namespace (and the one the class was declared in), not this function's.
        model.model_rebuild(raise_errors=True, _parent_namespace_depth=0)
    except PydanticUndefinedAnnotation as error:
        raise TypeError(
            f"{model.__name__}: the annotation {error.name!r} is not defined; declare it before the model "
            f"is used, or call {model.__name__}.model_rebuild() where it is defined"
        ) from None


class _FieldValidator:
    """Validates a value of one field given outside an instance (a key of a read, a value an update sets).

    Pydantic validates instances, and has no public API that validates the value of one field.
    This runs the field's schema from the model's core schema: its type, ``Field`` constraints,
    ``Annotated`` validators and ``@field_validator``s, under the model's config. Validators that
    take a ``ValidationInfo`` are left out, since they may read other fields, which a single value
    lacks (``info.data`` is ``None``), and so are model validators. Built on first use.
    """

    __slots__ = ("_validate", "model", "name")

    def __init__(self, model: type[BaseModel], name: str) -> None:
        self.model = model
        self.name = name
        self._validate: Any = None

    def __call__(self, value: Any) -> Any:
        validate = self._validate
        if validate is None:
            try:
                validate = _field_validation(self.model, self.name)
            except (KeyError, TypeError, AttributeError, StopIteration):  # a core schema of another layout
                validate = None
            validate = self._validate = validate or _unchanged
        try:
            return validate(value)
        except PydanticValidationError as error:
            # Without the input, which pydantic's messages quote (values may be personal data).
            raise ValueError("; ".join(problem["msg"] for problem in error.errors(include_url=False))) from None


def _unchanged(value: Any) -> Any:
    return value


class _InfoValidatorError(Exception):
    """A field's schema is a plain validator that takes a ``ValidationInfo`` (it cannot be left out)."""


def _field_validation(model: type[BaseModel], name: str) -> Any:
    """``validate_python`` of a validator of the field's core schema, or ``None`` if there is none."""
    definitions: list[Any] = []
    node: Any = model.__pydantic_core_schema__
    while node.get("type") != "model":
        kind = node.get("type")
        if kind == "definitions":
            definitions.extend(node["definitions"])
            node = node["schema"]
        elif kind == "definition-ref":
            node = next((entry for entry in definitions if entry.get("ref") == node["schema_ref"]), None)
            if node is None:
                return None
        elif kind in ("function-before", "function-after", "function-wrap"):  # model validators
            node = node["schema"]
        else:
            return None
    fields = node["schema"]
    if fields.get("type") != "model-fields" or name not in fields["fields"]:
        return None
    try:
        schema = _without_info_validators(fields["fields"][name]["schema"])
    except _InfoValidatorError:
        return None
    if definitions:
        schema = {"type": "definitions", "schema": schema, "definitions": definitions}
    return SchemaValidator(schema, node.get("config")).validate_python


def _without_info_validators(node: Any) -> Any:
    """A copy of a core schema without the validators that take a ``ValidationInfo``.

    Nested models keep theirs: they validate their own fields, with ``info.data``.

    Raises:
        _InfoValidatorError: The schema is a plain validator that takes one.
    """
    if isinstance(node, list):
        return [_without_info_validators(entry) for entry in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("type")
    if kind == "model":
        return node
    function = node.get("function")
    takes_info = isinstance(function, dict) and function.get("type") == "with-info"
    if takes_info and kind in ("function-before", "function-after", "function-wrap"):
        return _without_info_validators(node["schema"])
    if takes_info and kind == "function-plain":
        raise _InfoValidatorError
    return {key: _without_info_validators(value) for key, value in node.items()}


def _build(model: type[BaseModel], options: ModelOptions) -> ModelSchema:
    _resolve_annotations(model)
    specs = []
    for name, info in model.model_fields.items():
        field_options = _field_options(info)
        field_options["outside_validator"] = _FieldValidator(model, name)
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


class PydanticModel(BaseModel, _Persistence, metaclass=_ModelMetaclass):
    """A pydantic model stored in DynamoDB. Class keywords are the same as :class:`dynamoxy.Model`'s.

    Indexes are class attributes (``by_email = dynamoxy.GlobalIndex(partition_key="email")``).
    Instances compare like any pydantic model: an instance read from DynamoDB equals a new one
    with the same values. Like native models, ``Model.create(client, ...)``,
    ``Model.get(client, ...)`` and ``Model.query(client, ...)`` create and read items, and
    instances that a repository read or wrote can persist themselves: ``save()``,
    ``delete()``, ``update(...)``, ``add(...)``, ``increment(...)``, ``decrement(...)`` and
    ``refresh()`` (paths as ``dynamoxy.attr(Model.field)`` for static typing).
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
        write_ids: bool | None = None,
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
            write_ids=inherited.write_ids if write_ids is None else write_ids,
        )
        super().__init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        # Pydantic has collected the fields: paths answer class access again, also in subclasses' hooks.
        _BUILDING.set(False)
        super().__pydantic_init_subclass__(**kwargs)
        # A field's path replaces the class attribute of its name: refuse the names of the model API
        # (as native models do), where pydantic only warns and the method would be gone.
        taken = sorted(_MODEL_API.intersection(cls.model_fields))
        if taken:
            name = taken[0]
            raise TypeError(
                f"{cls.__name__}.{name}: the name is part of the model API ({name}); name the field differently "
                f"and keep the stored name with Annotated[..., dynamoxy.AttributeName({name!r})]"
            )
        for name, info in cls.model_fields.items():
            path = _PydanticFieldPath(
                **{key: value for key, value in _field_options(info).items() if key == "attribute_name"}
            )
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
        """The stored primary key of this instance (see :meth:`dynamoxy.Model.primary_key`)."""
        schema = schema_of(type(self))
        return schema.loaded_key(self) or schema.key_of(self)

    @classmethod
    def key(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """A stored primary key from values (see :meth:`dynamoxy.Model.key`)."""
        return schema_of(cls).key_from(args, kwargs)

    @property
    def is_loaded(self) -> bool:
        """Whether this instance was read from or written to DynamoDB."""
        state = self.__dict__.get(_STATE)
        return state is not None and state.loaded


#: Names of the model API (methods and properties of ``PydanticModel``), which fields cannot take.
#: Pydantic's own names (``model_*``) are pydantic's to protect.
_MODEL_API = frozenset(
    name for klass in (PydanticModel, _Persistence) for name in vars(klass) if not name.startswith(("_", "model_"))
)
