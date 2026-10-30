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

Models validate on construction and on assignment. Persistence goes through a
:class:`dynamoxy.Repository` (``client.model(User)``); model classes hold no client, so
several clients can use the same models. An instance that a repository read or wrote remembers
that repository and can persist itself through it::

    user = await users.get_or_raise("u1")
    user.name = "Ada"
    await user.save()  # users.save(user)
    await user.increment(User.visits)  # users.update(user, User.visits.increment())
    await user.delete()  # users.delete(user)
"""

from __future__ import annotations

import copy as _copy
import dataclasses
from collections.abc import Hashable
from collections.abc import Set as AbstractSet
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Self, dataclass_transform, overload

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
from dynamoxy.errors import FieldError, ValidationError, validation_error
from dynamoxy.expressions import Arithmetic, IfNotExists, ListAppend, Path, UpdateAction, Value
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
    from datetime import timedelta

    from dynamoxy.codecs import Codec, DateTimeFormat
    from dynamoxy.expressions import Condition
    from dynamoxy.repository import Repository

__all__ = [
    "Document",
    "Model",
    "after_delete",
    "after_load",
    "after_save",
    "after_update",
    "before_delete",
    "before_save",
    "before_update",
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

    It runs for put, create, save (also ``instance.save()``), batch puts and transactions, may
    change the instance, and aborts the write by raising. Updates run :func:`before_update`
    hooks instead.
    """
    return _mark(function, "before_save")


