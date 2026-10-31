"""Repositories: typed persistence for one model class.

::

    users = client.model(User)  # or a subclass with domain queries
    await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
    user = await users.get("u1")
    await users.update(user, User.visits.add(1))
    async for user in users.query(index=User.by_email, email="ada@example.com"):
        ...

Subclass :class:`Repository` to collect the queries of a model in one place::

    class UserRepository(dynamoxy.Repository[User]):
        async def by_email(self, email: str) -> User | None:
            return await self.query(index=User.by_email, email=email).first()

Write semantics:

* ``create`` fails with :class:`ItemAlreadyExistsError` if the key exists.
* ``put`` replaces whatever is stored under the key.
* ``save`` is ``put`` with optimistic locking for models with a ``version()`` field: it fails
  with :class:`VersionConflictError` when the stored version is not the one the instance was
  loaded with (for new instances: when the key exists).
* ``update`` changes attributes in place (``SET``/``ADD``/``REMOVE``/``DELETE``) and returns
  the new state; the item must exist unless ``upsert=True``.
* ``delete`` removes the item and returns what was deleted.

Every write maintains ``created_at``/``updated_at``/``version`` fields, derived (templated)
attributes, and unique guard items, and runs the lifecycle hooks.

Instances remember the repository that last read or wrote them, so they can persist themselves
through it: ``await user.save()`` is ``await users.save(user)``, ``await user.increment(User.visits)``
is ``await users.update(user, User.visits.increment())``, and ``delete()``, ``update(...)``,
``add(...)``, ``decrement(...)`` and ``refresh()`` work the same way, overrides of repository
subclasses included.
"""

from __future__ import annotations

import functools
import os
import types
import typing
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from dynamoxy import _core
from dynamoxy._utils import MISSING, stored_identity
from dynamoxy.constraints import GUARD_PREFIX, guard_item, guard_key
from dynamoxy.errors import (
    ConditionalCheckFailedError,
    DynamoxyError,
    FieldError,
    ItemAlreadyExistsError,
    ItemDecodeError,
    ItemNotFoundError,
    TransactionCanceledError,
    UniqueConstraintError,
    UnprocessedItemsError,
    VersionConflictError,
    WriteOutcomeUnknownError,
    cancellation_reasons,
    validation_error,
)
from dynamoxy.events import EventKind
from dynamoxy.expressions import Condition, ExpressionBuilder, F, UpdateAction, Value, and_, as_condition
from dynamoxy.query import Query, Scan
from dynamoxy.schema import WRITE_ID_ATTRIBUTE, FieldSpec, IndexSpec, ModelSchema, models_for_table, schema_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from dynamoxy.client import Client
    from dynamoxy.schema import GlobalIndex, HookPoint, LocalIndex

__all__ = ["Repository"]

type WriteMode = Literal["create", "put", "save"]

#: Native method, operation name and ``ReturnValues`` of the single operation of a plan, by kind.
_SINGLE_OPERATIONS = {
    "put": ("put_item", "PutItem", "NONE"),
    "update": ("update_item", "UpdateItem", "ALL_NEW"),
    "delete": ("delete_item", "DeleteItem", "ALL_OLD"),
}


def utcnow() -> datetime:
    """The current time in UTC (what ``created_at``/``updated_at`` fields receive)."""
    return datetime.now(UTC)


def new_write_id() -> str:
    """A random id for one write (64 bits as hex text), stored in the item's ``_wid``.

    From the operating system's random source: ids of different processes never follow one
    seeded sequence (an application may seed ``random``).
    """
    return os.urandom(8).hex()


def _model_of_class(cls: type) -> type | None:
    """The model of a repository class, inherited too.

    It is the class's ``model``, or ``Model`` of a ``Repository[Model]`` base (also of a generic
    subclass of ``Repository``).
    """
    for klass in cls.__mro__:
        declared = klass.__dict__.get("model")
        if isinstance(declared, type):
            return declared
        for base in types.get_original_bases(klass) if "__orig_bases__" in klass.__dict__ else ():
            origin = typing.get_origin(base)
            arguments = typing.get_args(base)
            generic = isinstance(origin, type) and issubclass(origin, Repository) and len(arguments) == 1
            if generic and isinstance(arguments[0], type):
                return arguments[0]
    return None


class _Plan:
    """The native operations of one logical write, and how to explain their failures."""

    def __init__(self) -> None:
        self.operations: list[dict[str, Any]] = []
        self.explain: list[Any] = []

    def add(self, operation: dict[str, Any], explain: Any) -> None:
        self.operations.append(operation)
        self.explain.append(explain)


class _WritePlan:
    """A planned write of a repository: native operations, and what to do once they ran."""

    def __init__(
        self,
        repository: Repository[Any],
        plan: _Plan,
        mode: str,
        key: dict[str, Any],
        expected: Any,
        finalize: Any,
        *,
        upsert: bool = False,
        instance: Any = None,
        old: dict[str, Any] | None = None,
        prepared: tuple[dict[str, Any], dict[str, Any], int | None] | None = None,
        stale: bool = False,
        absent: bool = False,
        write_id: str | None = None,
        loaded: bool = False,
    ) -> None:
        self.repository = repository
        self.plan = plan
        self.mode = mode
        self.key = key
        self.expected = expected
        self.finalize = finalize
        self.upsert = upsert
        self.instance = instance
        self.old = old
        self.prepared = prepared
        # The guard conditions use the unique values the instance was loaded with: a failure may
        # only mean that another writer changed them since (no version, no caller's condition).
        self.stale = stale
        # The read found no item, and the write requires that there still is none: a failure that
        # returns an item means that another writer stored one since.
        self.absent = absent
        # The id this write stores in the item (None: the model stores none, or a delete).
        self.write_id = write_id
        # Whether the instance was loaded (a save of one without a version expects no version).
        self.loaded = loaded

    def outdated(self, item: dict[str, Any] | None) -> bool:
        """Whether a failed condition of the item, which returned ``item``, means the plan is outdated.

        The stored item is then no longer the one the plan was made from, and a plan made from
        the item as stored now is the write that was asked for.
        """
        return self.stale or (self.absent and item is not None)

    async def execute(self) -> dict[str, Any] | None:
        """Runs the plan alone.

        Returns the item attributes the single operation returned (``ALL_NEW`` for updates,
        ``ALL_OLD`` for deletes), or ``None`` for transactions and plans without operations.
        """
        repository = self.repository
        client = repository.client
        if not self.plan.operations:  # nothing to write: a delete of an item that is not stored
            await self.finalize(None)
            return None
        if len(self.plan.operations) == 1:
            operation = dict(self.plan.operations[0])
            kind = operation.pop("kind")
            method, name, returns = _SINGLE_OPERATIONS[kind]
            try:
                output = await client.call(
                    method,
                    name,
                    table=repository.table_name,
                    model=repository.model_class,
                    return_values=returns,
                    **operation,
                )
            except ConditionalCheckFailedError as error:
                raise repository._conflict(self, error, error.item) from error
            except WriteOutcomeUnknownError as error:
                attributes = await self._settle(error, name)
            else:
                attributes = output["attributes"]
            await self.finalize(attributes)
            return attributes
        try:
            await client.call(
                "transact_write_items",
                "TransactWriteItems",
                table=repository.table_name,
                model=repository.model_class,
                transact_items=self.plan.operations,
            )
        except TransactionCanceledError as error:
            raise repository._cancellation(self, error) from error
        if self.mode != "update":
            await self.finalize(None)
        return None

    async def _settle(self, error: WriteOutcomeUnknownError, operation: str) -> dict[str, Any] | None:
        """What the write returned, when the item as stored now shows that it was applied.

        An attempt that may have been applied was followed by no answer that settled it (see
        ``WriteOutcomeUnknownError``). The item DynamoDB returned to the next attempt, or a
        consistent read when it returned none, holds this write's id only if the write was applied
        (ids are random): then the write succeeded, and an update returns that item. Otherwise
        ``error`` is raised with the item as stored now (another writer may have written after
        this write, or the write may still be in flight).
        """
        repository = self.repository
        if self.write_id is None:
            raise error
        item = error.item
        if item is None:
            try:
                item = await repository._get_raw(self.key, consistent=True)
            except DynamoxyError:
                raise error from None  # the write's outcome is what matters; the read changes nothing
        if item is None or item.get(WRITE_ID_ATTRIBUTE) != self.write_id:
            error.item = item
            raise error
        repository.client.events.emit(
            EventKind.REQUEST_OUTCOME_SETTLED,
            f"{operation} of {repository.model_class.__name__} was applied by an earlier attempt",
            {"operation": operation, "table": repository.table_name, "model": repository.model_class.__name__},
        )
        return item if self.mode == "update" else None