def after_save[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs after the instance was written (see :func:`before_save`)."""
    return _mark(function, "after_save")


def before_update[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs before an update of the instance.

    It runs for ``update()`` of an instance (in repositories and transactions) and for the
    instance's own ``update()``, ``add()``, ``increment()`` and ``decrement()``, and aborts the
    update by raising. The update writes its actions only: changes the hook makes to the
    instance are not stored. Updates by key have no instance and run no hooks.
    """
    return _mark(function, "before_update")


def after_update[F: Callable[..., Any]](function: F) -> F:
    """Marks a method (sync or async) that runs after an update, on the refreshed instance."""
    return _mark(function, "after_update")


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

    def load_stored(self, values: dict[str, Any], stored: dict[str, Any], repository: Repository[Any] | None) -> Any:
        """``load``, recording ``stored`` as the instance's stored item read by ``repository``."""
        instance = object.__new__(self.model)
        values[_STATE] = ModelState(stored, repository)
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
            if name in _reserved_names(cls):
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
        """A new, unsaved instance with the same values and some fields changed (validated).

        Automatic fields (``created_at``, ``updated_at``, ``version``) start empty, as in a
        constructed instance, so saving the copy behaves like saving a new instance: a versioned
        copy is written with version 1, and its ``save()`` fails if the key exists. Pass them in
        ``changes`` to keep them (``item.copy(created_at=item.created_at)``). ``copy.copy()`` and
        ``copy.deepcopy()`` make exact duplicates instead.

        Raises:
            TypeError: ``changes`` names something that is not a field.
            ValidationError: A changed value is invalid.
        """
        schema = schema_of(type(self))
        unknown = [name for name in changes if name not in schema.fields]
        if unknown:
            raise TypeError(f"{type(self).__name__}.copy() got unexpected field(s) {', '.join(map(repr, unknown))}")
        values = {name: value for name, value in self.__dict__.items() if name != _STATE}
        clone = object.__new__(type(self))
        clone.__dict__.update(_copy.deepcopy(values))
        for name in schema._automatic:
            clone.__dict__[name] = None
        for name, value in changes.items():
            setattr(clone, name, value)
        return clone

    def __copy__(self) -> Self:
        # A shallow duplicate with a persistence state of its own: writing one of them must not
        # change what the other knows about its stored item.
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        state = self.__dict__.get(_STATE)
        if state is not None:
            clone.__dict__[_STATE] = _copy.copy(state)
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


#: Paths of sets and lists of ``E`` (for the overloads of ``add``).
type _SetPath[E] = Path[set[E]] | Path[frozenset[E]] | Path[set[E] | None] | Path[frozenset[E] | None]
type _ListPath[E] = Path[list[E]] | Path[tuple[E, ...]] | Path[list[E] | None] | Path[tuple[E, ...] | None]
type _Condition = Condition | bool | None


class _Persistence:
    """Persistence methods of model instances (:class:`Model` and ``PydanticModel``).

    An instance remembers the repository that last read or wrote it, next to the stored copy.
    These methods call that repository's public methods (``save``, ``update``, ``delete``,
    ``refresh``), so they behave exactly like them (validation, hooks, versions, unique
    guards, automatic and derived fields) and go through the overrides of repository
    subclasses. Instances no repository read or wrote (new ones, ``copy()``, unpickled ones)
    are not bound: their methods raise ``RuntimeError`` before any I/O.
    """

    __slots__ = ()

    async def save(self, *, condition: Condition | bool | None = None) -> Self:
        """Writes the instance: creates the item, or replaces the stored one (``repository.save``).

        For models with a ``version()`` field, the write only succeeds if the stored item is
        still the one this instance was loaded with.

        Example::

            customer = await customers.get_or_raise("c1")
            customer.name = "Ada"
            await customer.save()

        Raises:
            VersionConflictError: The stored version differs (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            UniqueConstraintError: A unique value is taken.
            ValidationError: The instance is invalid.
            RuntimeError: The instance is not bound to a repository.
        """
        return await _repository_of(self, "save", "save").save(self, condition=condition)

    async def delete(self, *, condition: Condition | bool | None = None) -> Self | None:
        """Deletes the item (``repository.delete``).

        The instance is no longer loaded afterwards, but stays bound to the repository.

        Returns:
            The item as it was stored (a new instance), or ``None`` when nothing was stored.

        Raises:
            VersionConflictError: The item changed since it was loaded (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            RuntimeError: The instance is not bound to a repository.
        """
        return await _repository_of(self, "delete", "delete").delete(self, condition=condition)

    async def update(
        self, *actions: UpdateAction, condition: Condition | bool | None = None, upsert: bool = False
    ) -> Self:
        """Applies update actions atomically and refreshes the instance (``repository.update``).

        Example::

            await order.update(Order.status.set("paid"), Order.events.append_or_create(["paid"]))

        Raises:
            ItemNotFoundError: The item no longer exists (unless ``upsert``).
            VersionConflictError: The item changed since it was loaded (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            ValueError: An action targets a key, immutable or derived attribute (see ``update``).
            RuntimeError: The instance is not bound to a repository.
        """
        repository = _repository_of(self, "update", "update")
        return await repository.update(self, *actions, condition=condition, upsert=upsert)

    # Overloads let type checkers check the value against the attribute: elements for sets and
    # lists, numbers of the attribute's type otherwise. Nested paths (``Path[Any]``) accept any.

    @overload
    async def add[E](self, path: _SetPath[E], value: E | AbstractSet[E], *, condition: _Condition = None) -> Self: ...
    @overload
    async def add[E](self, path: _ListPath[E], value: E, *, condition: _Condition = None) -> Self: ...
    @overload
    async def add(self, path: Path[int] | Path[int | None], value: int, *, condition: _Condition = None) -> Self: ...
    @overload
    async def add(
        self, path: Path[float] | Path[float | None], value: float, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def add(
        self, path: Path[Decimal] | Path[Decimal | None], value: Decimal | int, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def add(
        self, path: Path[timedelta] | Path[timedelta | None], value: timedelta, *, condition: _Condition = None
    ) -> Self: ...
    async def add(self, path: Path[Any], value: Any, *, condition: Condition | bool | None = None) -> Self:
        """Adds ``value`` atomically: to a number, as an element of a set, or at the end of a list.

        ``path`` is a field (``Player.score``) or a nested path (``Player.stats["wins"]``). Sets
        take one element or a set of elements; lists take one element and are created when
        missing; missing numbers count as 0. The value is validated with the field's type
        before any I/O. The instance is refreshed with the stored result.

        Example::

            await player.add(Player.badges, "first-win")
            await player.add(Player.history, "won")
            await player.add(Player.coins, Decimal("2.5"))

        Raises:
            TypeError: ``path`` holds neither numbers, sets nor lists, or is not a field of this model.
            ValueError: ``value`` is an empty set.
            ValidationError: ``value`` does not fit the attribute.
            VersionConflictError: The item changed since it was loaded (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            RuntimeError: The instance is not bound to a repository.
        """
        repository = _repository_of(self, "add", "update")
        return await repository.update(self, _add_action(self, path, value), condition=condition)

    @overload
    async def increment(
        self, path: Path[int] | Path[int | None], amount: int = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def increment(
        self, path: Path[float] | Path[float | None], amount: float = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def increment(
        self, path: Path[Decimal] | Path[Decimal | None], amount: Decimal | int = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def increment(
        self, path: Path[timedelta] | Path[timedelta | None], amount: timedelta, *, condition: _Condition = None
    ) -> Self: ...
    async def increment(
        self, path: Path[Any], amount: float | Decimal | timedelta = 1, *, condition: Condition | bool | None = None
    ) -> Self:
        """Adds ``amount`` (default 1) to a number atomically; a missing number counts as 0.

        Works on fields (``Player.score``) and nested paths (``Player.stats["wins"]``). DynamoDB
        does the arithmetic, so constraints such as ``ge=0`` cannot be checked locally: guard
        the change with a condition (``condition=Player.score >= 1``) when they matter.

        Example::

            await page.increment(Page.views)
            await player.increment(Player.stats["wins"], 2)

        Raises:
            TypeError: ``path`` does not hold numbers, or is not a field of this model.
            ValidationError: ``amount`` does not fit the attribute (``0.5`` for an ``int``).
            VersionConflictError: The item changed since it was loaded (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            RuntimeError: The instance is not bound to a repository.
        """
        repository = _repository_of(self, "increment", "update")
        return await repository.update(self, _number_action(self, "increment", path, amount), condition=condition)

    @overload
    async def decrement(
        self, path: Path[int] | Path[int | None], amount: int = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def decrement(
        self, path: Path[float] | Path[float | None], amount: float = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def decrement(
        self, path: Path[Decimal] | Path[Decimal | None], amount: Decimal | int = 1, *, condition: _Condition = None
    ) -> Self: ...
    @overload
    async def decrement(
        self, path: Path[timedelta] | Path[timedelta | None], amount: timedelta, *, condition: _Condition = None
    ) -> Self: ...
    async def decrement(
        self, path: Path[Any], amount: float | Decimal | timedelta = 1, *, condition: Condition | bool | None = None
    ) -> Self:
        """Subtracts ``amount`` (default 1) from a number atomically (see :meth:`increment`).

        Example::

            await account.decrement(Account.credits, 10, condition=Account.credits >= 10)

        Raises:
            TypeError: ``path`` does not hold numbers, or is not a field of this model.
            ValidationError: ``amount`` does not fit the attribute.
            VersionConflictError: The item changed since it was loaded (versioned models).
            ConditionalCheckFailedError: ``condition`` did not hold.
            RuntimeError: The instance is not bound to a repository.
        """
        repository = _repository_of(self, "decrement", "update")
        return await repository.update(self, _number_action(self, "decrement", path, amount), condition=condition)

    async def refresh(self, *, consistent: bool = True) -> Self:
        """Reloads the instance in place from DynamoDB (``repository.refresh``).

        Raises:
            ItemNotFoundError: The item no longer exists.
            RuntimeError: The instance is not bound to a repository.
        """
        return await _repository_of(self, "refresh", "refresh").refresh(self, consistent=consistent)


def _repository_of(instance: Any, method: str, repository_method: str) -> Repository[Any]:
    """The repository an instance is bound to.

    Raises:
        RuntimeError: No repository read or wrote the instance.
    """
    state = instance.__dict__.get(_STATE)
    repository = state.repository if state is not None else None
    if repository is None:
        name = type(instance).__name__
        arguments = "item, ..." if repository_method == "update" else "item"
        raise RuntimeError(
            f"{name}.{method}(): this instance is not bound to a repository (no repository read or wrote it; "
            "new instances, copies and unpickled instances are not bound). Use a repository once, for example "
            f"`await client.model({name}).{repository_method}({arguments})`; that binds the instance"
        )
    return repository


def _target(instance: Any, path: Path[Any], method: str) -> tuple[str, Codec[Any] | None]:
    """A description of ``path`` for messages, and the codec of its values (``None`` when unknown).

    Raises:
        TypeError: ``path`` is not a path, or a field of another model.
    """
    model = type(instance)
    if not isinstance(path, Path):
        raise TypeError(
            f"{model.__name__}.{method}() takes an attribute path such as {model.__name__}.<field>, "
            f"got {type(path).__name__}"
        )
    if isinstance(path, FieldPath) and not issubclass(model, path.model):
        raise TypeError(f"{path.model.__name__}.{path.field_name} is not a field of {model.__name__}")
    root, *rest = path.elements
    spec = schema_of(model).attributes.get(str(root))
    codec: Codec[Any] | None = spec.codec if spec is not None else None
    for element in rest:
        if codec is None:
            break
        codec = codec.element() if isinstance(element, int) else codec.member(element)
    where = spec.name if spec is not None else str(root)
    where += "".join(f"[{element}]" if isinstance(element, int) else f".{element}" for element in rest)
    return f"{model.__name__}.{where}", codec


def _checked(where: str, codec: Codec[Any] | None, value: Any, hint: str = "") -> Any:
    """``value`` validated and encoded with ``codec`` (unchanged without one).

    Raises:
        ValidationError: ``value`` does not fit (the message starts with ``hint``).
    """
    if codec is None:
        return value
    try:
        return codec.encode(codec.validate(value))
    except ValueError as error:
        raise _invalid(where, f"{hint}{error}", value) from None


def _invalid(where: str, problem: str, value: Any) -> ValidationError:
    return validation_error(f"{where}: {problem}", [FieldError(where.partition(".")[2], problem, value)])


def _number(where: str, codec: Codec[Any] | None, amount: Any) -> float | Decimal:
    """The stored form of a number to add to ``where``.

    Raises:
        ValidationError: ``amount`` is not a number the attribute accepts.
    """
    encoded = _checked(where, codec, amount)
    if isinstance(encoded, bool) or not isinstance(encoded, (int, float, Decimal)):
        raise _invalid(where, f"expected a number, got {type(amount).__name__}", amount)
    return encoded


def _add_number(path: Path[Any], amount: float | Decimal) -> UpdateAction:
    if len(path.elements) == 1:
        return UpdateAction("ADD", path, Value(amount))
    # DynamoDB documents ADD for top-level attributes; SET with if_not_exists works at any depth.
    return UpdateAction("SET", path, Arithmetic(IfNotExists(path, Value(0)), "+", Value(amount)))


def _number_action(instance: Any, method: str, path: Path[Any], amount: Any) -> UpdateAction:
    """The action of ``increment()`` (or ``decrement()``) of ``path`` by ``amount``.

    Raises:
        TypeError: ``path`` does not hold numbers.
        ValidationError: ``amount`` does not fit the attribute.
    """
    where, codec = _target(instance, path, method)
    if codec is not None and codec.dynamo_type not in (None, "N"):
        raise TypeError(f"{where} holds {codec.describe()} values: {method}() needs a number attribute")
    encoded = _number(where, codec, amount)
    return _add_number(path, -encoded if method == "decrement" else encoded)


def _add_action(instance: Any, path: Path[Any], value: Any) -> UpdateAction:
    """The action of ``add(path, value)``: by the attribute's type, or the value's when it is unknown.

    Raises:
        TypeError: The attribute holds neither numbers, sets nor lists.
        ValueError: An empty set.
        ValidationError: ``value`` does not fit the attribute.
    """
    where, codec = _target(instance, path, "add")
    kind = codec.dynamo_type if codec is not None else None
    if kind is None:  # untyped (Any, or not a field): numbers and sets, as DynamoDB's ADD
        if isinstance(value, AbstractSet):
            kind = "SS"
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            kind = "N"
        else:
            raise TypeError(
                f"{where} has no known type, so add() takes a number or a set; append to a list with "
                f"update({type(instance).__name__}.<path>.append_or_create([...]))"
            )
    if kind == "N":
        return _add_number(path, _number(where, codec, value))
    if kind in ("SS", "NS", "BS"):
        if isinstance(value, AbstractSet):
            elements = set(value)
        elif isinstance(value, Hashable):
            elements = {value}
        else:
            message = f"add() takes one element or a set of elements, got {type(value).__name__}"
            raise _invalid(where, message, value)
        if not elements:
            raise ValueError(f"{where}: add() needs at least one element to add to a set")
        return UpdateAction("ADD", path, Value(_checked(where, codec, elements)))
    if kind == "L":
        element_codec = codec.element() if codec is not None else None
        element = _checked(where, element_codec, value, "add() appends one element: ")
        return UpdateAction("SET", path, ListAppend(IfNotExists(path, Value([])), Value([element])))
    assert codec is not None
    raise TypeError(f"{where} holds {codec.describe()} values: add() works on numbers, sets and lists")


class Model(Document, _Persistence):
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
        write_ids: Store the id of each write in ``_wid`` (the default), so that a write whose
            attempt may have been applied (a timeout) recognises it instead of raising
            ``WriteOutcomeUnknownError``. Inherited.

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


def _api_names(*classes: type) -> frozenset[str]:
    return frozenset(name for cls in classes for name in vars(cls) if not name.startswith("_"))


#: Names of the document and model APIs, which fields must not shadow.
_DOCUMENT_API = _api_names(Document)
_MODEL_API = _api_names(Document, Model, _Persistence)


def _reserved_names(cls: type) -> frozenset[str]:
    return _MODEL_API if issubclass(cls, Model) else _DOCUMENT_API