class Repository[M]:
    """Typed persistence for one model class (see the module documentation).

    Args:
        client: The client.
        model: The model class (optional in subclasses of ``Repository[Model]``).
    """

    model: ClassVar[type[Any] | None] = None

    def __init__(self, client: Client, model: type[M] | None = None) -> None:
        resolved = model or _model_of_class(type(self))
        if resolved is None:
            raise TypeError(
                f"{type(self).__name__} does not know its model: pass it (Repository(client, User)) "
                "or subclass Repository[User]"
            )
        schema = schema_of(resolved)
        if not schema.is_table or schema.table is None:
            raise TypeError(f"{resolved.__name__} is not stored in a table; declare table='...' on the class")
        self.client = client
        self.model_class: type[M] = resolved
        self.schema: ModelSchema = schema
        self.table = client.table(schema.table)
        self.table_name = self.table.physical_name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.model_class.__name__} @ {self.table_name}>"

    # ---- helpers --------------------------------------------------------------------------

    def _context(self, operation: str) -> Any:
        return self.client.hook_context(operation, self.model_class)

    def _load(self, item: dict[str, Any]) -> M:
        schema = self.schema
        if schema.type_attribute is not None:
            self._check_entity(item)
        # Items come straight from responses: the instance's stored copy is the item itself.
        instance = schema._decode(item, item, self)
        hooks = self.client.hooks
        if hooks.has("after_load", self.model_class):
            hooks.run_sync("after_load", instance, self._context("load"))
        return instance

    def _check_entity(self, item: dict[str, Any]) -> None:
        """Refuses an item that the type attribute says another model of the table stores.

        Decoded as this model, it would lose the other model's fields, and a save would store it
        as this model. Entity names no model declares (an entity renamed since) still decode.

        Raises:
            ItemDecodeError: The item is another model's.
        """
        schema = self.schema
        stored = item.get(schema.type_attribute)  # type: ignore[arg-type]
        if stored is None or stored == schema.entity or not isinstance(stored, str):
            return
        other = models_for_table(schema.table or "").get(stored)
        if other is None:
            return
        raise ItemDecodeError(
            f"cannot decode {self.model_class.__name__} from a stored item (see .key): it stores another model, "
            f"{other.__name__} ({schema.type_attribute} is {stored!r}): read it with client.model({other.__name__})",
            model=self.model_class,
            key=schema.key_values(item),
        )

    def _load_written(self, item: dict[str, Any], operation: str) -> M:
        """:meth:`_load` of the item a write returned: a decode error says that the write was made."""
        try:
            return self._load(item)
        except ItemDecodeError as error:
            problems = "; ".join(str(problem) for problem in error.errors) or str(error)
            written = ItemDecodeError(
                f"{self.model_class.__name__}: the {operation} was applied, but the item it returned does not "
                f"decode{' (see .key)' if error.key else ''}: {problems}",
                model=error.model,
                key=error.key,
            )
            written.errors = error.errors
            raise written from None

    def _load_page(self, items: list[dict[str, Any]]) -> list[M]:
        if self.client.hooks.has("after_load", self.model_class):
            return [self._load(item) for item in items]
        decode = self.schema._decode
        return [decode(item, item, self) for item in items]

    def key(self, *key: Any, **key_values: Any) -> dict[str, Any]:
        """The stored primary key from values.

        Give positional partition/sort values, key field names, or the fields their templates use.
        """
        return self.schema.key_from(key, key_values)

    def _own(self, target: Any) -> M | None:
        """``target`` when it is an instance of this repository's model, ``None`` for a key.

        Raises:
            TypeError: ``target`` is an instance of a subclass: this repository would store it as
                its own model (dropping the subclass's fields and entity type).
        """
        if not isinstance(target, self.model_class):
            return None
        if type(target) is not self.model_class:
            name, other = self.model_class.__name__, type(target).__name__
            raise TypeError(
                f"expected a {name}, got an instance of its subclass {other}, which a repository of {name} would "
                f"store as a {name}: write it through client.model({other})"
            )
        return target

    def _target_key(self, target: Any) -> dict[str, Any]:
        if self._own(target) is not None:
            key = self.schema.key_of(target)
            stored = self._stored(target)
            return self._check_key_unchanged(stored, key) if stored is not None else key
        if isinstance(target, dict):
            schema = self.schema
            if target.keys() == schema._key_attribute_set and all(
                _is_stored_key_value(spec, target[spec.attribute]) for spec in schema._key_specs
            ):
                return dict(target)
            # Field values (a UUID, a datetime, ... where the attribute has the field's name).
            return schema.key_from((), target)
        if isinstance(target, tuple):
            return self.schema.key_from(target, {})
        return self.schema.key_from((target,), {})

    def _reader_options(self, spec: IndexSpec | None, *, query: bool) -> dict[str, Any]:
        """What reads of the table or of an index need to return this model's items only."""
        # Options depend on the schema alone (conditions are immutable): built once per target.
        cache: dict[tuple[str | None, bool], dict[str, Any]] = self.schema.__dict__.setdefault(
            "_reader_options_cache", {}
        )
        key = (spec.name if spec is not None else None, query)
        options = cache.get(key)
        if options is None:
            options = cache[key] = _reader_options(self.schema, spec, query=query)
        return options

    async def _get_raw(self, key: dict[str, Any], *, consistent: bool = True) -> dict[str, Any] | None:
        output = await self.client.call(
            "get_item",
            "GetItem",
            table=self.table_name,
            model=self.model_class,
            table_name=self.table_name,
            key=key,
            consistent_read=consistent,
        )
        return output["item"]

    # ---- reads ----------------------------------------------------------------------------

    async def get(self, *key: Any, consistent: bool = False, **key_values: Any) -> M | None:
        """The item with this key, or ``None``.

        Example::

            await users.get("u1")
            await orders.get(customer_id="c1", order_id="o1")

        Raises:
            ValueError: The key arguments are incomplete or invalid.
        """
        item = await self._get_raw(self.key(*key, **key_values), consistent=consistent)
        return None if item is None else self._load(item)

    async def get_or_raise(self, *key: Any, consistent: bool = False, **key_values: Any) -> M:
        """The item with this key.

        Raises:
            ItemNotFoundError: No item has this key.
        """
        stored = self.key(*key, **key_values)
        item = await self._get_raw(stored, consistent=consistent)
        if item is None:
            raise ItemNotFoundError(
                f"{self.model_class.__name__} does not exist (see .key)", model=self.model_class, key=stored
            )
        return self._load(item)

    async def exists(self, *key: Any, consistent: bool = False, **key_values: Any) -> bool:
        """Whether an item has this key."""
        return await self._get_raw(self.key(*key, **key_values), consistent=consistent) is not None

    async def refresh(self, instance: M, *, consistent: bool = True) -> M:
        """Reloads ``instance`` in place from DynamoDB.

        A loaded instance reloads the item it was loaded from, also when a key field changed.

        Raises:
            ItemNotFoundError: The item no longer exists.
        """
        # A loaded instance reloads the item it was loaded from, also when a key field changed.
        self._own(instance)
        stored = self._stored(instance)
        key = self.schema.key_values(stored) if stored is not None else self.schema.key_of(instance)
        item = await self._get_raw(key, consistent=consistent)
        if item is None:
            raise ItemNotFoundError(
                f"{self.model_class.__name__} no longer exists (see .key)", model=self.model_class, key=key
            )
        self._assign(instance, self._load(item), item)
        return instance

    def _assign(self, instance: M, source: M, item: dict[str, Any]) -> None:
        adapter = self.schema.adapter
        assign = getattr(adapter, "assign", None)
        if assign is not None:
            assign(instance, source, self.schema.fields)
        else:
            for name in self.schema.fields:
                adapter.set(instance, name, adapter.get(source, name))
        state = self.schema.state(instance)
        if state is not None:
            state.stored = dict(item)
            state.repository = self

    def query(
        self, *, index: str | GlobalIndex | LocalIndex | None = None, consistent: bool = False, **key_values: Any
    ) -> Query[M]:
        """Items of one partition of the table or of an index.

        ``key_values`` give the partition key (its field name, or the fields its template
        uses) and optionally the sort key. Without a sort key value, a templated sort key with
        a constant prefix (``"ORDER#{order_id}"``) restricts the query to this model's items.
        Add sort key conditions with ``.where()`` and filters with ``.filter()``.
        ``consistent=True`` reads with strong consistency (twice the read cost), which global
        secondary indexes do not support. Entries of a global index that does not hold every
        field (``KEYS_ONLY``, ``INCLUDE``) are read with ``.hydrate()`` (full items) or
        ``.project(...)`` (dicts); other reads of them raise ``ValueError`` before any request.

        Example::

            orders.query(customer_id="c1").where(Order.sk.begins_with("ORDER#2024")).descending()
            users.query(index=User.by_email, email="ada@example.com").first()
            orders.query(customer_id="c1", consistent=True)  # reflects every write that succeeded

        Raises:
            ValueError: The index or the key arguments are unknown or incomplete, or
                ``consistent=True`` targets a global secondary index.
        """
        spec = self._index(index)
        partition_spec = spec.partition_key if spec is not None else self.schema.partition_key
        sort_spec = spec.sort_key if spec is not None else self.schema.sort_key
        assert partition_spec is not None
        if spec is None:
            partition_value, remaining = self._partition_value(partition_spec, key_values)
        else:
            partition_value, remaining = self.schema.index_key(spec, key_values)
        condition: Condition = F(partition_spec.attribute) == partition_value
        automatic: Condition | None = None
        explicit = False
        if sort_spec is not None:
            sort_condition, is_automatic, remaining = self._sort_condition(sort_spec, remaining, key_values)
            if is_automatic:
                automatic = sort_condition
            elif sort_condition is not None:
                condition = condition & sort_condition
                explicit = True
        if remaining:
            raise ValueError(
                f"{self.model_class.__name__}.query(): unexpected arguments {', '.join(sorted(remaining))}"
            )
        return Query(
            self.client,
            self.table_name,
            decode=self._load,
            model=self.model_class,
            index=spec.name if spec is not None else None,
            global_index=spec is not None and spec.kind == "global",
            key_condition=condition,
            consistent=consistent,
            hydrate=self._hydrate,
            decode_page=self._load_page,
            auto_sort=automatic,
            explicit_sort=explicit,
            **self._reader_options(spec, query=True),
        )

    def _index(self, index: str | GlobalIndex | LocalIndex | None) -> IndexSpec | None:
        if index is None:
            return None
        name = index if isinstance(index, str) else index.name
        spec = self.schema.indexes.get(name or "")
        if spec is None:
            known = ", ".join(self.schema.indexes) or "none"
            raise ValueError(f"{self.model_class.__name__} has no index {name!r} (indexes: {known})")
        return spec

    def _partition_value(self, spec: FieldSpec, key_values: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
        remaining = dict(key_values)
        used: set[str] = set()
        value = self.schema._key_value(spec, remaining, [], used)
        for name in used:
            remaining.pop(name, None)
        return value, remaining

    def _sort_condition(
        self, spec: FieldSpec, remaining: dict[str, Any], arguments: Mapping[str, Any]
    ) -> tuple[Condition | None, bool, dict[str, Any]]:
        """The sort key condition of a query, whether dynamoxy added it, and the unused arguments.

        A sort key value among the arguments selects it, and so do values of every field of its
        template, some of which the partition key may use too (``"ORG#{org_id}"`` and
        ``"MEMBER#{org_id}#{user_id}"``), as long as one of them is not the partition key's
        (``remaining`` holds the arguments the partition key left, ``arguments`` all of them).
        Otherwise a template's constant prefix (``"ORDER#{order_id}"``) or a constant template
        restricts the query to this model's items.
        """
        template = spec.template
        names = {spec.name, spec.attribute}
        fields = template.fields if template is not None else ()
        if names & remaining.keys() or (
            fields and all(name in arguments for name in fields) and any(name in remaining for name in fields)
        ):
            value, unused = self._partition_value(spec, arguments)
            return F(spec.attribute) == value, False, {name: remaining[name] for name in remaining if name in unused}
        if template is not None and template.prefix and not template.is_constant:
            return F(spec.attribute).begins_with(template.prefix), True, remaining
        if template is not None and template.is_constant:
            return F(spec.attribute) == template.template, True, remaining
        return None, False, remaining

    async def _hydrate(self, items: list[dict[str, Any]], consistent: bool) -> list[dict[str, Any] | None]:
        """The full items of index entries, in order (``None`` for items deleted meanwhile)."""
        keys = [self.schema.key_values(item) for item in items]
        full = await self.table.batch_get(keys, consistent=consistent)
        identity = self.schema._key_identity
        by_key = {identity(item): item for item in full}
        return [by_key.get(identity(key)) for key in keys]

    def scan(self, *, index: str | GlobalIndex | LocalIndex | None = None, consistent: bool = False) -> Scan[M]:
        """Every item of this model, in the table or an index.

        Combine with ``.filter()``, ``.segments(n)`` for parallel scans and ``.limit()``.
        ``consistent=True`` reads with strong consistency (not on global secondary indexes).

        Raises:
            ValueError: The index is unknown, or ``consistent=True`` targets a global secondary
                index.
        """
        spec = self._index(index)
        return Scan(
            self.client,
            self.table_name,
            decode=self._load,
            model=self.model_class,
            index=spec.name if spec is not None else None,
            global_index=spec is not None and spec.kind == "global",
            consistent=consistent,
            hydrate=self._hydrate,
            decode_page=self._load_page,
            **self._reader_options(spec, query=False),
        )

    async def count(
        self, *, index: str | GlobalIndex | LocalIndex | None = None, consistent: bool = False, **key_values: Any
    ) -> int:
        """Items in a partition (``query`` arguments), or of the whole model without arguments."""
        if key_values:
            return await self.query(index=index, consistent=consistent, **key_values).count()
        return await self.scan(index=index, consistent=consistent).count()

    async def batch_get(self, keys: Iterable[Any], *, consistent: bool = False) -> list[M]:
        """Reads many items by key; missing keys are skipped and order follows ``keys``.

        Keys may be stored keys (``User.key(...)``), partition values, or ``(partition, sort)``
        tuples.
        """
        stored = [self._target_key(key) for key in keys]
        identity = self.schema._key_identity
        unique: dict[Any, dict[str, Any]] = {}
        for key in stored:
            unique.setdefault(identity(key), key)
        if not unique:
            return []
        output = await self.client.call(
            "batch_get",
            "BatchGetItem",
            table=self.table_name,
            model=self.model_class,
            request_items={self.table_name: {"keys": list(unique.values()), "consistent_read": consistent}},
        )
        pending = output["unprocessed_keys"].get(self.table_name)
        if pending and pending["keys"]:
            raise UnprocessedItemsError(
                f"{len(pending['keys'])} keys of {self.table_name} stayed unprocessed (throttling)",
                unprocessed=[("get", key) for key in pending["keys"]],
            )
        found = {identity(item): item for item in output["items"].get(self.table_name, [])}
        result: list[M] = []
        for key in stored:
            item = found.get(identity(key))
            if item is not None:
                result.append(self._load(item))
        return result

    # ---- writes ---------------------------------------------------------------------------
    #
    # Every write is planned first (validation, automatic fields, conditions, guard items) and
    # then executed: one native call when the plan has one operation, a transaction otherwise.
    # Transactions (`client.transaction()`) execute the plans of several writes together.

    def _prepare(
        self, instance: M, mode: str, *, standalone: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any], int | None]:
        """Validates and computes the stored item. Returns (item, changes to apply, expected version).

        Except for creates, the item keeps the attributes of the loaded item that no field declares.
        A ``standalone`` write (not part of a transaction or batch) stores a new write id.
        """
        schema = self.schema
        if self._own(instance) is None:
            raise TypeError(f"expected a {self.model_class.__name__}, got {type(instance).__name__}")
        schema.validate(instance)
        values = schema.values(instance)
        changes: dict[str, Any] = {}
        if schema._timestamps:
            now = utcnow()
            if schema.created_at_field is not None and values.get(schema.created_at_field.name) is None:
                changes[schema.created_at_field.name] = now
            if schema.updated_at_field is not None:
                changes[schema.updated_at_field.name] = now
        expected: int | None = None
        if schema.version_field is not None:
            current = values.get(schema.version_field.name) or 0
            changes[schema.version_field.name] = current + 1
            expected = current
        values.update(changes)
        if schema._derived:
            derived = schema.render_derived(values)
            values.update(derived)
            changes.update(derived)
        item = schema._encode_rendered(values)
        if mode != "create":
            stored = self._stored(instance)
            if stored is not None:
                kept = self._check_unchanged(stored, item)  # what changes no value stays as stored
                item.update(kept)
                if schema._derived:
                    # Derived key fields of the instance hold the key as stored too.
                    fields = schema.fields
                    changes.update(
                        (name, value)
                        for name, value in schema.decode_values(kept).items()
                        if fields[name].template is not None
                    )
                item = self._with_undeclared(stored, item)
        if standalone and schema.write_ids:  # replaces the id of the write that stored the loaded item
            item[WRITE_ID_ATTRIBUTE] = new_write_id()
        return item, changes, expected

    def _with_undeclared(self, stored: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """``item`` with the attributes of the ``stored`` item (same key) that no field declares.

        A newer version of the application (during a rolling deploy) or another service may have
        written them: a write of the item as loaded keeps them, and ``update(F(name).remove())``
        removes one.
        """
        declared = self.schema._stored_names
        undeclared = {name: value for name, value in stored.items() if name not in declared}
        return {**undeclared, **item} if undeclared else item

    def _apply(self, instance: M, changes: Mapping[str, Any], item: dict[str, Any] | None) -> None:
        adapter = self.schema.adapter
        for name, value in changes.items():
            adapter.set(instance, name, value)
        state = self.schema.state(instance)
        if state is not None:
            state.stored = dict(item) if item is not None else None
            state.repository = self

    def _guard_conditions(self, old: Mapping[str, Any], specs: Iterable[FieldSpec]) -> list[Condition]:
        """Conditions that the unique values of the stored item are still ``old``'s.

        A value ``old`` does not have may be missing or stored as DynamoDB's ``NULL`` (by another
        writer, or by ``set(None)`` of older versions), which reads as ``None`` too.
        """
        return [
            F(spec.attribute) == old[spec.attribute]
            if old.get(spec.attribute) is not None
            else F(spec.attribute).not_exists() | F(spec.attribute).is_type("NULL")
            for spec in specs
        ]

    def _plan_guards(
        self,
        plan: _Plan,
        old: Mapping[str, Any] | None,
        new: Mapping[str, Any] | None,
        key: dict[str, Any],
        specs: Iterable[FieldSpec],
    ) -> None:
        schema = self.schema
        assert schema.partition_key is not None
        for spec in specs:
            before = old.get(spec.attribute) if old is not None else None
            after = new.get(spec.attribute) if new is not None else None
            # As DynamoDB compares them: a float read back is the Decimal that was stored.
            if stored_identity(before) == stored_identity(after):
                continue
            if before is not None:
                plan.add(
                    {"kind": "delete", "table_name": self.table_name, "key": guard_key(schema, spec, before)}, None
                )
            if after is not None:
                builder = ExpressionBuilder()
                plan.add(
                    {
                        "kind": "put",
                        "table_name": self.table_name,
                        "item": guard_item(schema, spec, after, key),
                        "condition_expression": builder.condition(
                            schema._path(schema.partition_key.attribute).not_exists()
                        ),
                        **builder.arguments(),
                    },
                    ("guard", spec, after),
                )

    def _stored(self, instance: M) -> dict[str, Any] | None:
        """The item ``instance`` was loaded with (``None``: it was not loaded)."""
        state = self.schema.state(instance)
        return state.stored if state is not None and state.loaded else None

    def _check_key_unchanged(self, stored: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
        """The key of the ``stored`` item, which a write through a loaded instance (whose key is ``item``'s) addresses.

        It is the key as stored, also in a form the field's codec does not write (see
        :meth:`ModelSchema.addresses`).

        Raises:
            ValueError: The key (or a field a key template uses) changed since the instance was
                loaded.
        """
        key = self.schema.addresses(stored, item)
        if key is None:
            raise ValueError(
                f"{self.model_class.__name__}: the key of this instance changed since it was loaded, and DynamoDB "
                "cannot change keys: write the new item from a copy (create(instance.copy())) and delete the old one"
            )
        return key

    def _check_unchanged(self, stored: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """Refuses a write of a loaded instance that changes its key or an immutable field.

        Returns the attributes the write keeps as stored: the key (see
        :meth:`_check_key_unchanged`), and immutable values stored in a form that decodes to the
        instance's value but that the field's codec does not write.

        Raises:
            ValueError: The key (or a field a key template uses) or an immutable value changed
                since the instance was loaded.
        """
        schema = self.schema
        name = self.model_class.__name__
        kept = self._check_key_unchanged(stored, item)
        for spec in schema._immutable:
            old = stored.get(spec.attribute)
            if old is None:
                continue
            new = item.get(spec.attribute)
            if stored_identity(old) == stored_identity(new):
                continue
            if new is None or not _same_value(spec, old, new):
                raise ValueError(f"{name}.{spec.name} is immutable: it cannot change once stored")
            kept[spec.attribute] = old
        return kept

    async def _plan_put(
        self,
        instance: M,
        mode: WriteMode,
        condition: Condition | bool | None,
        *,
        prepared: tuple[dict[str, Any], dict[str, Any], int | None] | None = None,
        standalone: bool = False,
    ) -> _WritePlan:
        """Plans a put; ``prepared`` (a plan's) plans it again with the item as stored now.

        A ``standalone`` put (not part of a transaction) stores a write id: transactions need none
        (their token makes their retries exact), and the same transaction sent again with the same
        token must be identical.
        """
        schema = self.schema
        hooks = self.client.hooks
        fresh = prepared is not None
        if prepared is None:
            if hooks.has("before_save", type(instance)):
                await hooks.run("before_save", instance, self._context(mode))
            prepared = self._prepare(instance, mode, standalone=standalone)
        item, changes, expected = prepared
        key = schema.key_values(item)
        state = schema.state(instance)
        loaded = state.stored if state is not None and state.loaded else None
        assert schema.partition_key is not None
        partition = schema._path(schema.partition_key.attribute)
        parts: list[Condition] = []
        if mode == "create":
            parts.append(partition.not_exists())
        elif mode == "save" and schema.version_field is not None:
            version = schema._path(schema.version_field.attribute)
            if expected:
                parts.append(version == expected)
            elif loaded is not None:
                # Loaded from an item stored before the model had a version: it must still exist
                # without one (and gets its first version now).
                parts.append(partition.exists() & version.not_exists())
            else:
                parts.append(partition.not_exists())
        user_condition = as_condition(condition)
        if user_condition is not None:
            parts.append(user_condition)
        old: dict[str, Any] | None = None
        stale = False
        if schema.unique_fields and mode != "create":
            if loaded is not None and not fresh:
                old = loaded
                versioned = mode == "save" and schema.version_field is not None
                stale = user_condition is None and not versioned
            else:
                old = await self._get_raw(key)
            if old is None:
                parts.append(partition.not_exists())
            else:
                parts.extend(self._guard_conditions(old, schema.unique_fields))
        plan = _Plan()
        builder = ExpressionBuilder()
        plan.add(
            {
                "kind": "put",
                "table_name": self.table_name,
                "item": item,
                "condition_expression": builder.condition(and_(*parts)) if parts else None,
                "return_values_on_condition_check_failure": "ALL_OLD",
                **builder.arguments(),
            },
            ("item", mode),
        )
        if schema.unique_fields:
            self._plan_guards(plan, old, item, key, schema.unique_fields)

        async def finalize(_: dict[str, Any] | None) -> None:
            self._apply(instance, changes, item)
            if hooks.has("after_save", type(instance)):
                await hooks.run("after_save", instance, self._context(mode))

        write_id = item.get(WRITE_ID_ATTRIBUTE) if standalone and schema.write_ids else None
        return _WritePlan(
            self,
            plan,
            mode,
            key,
            expected,
            finalize,
            prepared=prepared,
            stale=stale,
            write_id=write_id,
            loaded=loaded is not None,
        )

    async def _write(self, instance: M, mode: WriteMode, condition: Condition | bool | None) -> M:
        write = await self._plan_put(instance, mode, condition, standalone=True)
        try:
            await write.execute()
        except ConditionalCheckFailedError as error:
            if type(error) is not ConditionalCheckFailedError or not write.outdated(error.item):
                raise
            # Another writer changed the unique values since the instance was loaded: plan the
            # guards again from the item as stored now (the write itself is unchanged).
            await (await self._plan_put(instance, mode, condition, prepared=write.prepared, standalone=True)).execute()
        return instance

    async def create(self, item: M, *, condition: Condition | bool | None = None) -> M:
        """Writes a new item.

        Raises:
            ItemAlreadyExistsError: An item with the same key exists.
            UniqueConstraintError: A unique value is taken.
            ValidationError: The instance is invalid.
        """
        return await self._write(item, "create", condition)

    async def put(self, item: M, *, condition: Condition | bool | None = None) -> M:
        """Writes an item, replacing any stored item with the same key.

        Raises:
            ConditionalCheckFailedError: ``condition`` did not hold.
            UniqueConstraintError: A unique value is taken.
            ValueError: The instance was loaded and its key or an immutable field changed since.
        """
        return await self._write(item, "put", condition)

    async def save(self, item: M, *, condition: Condition | bool | None = None) -> M:
        """Writes an item with optimistic locking (models with a ``version()`` field).

        Raises:
            VersionConflictError: The stored version differs from the instance's (or, for a
                new instance, the key exists).
            ConditionalCheckFailedError: ``condition`` did not hold.
            ValueError: The instance was loaded and its key or an immutable field changed since.
        """
        return await self._write(item, "save", condition)

    async def _plan_update(
        self,
        target: Any,
        actions: Sequence[UpdateAction],
        condition: Condition | bool | None,
        upsert: bool,
        *,
        standalone: bool = False,
        fresh: bool = False,
    ) -> _WritePlan:
        """Plans an update; a ``standalone`` one (not part of a transaction) stores a write id.

        A ``fresh`` plan is made again, from the item as stored now (its hooks ran already).
        """
        schema = self.schema
        instance = self._own(target)
        actions_list = list(actions)
        if not actions_list:
            raise ValueError("update() needs at least one action, e.g. User.visits.add(1)")
        hooks = self.client.hooks
        if not fresh and instance is not None and hooks.has("before_update", type(instance)):
            await hooks.run("before_update", instance, self._context("update"))
        key = self._target_key(target)
        state = schema.state(instance) if instance is not None else None
        touched: dict[str, FieldSpec] = {}
        literals: dict[str, Any] = {}
        attributes = schema.attributes
        for position, action in enumerate(actions_list):
            elements = action.path.elements
            spec = attributes.get(str(elements[0]))
            if spec is None:
                continue
            if spec.role is not None or spec.template is not None:
                raise ValueError(
                    f"{self.model_class.__name__}.{spec.name} is a key or derived attribute and cannot be updated"
                )
            if spec.name in schema._key_template_fields:
                raise ValueError(
                    f"{self.model_class.__name__}.{spec.name} is part of the key (a key template uses it) and cannot "
                    "be updated: write the new item from a copy (create(instance.copy())) and delete the old one"
                )
            if spec.immutable:
                raise ValueError(f"{self.model_class.__name__}.{spec.name} is immutable")
            touched[spec.name] = spec
            if len(elements) == 1:
                is_literal, value = action.literal()
                if is_literal and value is not None:
                    literals[spec.name] = spec.codec.decode(value)
                elif is_literal or action.kind == "REMOVE":
                    # No attribute: None, or an empty set for set fields (see FieldSpec.empty).
                    empty = spec.empty()
                    literals[spec.name] = None if empty is MISSING else empty
                else:
                    continue
                # Values set outright must satisfy the field like a constructor argument would
                # (arithmetic such as add() cannot be checked locally: guard it with a condition).
                try:
                    checked = spec.validate(literals[spec.name])
                except ValueError as error:
                    model = self.model_class.__name__
                    problem = "is required and cannot be removed" if literals[spec.name] is None else str(error)
                    raise validation_error(
                        f"{model}.{spec.name}: {problem}", [FieldError(spec.name, problem, literals[spec.name])]
                    ) from None
                if checked is not None and checked != literals[spec.name]:  # a validator normalized it
                    literals[spec.name] = checked
                    encoded = spec.codec.encode(checked)
                    actions_list[position] = (
                        UpdateAction("SET", action.path, Value(encoded))
                        if encoded is not None
                        else UpdateAction("REMOVE", action.path, None)  # an empty set
                    )
        assert schema.partition_key is not None
        parts: list[Condition] = []
        if not upsert:
            parts.append(schema._path(schema.partition_key.attribute).exists())
        expected: Any = None
        now = utcnow() if schema._timestamps else None
        # Automatic fields the update changes: their new value, or MISSING when DynamoDB computes it.
        automatic: dict[str, Any] = {}
        if schema.version_field is not None:
            actions_list.append(UpdateAction("ADD", schema._path(schema.version_field.attribute), Value(1)))
            automatic[schema.version_field.name] = MISSING
            if instance is not None:
                expected = schema.adapter.get(instance, schema.version_field.name)
                if expected is not None:
                    parts.append(schema._path(schema.version_field.attribute) == expected)
        if schema.updated_at_field is not None and schema.updated_at_field.name not in touched:
            field = schema.updated_at_field
            actions_list.append(UpdateAction("SET", schema._path(field.attribute), Value(field.codec.encode(now))))
            automatic[field.name] = now
        if upsert and schema.created_at_field is not None and schema.created_at_field.name not in touched:
            field = schema.created_at_field
            actions_list.append(schema._path(field.attribute).set_if_not_exists(field.codec.encode(now)))
            automatic[field.name] = MISSING
        completion: dict[str, Any] = {}  # what an upsert writes when it creates the item
        if upsert:
            provided = self._field_values_of(target) if instance is None else {}
            # What default factories that take data (pydantic) get: the key and the set values.
            known = {**self._key_field_values(target), **literals} if instance is None else {}
            completion_actions, completion = self._upsert_completion(instance, touched, provided, known)
            actions_list.extend(completion_actions)
        loaded = state.stored if state is not None and state.loaded and not fresh else None
        stored_old = loaded
        unique_touched = [spec for spec in schema.unique_fields if spec.name in touched] if schema.unique_fields else []
        for spec in unique_touched:
            if spec.name not in literals:
                raise ValueError(
                    f"{self.model_class.__name__}.{spec.name} is unique: update it with .set(value) or .remove() so "
                    "dynamoxy can maintain its guard item"
                )
        # Values the update sets outright, automatic ones (updated_at) included.
        assigned = {**literals, **{name: value for name, value in automatic.items() if value is not MISSING}}
        changed = touched.keys() | automatic.keys()
        derived_specs = (
            [
                spec
                for spec in schema._derived
                if spec.role is None and set(spec.template.fields) & changed  # type: ignore[union-attr]
            ]
            if schema._derived
            else []
        )
        needs_read = bool(unique_touched) and stored_old is None
        if derived_specs and stored_old is None and instance is None:
            needs_read = needs_read or any(
                name not in assigned
                for spec in derived_specs
                for name in spec.template.fields  # pyrefly: ignore[missing-attribute]
            )
        if needs_read:
            stored_old = await self._get_raw(key)
            if stored_old is None and not upsert:
                raise ItemNotFoundError(
                    f"{self.model_class.__name__} does not exist (see .key)", model=self.model_class, key=key
                )
        if derived_specs:
            known: dict[str, Any] = {}
            # Changed by arithmetic, functions or in nested paths (the version: ADD).
            computed = changed - assigned.keys()
            if any(computed.intersection(spec.template.fields) for spec in derived_specs):  # type: ignore[union-attr]
                # The new values of such fields are what the update makes of the item as last read,
                # evaluated like DynamoDB does (exact for versioned models, whose version is checked).
                if stored_old is not None:
                    base = stored_old
                elif instance is not None:
                    base = schema.encode(instance)
                else:
                    base = {}  # an upsert that creates the item
                known.update(schema.decode_values(self._evaluate(base, actions_list)))
            elif stored_old is not None:
                known.update(schema.decode_values(stored_old))
            elif instance is not None:
                known.update(schema.values(instance))
            else:
                known.update(completion)  # an upsert that creates the item writes these values
            known.update(assigned)
            for spec in derived_specs:
                assert spec.template is not None
                rendered = spec.template.render(known, schema.codecs)
                path = schema._path(spec.attribute)
                actions_list.append(
                    UpdateAction("SET", path, Value(rendered)) if rendered is not None else path.remove()
                )
        if upsert:
            # Derived attributes the actions do not affect: rendered from what a created item holds,
            # and only written when missing (existing items keep theirs).
            created = {**completion, **literals}
            for spec in schema.fields.values():
                if spec.template is None or spec.role is not None or spec in derived_specs:
                    continue
                rendered = spec.template.render(created, schema.codecs)
                if rendered is not None:
                    actions_list.append(schema._path(spec.attribute).set_if_not_exists(rendered))
        # The read (for unique values or derived attributes) found no item: an upsert creates it
        # only if it is still missing, since an item another writer created meanwhile has a guard
        # of its own unique value to delete, and its own values to render derived attributes from.
        absent = needs_read and stored_old is None
        if unique_touched and stored_old is not None:
            parts.extend(self._guard_conditions(stored_old, unique_touched))
        elif absent:
            parts.append(schema._path(schema.partition_key.attribute).not_exists())
        user_condition = as_condition(condition)
        if user_condition is not None:
            parts.append(user_condition)
        # Guards of the values the instance was loaded with, and no version or caller's condition.
        stale = bool(unique_touched) and loaded is not None and expected is None and user_condition is None
        write_id = new_write_id() if standalone and schema.write_ids else None
        if write_id is not None:
            actions_list.append(F(WRITE_ID_ATTRIBUTE).set(write_id))
        plan = _Plan()
        builder = ExpressionBuilder()
        plan.add(
            {
                "kind": "update",
                "table_name": self.table_name,
                "key": key,
                "update_expression": builder.update(actions_list),
                "condition_expression": builder.condition(and_(*parts)) if parts else None,
                "return_values_on_condition_check_failure": "ALL_OLD",
                **builder.arguments(),
            },
            ("item", "update"),
        )
        if unique_touched:
            new_values = {
                spec.attribute: spec.codec.encode(literals[spec.name]) if literals[spec.name] is not None else None
                for spec in unique_touched
            }
            self._plan_guards(plan, stored_old or {}, new_values, key, unique_touched)

        async def finalize(attributes: dict[str, Any] | None) -> None:
            if instance is None:
                return
            if attributes is None:  # in a transaction, which returns nothing
                attributes = await self._refreshed_after_commit(stored_old, instance, actions_list, key)
            if attributes is not None:
                self._assign(instance, self._load_written(attributes, "update"), attributes)
            if hooks.has("after_update", type(instance)):
                await hooks.run("after_update", instance, self._context("update"))

        return _WritePlan(
            self,
            plan,
            "update",
            key,
            expected,
            finalize,
            upsert=upsert,
            instance=instance,
            stale=stale,
            absent=absent,
            write_id=write_id,
        )

    def _field_values_of(self, target: Any) -> dict[str, Any]:
        """Field values given as an update target (``{"customer_id": "c1", "order_id": "o1"}``)."""
        if not isinstance(target, dict):
            return {}
        key_names = {spec.attribute for spec in self.schema.key_fields()}
        if set(target) == key_names:  # a stored key: the values its templates used are unknown
            return {}
        return {name: value for name, value in target.items() if name in self.schema.fields}

    def _key_field_values(self, target: Any) -> dict[str, Any]:
        """The values of the key fields without a template in a stored key (``{"pk": "a"}``)."""
        if not isinstance(target, dict):
            return {}
        values: dict[str, Any] = {}
        for spec in self.schema.key_fields():
            if spec.template is None and spec.attribute in target:
                try:
                    values[spec.name] = spec.codec.decode(target[spec.attribute])
                except (ValueError, TypeError):
                    continue
        return values

    def _upsert_completion(
        self,
        instance: M | None,
        touched: Mapping[str, FieldSpec],
        provided: Mapping[str, Any],
        known: Mapping[str, Any] | None = None,
    ) -> tuple[list[UpdateAction], dict[str, Any]]:
        """Actions that make an item created by ``update(..., upsert=True)`` a complete model item.

        The entity type is set, and every field the actions leave alone gets ``if_not_exists``:
        the instance's value, or for keys the given field values and the field's default.
        Existing items keep their values. Keys give no default to fields that store a value as
        no attribute (``None``, an empty set): ``if_not_exists`` would replace an existing
        item's, so a created item reads that value. Returns the actions and the Python values a
        created item gets.

        Raises:
            ValueError: Before any I/O: a required field has no value, or a unique value needs
                its guard item written by the actions.
        """
        schema = self.schema
        model = self.model_class.__name__
        actions: list[UpdateAction] = []
        values: dict[str, Any] = {}
        if schema.type_attribute is not None and schema.entity is not None:
            actions.append(UpdateAction("SET", schema._path(schema.type_attribute), Value(schema.entity)))
        loaded = instance is not None and (state := schema.state(instance)) is not None and state.loaded
        missing: list[str] = []
        for spec in schema.fields.values():
            if spec.name in touched or spec.role is not None or spec.derived:
                continue
            if spec.auto in ("created_at", "updated_at", "version"):
                continue
            if instance is not None:
                value = schema.adapter.get(instance, spec.name)
            elif spec.name in provided:
                value = spec.validate(provided[spec.name])
            elif spec.has_default and spec.empty() is not MISSING:
                continue  # an existing item's None or empty set is no attribute either
            else:
                value = spec.initial({**(known or {}), **provided, **values})
            if value is MISSING or value is None:
                if value is MISSING and not spec.optional:
                    missing.append(spec.name)
                continue
            values[spec.name] = value
            if spec.unique:
                if loaded:  # the item and its guard existed when it was loaded
                    continue
                raise ValueError(
                    f"{model}.{spec.name} is unique: an upsert that may create the item must set it in the "
                    f"actions ({model}.{spec.name}.set(...)) so dynamoxy can write its guard item"
                )
            encoded = spec.codec.encode(value)
            if encoded is None or (isinstance(encoded, (set, frozenset)) and not encoded):
                continue
            actions.append(schema._path(spec.attribute).set_if_not_exists(encoded))
        if missing:
            names = ", ".join(missing)
            setters = ", ".join(
                f"{model}.{name}.{'set' if schema.fields[name].unique else 'set_if_not_exists'}(...)"
                for name in missing
            )
            hint = ""
            if any(spec.template is not None for spec in schema.key_fields()):
                hint = f" or pass the key as field values ({{{', '.join(repr(name) + ': ...' for name in missing)}}})"
            raise ValueError(
                f"update(..., upsert=True) creates {model} when it is missing, but nothing sets the required "
                f"field(s) {names}: set them in the actions ({setters}){hint}, or create() the item first"
            )
        return actions, values

    def _updated_locally(
        self, stored: dict[str, Any] | None, instance: M, actions: Sequence[UpdateAction]
    ) -> dict[str, Any]:
        """The item after ``actions``, evaluated like DynamoDB on the item as last read.

        Exact for versioned models (the version condition guarantees the stored item was the one
        loaded). For other models, changes other writers made meanwhile are not reflected.
        Instances that were never loaded start from their own values.
        """
        return self._evaluate(stored if stored is not None else self.schema.encode(instance), actions)

    async def _refreshed_after_commit(
        self, stored: dict[str, Any] | None, instance: M, actions: Sequence[UpdateAction], key: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The item an update in a committed transaction left, to refresh ``instance`` with.

        The update evaluated on the item as last read; when it does not apply to that item
        (another writer changed an unversioned item since it was read: a ``list_append`` to an
        attribute the instance read as missing), the item as stored now. The transaction was
        applied, so nothing here raises: when the item cannot be read (or is gone), the instance
        forgets its stored copy, as if it had not been loaded, and ``None`` is returned.
        """
        try:
            return self._updated_locally(stored, instance, actions)
        except DynamoxyError:
            pass
        try:
            current = await self._get_raw(key)
        except Exception:  # noqa: BLE001 - the commit succeeded: the refresh may not fail it
            current = None
        if current is None:
            state = self.schema.state(instance)
            if state is not None:
                state.stored = None
        return current

    def _evaluate(self, item: dict[str, Any], actions: Sequence[UpdateAction]) -> dict[str, Any]:
        """``item`` (stored form) after ``actions``, evaluated by the core's evaluator like DynamoDB does.

        Raises:
            ValidationError: The actions are invalid for ``item`` (DynamoDB's message).
        """
        builder = ExpressionBuilder()
        expression = builder.update(actions)
        assert expression is not None, "updates have at least one action"
        return _core._apply_update(item, expression, names=builder.names or None, values=builder.values or None)

    async def update(
        self,
        target: M | Mapping[str, Any],
        /,
        *actions: UpdateAction,
        condition: Condition | bool | None = None,
        upsert: bool = False,
    ) -> M:
        """Changes attributes in place and returns the new state.

        ``target`` is an instance (refreshed in place; its version is checked for versioned
        models), a stored key (``User.key("u1")``) or key field values
        (``{"customer_id": "c1", "order_id": "o1"}``).

        With ``upsert=True`` a missing item is created. It is completed like a constructed
        instance: untouched fields get their defaults (or the instance's values) with
        ``if_not_exists``, derived keys and the entity type are written, and required fields
        without a default must be set by the actions (``User.name.set_if_not_exists("Ada")``).
        Key targets give ``X | None`` and set fields no default, since ``if_not_exists`` would
        replace an existing item's ``None`` or empty set: a created item reads those.

        Example::

            user = await users.update(user, User.visits.add(1), User.name.set("Ada"))
            await users.update(User.key("u1"), User.tags.add({"admin"}), condition=User.active == True)

        Raises:
            ItemNotFoundError: The item does not exist (unless ``upsert``).
            VersionConflictError: The instance's version is stale.
            ConditionalCheckFailedError: ``condition`` did not hold.
            UniqueConstraintError: A new unique value is taken.
            ValueError: An action targets a key, immutable or derived attribute (or a field a
                key template uses), a unique attribute is set with something other than a plain
                value, an upsert leaves a required field without a value, or the instance was
                loaded and its key changed since.
        """
        write = await self._plan_update(target, actions, condition, upsert, standalone=True)
        try:
            attributes = await write.execute()
        except ConditionalCheckFailedError as error:
            # Not upserts of stale instances, whose error says to refresh the instance: planned
            # again after another writer deleted the item, one would create it without the unique
            # values that the instance had and the actions do not set. An upsert whose read found
            # no item is planned again when another writer created it meanwhile.
            if (
                type(error) is not ConditionalCheckFailedError
                or not write.outdated(error.item)
                or (upsert and not write.absent)
            ):
                raise
            # Another writer changed the unique values since the instance was loaded, or created
            # the item since the upsert read it: plan the update again from the item as stored now.
            write = await self._plan_update(target, actions, condition, upsert, standalone=True, fresh=True)
            attributes = await write.execute()
        if attributes is None:  # executed as a transaction (unique attributes): read the result
            attributes = await self._get_raw(write.key)
            if attributes is None:
                raise ItemNotFoundError(
                    f"{self.model_class.__name__} does not exist (see .key)", model=self.model_class, key=write.key
                )
            await write.finalize(attributes)
        if isinstance(target, self.model_class):
            return target
        return self._load_written(attributes, "update")

    async def _plan_delete(self, target: Any, condition: Condition | bool | None, *, fresh: bool = False) -> _WritePlan:
        """Plans a delete; a ``fresh`` plan is made again, from the item as stored now (its hooks ran already).

        Models with unique attributes delete the guards of the stored item: the item the instance
        was loaded with, or the one a read finds. When none is stored, the plan has no operations,
        unless a condition applies (the caller's, or the version): then it deletes the key on
        condition that still no item is stored, so the condition holds or fails for the missing
        item as it does for other models.
        """
        schema = self.schema
        instance = self._own(target)
        key = self._target_key(target)
        hooks = self.client.hooks
        if not fresh and instance is not None and hooks.has("before_delete", type(instance)):
            await hooks.run("before_delete", instance, self._context("delete"))
        parts: list[Condition] = []
        expected: Any = None
        if instance is not None and schema.version_field is not None:
            expected = schema.adapter.get(instance, schema.version_field.name)
            if expected is not None:
                parts.append(schema._path(schema.version_field.attribute) == expected)
        user_condition = as_condition(condition)
        if user_condition is not None:
            parts.append(user_condition)
        old: dict[str, Any] | None = None
        stale = absent = False
        if schema.unique_fields:
            loaded = self._stored(instance) if instance is not None and not fresh else None
            old = loaded if loaded is not None else await self._get_raw(key)
            stale = loaded is not None and not parts  # guards of the values as loaded, and nothing else

            if old is not None:
                parts.extend(self._guard_conditions(old, schema.unique_fields))
            elif parts:
                # An item stored after the read has guards that this plan does not delete.
                assert schema.partition_key is not None
                parts.append(schema._path(schema.partition_key.attribute).not_exists())
                absent = True
        plan = _Plan()
        if old is not None or absent or not schema.unique_fields:  # else nothing is stored, nor checked
            builder = ExpressionBuilder()
            plan.add(
                {
                    "kind": "delete",
                    "table_name": self.table_name,
                    "key": key,
                    "condition_expression": builder.condition(and_(*parts)) if parts else None,
                    "return_values_on_condition_check_failure": "ALL_OLD",
                    **builder.arguments(),
                },
                ("item", "delete"),
            )
        if old is not None:
            self._plan_guards(plan, old, None, key, schema.unique_fields)

        async def finalize(_: dict[str, Any] | None) -> None:
            if instance is not None:
                state = schema.state(instance)
                if state is not None:
                    state.stored = None
                    state.repository = self  # stays bound
                if hooks.has("after_delete", type(instance)):
                    await hooks.run("after_delete", instance, self._context("delete"))

        return _WritePlan(self, plan, "delete", key, expected, finalize, old=old, stale=stale, absent=absent)

    async def delete(
        self, target: M | Mapping[str, Any] | Any, /, *, condition: Condition | bool | None = None
    ) -> M | None:
        """Deletes an item and returns it (``None`` when nothing was stored under the key).

        ``target`` is an instance (its version is checked for versioned models), a stored key,
        a partition value or a ``(partition, sort)`` tuple. As in DynamoDB, ``condition`` also
        applies when nothing is stored: ``User.name == "Ada"`` fails then, and
        ``User.name.not_exists()`` holds.

        Raises:
            VersionConflictError: The instance's version is stale.
            ConditionalCheckFailedError: ``condition`` did not hold.
            ValueError: The instance was loaded and its key changed since.
        """
        write = await self._plan_delete(target, condition)
        try:
            deleted = await write.execute()
        except ConditionalCheckFailedError as error:
            if type(error) is not ConditionalCheckFailedError or not write.outdated(error.item):
                raise
            # Another writer changed or deleted the item since the instance was loaded (or stored
            # one since the read found none): plan the delete again from the item as stored now.
            write = await self._plan_delete(target, condition, fresh=True)
            deleted = await write.execute()
        if deleted is None and len(write.plan.operations) > 1:  # transactions return no item
            deleted = write.old
        return self._load_written(deleted, "delete") if deleted is not None else None

    def _conflict(self, write: _WritePlan, error: DynamoxyError, stored: dict[str, Any] | None) -> DynamoxyError:
        """The specific error for a failed condition of a planned write."""
        name = self.model_class.__name__
        key, expected, mode = write.key, write.expected, write.mode
        version = self.schema.version_field
        stored_version = stored.get(version.attribute) if stored is not None and version is not None else None
        if mode == "create" and stored is not None:
            return ItemAlreadyExistsError(
                f"{name} already exists (see .key)", model=self.model_class, key=key, item=stored, cause=error
            )
        if mode == "update" and stored is None and not write.upsert:
            return ItemNotFoundError(f"{name} does not exist (see .key)", model=self.model_class, key=key)
        if version is not None and (mode in ("save", "update", "delete")) and (expected or mode == "save"):
            if stored is None and (expected or write.loaded):
                return VersionConflictError(
                    f"{name} was deleted after it was loaded ({f'version {expected}' if expected else 'no version'}; "
                    "see .key)",
                    model=self.model_class,
                    key=key,
                    expected_version=expected,
                    cause=error,
                )
            if stored is not None and stored_version != (expected or None):
                wanted = f"version {expected}" if expected else "no version"
                return VersionConflictError(
                    f"{name} changed since it was loaded: stored version {stored_version}, expected {wanted} "
                    "(see .key)",
                    model=self.model_class,
                    key=key,
                    expected_version=expected,
                    item=stored,
                    cause=error,
                )
        if isinstance(error, ConditionalCheckFailedError):
            error.key = key
        return error

    def _cancellation(self, write: _WritePlan, error: TransactionCanceledError) -> DynamoxyError:
        """The specific error for a canceled transaction of one planned write."""
        for reason in cancellation_reasons(error):
            if reason.code != "ConditionalCheckFailed" or reason.index >= len(write.plan.explain):
                continue
            explain = write.plan.explain[reason.index]
            specific = self.explain_failure(explain, reason.item, write, error)
            if specific is not None:
                return specific
        return error

    def explain_failure(
        self, explain: Any, item: dict[str, Any] | None, write: _WritePlan, cause: DynamoxyError
    ) -> DynamoxyError | None:
        """The specific error for one failed operation of a plan (``None`` when unknown)."""
        if explain is None:
            return None
        if explain[0] == "guard":
            _, spec, value = explain
            decoded = spec.codec.decode(value)
            return UniqueConstraintError(
                f"{self.model_class.__name__}.{spec.name}: the value is already used by another item (see .value)",
                model=self.model_class,
                field=spec.name,
                value=decoded,
                cause=cause,
            )
        message = f"{self.model_class.__name__}: the condition of {write.mode} failed (see .key)"
        if write.stale:
            message += (
                " (the unique values stored may have changed since the instance was loaded: refresh() it and retry)"
            )
        elif write.absent and item is not None:
            message += " (another writer stored the item after it was read: retry)"
        failed = ConditionalCheckFailedError(message)
        failed.item = item
        for name in ("code", "operation", "request_id", "status_code", "attempts", "throttles", "requests", "duration"):
            value = getattr(cause, name, None)
            if value is not None:
                setattr(failed, name, value)
        return self._conflict(write, failed, item)

    async def batch_put(self, items: Iterable[M]) -> None:
        """Writes many items (managed ``BatchWriteItem``: unconditional, not atomic).

        A key given more than once is written once, with the last item. Only the items that were
        written update their instances and run ``after_save`` hooks.

        Raises:
            TypeError: The model has unique attributes (use ``create``/``save`` or a transaction).
            ValueError: A loaded instance's key or immutable value changed.
            UnprocessedItemsError: Some writes stayed unprocessed after every retry round.
        """
        if self.schema.unique_fields:
            raise TypeError(
                f"{self.model_class.__name__} has unique attributes; batch writes cannot maintain them "
                "(use create/save or a transaction)"
            )
        # By key: DynamoDB refuses a request that writes one key twice, so the last item wins.
        prepared: dict[Any, tuple[M, dict[str, Any], dict[str, Any]]] = {}
        hooks = self.client.hooks
        for instance in items:
            if hooks.has("before_save", type(instance)):
                await hooks.run("before_save", instance, self._context("batch_put"))
            item, changes, _ = self._prepare(instance, "batch")
            identity = stored_identity(self.schema.key_values(item))
            prepared.pop(identity, None)
            prepared[identity] = (instance, item, changes)
        if not prepared:
            return
        left = await self._batch_write([("put", item) for _, item, _ in prepared.values()])
        done = [entry for identity, entry in prepared.items() if identity not in left]
        for instance, item, changes in done:
            self._apply(instance, changes, item)
        await self._run_hooks("after_save", [instance for instance, _, _ in done], "batch_put", left)

    async def batch_delete(self, targets: Iterable[M | Mapping[str, Any] | Any]) -> None:
        """Deletes many items by instance or key (managed ``BatchWriteItem``; not atomic).

        Raises:
            TypeError: The model has unique attributes.
            UnprocessedItemsError: Some deletes stayed unprocessed after every retry round.
        """
        if self.schema.unique_fields:
            raise TypeError(f"{self.model_class.__name__} has unique attributes; delete items one by one")
        targets = list(targets)
        hooks = self.client.hooks
        for target in targets:
            if self._own(target) is not None and hooks.has("before_delete", type(target)):
                await hooks.run("before_delete", target, self._context("batch_delete"))
        keys = {stored_identity(key): key for key in (self._target_key(target) for target in targets)}
        if not keys:
            return
        left = await self._batch_write([("delete", key) for key in keys.values()])
        done = [
            target
            for target in targets
            if isinstance(target, self.model_class) and stored_identity(self._target_key(target)) not in left
        ]
        for target in done:
            self._apply(target, {}, None)
        await self._run_hooks("after_delete", done, "batch_delete", left)

    async def _batch_write(self, requests: list[tuple[str, dict[str, Any]]]) -> dict[Any, tuple[str, dict[str, Any]]]:
        """Sends a managed ``BatchWriteItem``; returns the unprocessed requests by key identity."""
        output = await self.client.call(
            "batch_write",
            "BatchWriteItem",
            table=self.table_name,
            model=self.model_class,
            requests=[(self.table_name, kind, item) for kind, item in requests],
        )
        return {stored_identity(self.schema.key_values(item)): (kind, item) for _, kind, item in output["unprocessed"]}

    async def _run_hooks(
        self, point: HookPoint, instances: list[M], operation: str, left: dict[Any, tuple[str, dict[str, Any]]]
    ) -> None:
        """Runs ``after_*`` hooks of written instances, then raises what failed.

        Raises:
            UnprocessedItemsError: Some requests stayed unprocessed (``left``).
            Exception: The first hook that raised (after every hook ran).
        """
        hooks = self.client.hooks
        errors: list[Exception] = []
        for instance in instances:
            if hooks.has(point, type(instance)):
                try:
                    await hooks.run(point, instance, self._context(operation))
                except Exception as error:  # noqa: BLE001 - raised once every hook ran
                    errors.append(error)
        if left:
            raise UnprocessedItemsError(
                f"{len(left)} writes of {self.table_name} stayed unprocessed (throttling)",
                unprocessed=list(left.values()),
            ) from (errors[0] if errors else None)
        if errors:
            raise errors[0]

    # ---- table ----------------------------------------------------------------------------

    async def create_table(self, **options: Any) -> None:
        """Creates this model's table (see ``Client.create_tables`` for options)."""
        await self.client.create_tables(self.model_class, **options)


def projects_attribute(schema: ModelSchema, spec: IndexSpec, attribute: str) -> bool:
    """Whether the index ``spec`` as declared holds ``attribute`` (keys always are)."""
    projection = spec.projection
    if projection.type == "ALL" or attribute in index_key_attributes(schema, spec):
        return True
    included = (schema.fields[name].attribute if name in schema.fields else name for name in projection.attributes)
    return projection.type == "INCLUDE" and attribute in included


def projects_every_field(schema: ModelSchema, spec: IndexSpec) -> bool:
    """Whether the entries of the index ``spec`` hold every attribute of the model."""
    return all(projects_attribute(schema, spec, field.attribute) for field in schema.fields.values())


def index_key_attributes(schema: ModelSchema, spec: IndexSpec | None) -> tuple[str, ...]:
    """The key attributes of an index entry (table key, then index key), or of a table item."""
    names = list(schema._key_attributes)
    if spec is not None:
        for field in (spec.partition_key, spec.sort_key):
            if field is not None and field.attribute not in names:
                names.append(field.attribute)
    return tuple(names)


def _reader_options(schema: ModelSchema, spec: IndexSpec | None, *, query: bool) -> dict[str, Any]:
    """Keyword arguments of ``Query``/``Scan`` that keep a read to the items of one model.

    * Models with an entity type are filtered on it. An index declared without the type
      attribute (``KEYS_ONLY``, ``INCLUDE``) may not hold it - tables created before dynamoxy
      projected it into such indexes - so entries without one pass, and the items are checked
      on the client once they are known (``hydrate()``).
    * Models with unique attributes and no entity type skip guard items, by their partition key
      prefix. Queries of the table, of local indexes and of global indexes keyed on the table's
      partition key need no filter: they select one partition key value (and DynamoDB refuses
      filters on the key attributes of what a query reads).
    * Queries of templated sort keys check the prefix on the client when ``where()`` replaces
      the ``begins_with`` condition and no entity type tells the models apart.
    * Reads of an index that does not hold every field never decode its entries into instances
      (``partial``; reads of such a local index ask for every attribute instead), and filters of
      such a global index are checked against what it holds (``projected``).

    The filters are dynamoxy's own (``auto_filter``): they rarely remove items, so a limit still
    bounds each request.
    """
    names = index_key_attributes(schema, spec)
    options: dict[str, Any] = {"key_names": names}
    if spec is not None and not projects_every_field(schema, spec):
        # Entries of such an index are not complete items: instances decoded from them would hold
        # defaults instead of the stored values (and writing them would store the defaults).
        options["partial" if spec.kind == "global" else "select_all"] = True
    type_attribute, entity = schema.type_attribute, schema.entity
    if type_attribute is not None and entity is not None:
        path = schema._path(type_attribute)
        if spec is None or projects_attribute(schema, spec, type_attribute):
            options["auto_filter"] = path == entity
        else:
            options["auto_filter"] = path.not_exists() | (path == entity)
            options["checks"] = (functools.partial(_has_entity, type_attribute, entity),)
    elif schema.unique_fields and schema.partition_key is not None:
        partition = schema.partition_key.attribute
        target_keys = (
            {spec.partition_key.attribute, *([spec.sort_key.attribute] if spec.sort_key else ())}
            if spec is not None
            else set(schema._key_attributes)
        )
        if not query or partition not in target_keys:
            options["auto_filter"] = ~schema._path(partition).begins_with(GUARD_PREFIX)
    sort = (spec.sort_key if spec is not None else schema.sort_key) if query else None
    template = sort.template if sort is not None else None
    typed = type_attribute is not None and entity is not None
    if template is not None and template.prefix and not typed:
        if template.is_constant:
            options["where_checks"] = (functools.partial(_has_value, sort.attribute, template.template),)  # type: ignore[union-attr]
        else:
            options["where_checks"] = (functools.partial(_has_prefix, sort.attribute, template.prefix),)  # type: ignore[union-attr]
    if spec is not None and spec.kind == "global" and spec.projection.type != "ALL":
        held = set(names)
        held.update(
            schema.fields[name].attribute if name in schema.fields else name for name in spec.projection.attributes
        )
        if type_attribute is not None:
            held.add(type_attribute)
        options["projected"] = frozenset(held)
    return options


def _is_stored_key_value(spec: FieldSpec, value: Any) -> bool:
    """Whether ``value`` is a key attribute's stored form already (``S``, ``N`` or ``B``)."""
    kind = spec.codec.key_type()
    if kind == "S":
        return isinstance(value, str)
    if kind == "N":
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return isinstance(value, bytes)


def _same_value(spec: FieldSpec, old: Any, new: Any) -> bool:
    """Whether two stored forms of a field decode to the same value."""
    decode = spec.codec.decode
    try:
        return bool(decode(old) == decode(new))
    except (ValueError, TypeError, ArithmeticError):
        return False


def _has_entity(attribute: str, entity: str, item: dict[str, Any]) -> bool:
    return item.get(attribute, entity) == entity


def _has_prefix(attribute: str, prefix: str, item: dict[str, Any]) -> bool:
    value = item.get(attribute)
    return isinstance(value, str) and value.startswith(prefix)


def _has_value(attribute: str, expected: str, item: dict[str, Any]) -> bool:
    return item.get(attribute) == expected
