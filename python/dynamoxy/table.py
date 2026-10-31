"""Tables: dict-level item access and table management.

``client.table("users")`` gives a :class:`Table` that reads and writes plain dicts and manages
the table itself (create, indexes, TTL, tags, backups). Model tables are usually managed with
``client.create_tables(User, Order)`` / ``client.ensure_tables(...)``, which build a
:class:`TableDefinition` from the models.
"""

from __future__ import annotations

import dataclasses
import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dynamoxy._utils import seconds
from dynamoxy.errors import ResourceNotFoundError, UnprocessedItemsError
from dynamoxy.expressions import Condition, ExpressionBuilder, UpdateAction, as_condition
from dynamoxy.query import Query, Scan
from dynamoxy.results import Backup, ContinuousBackups, TableDescription, TimeToLive
from dynamoxy.schema import Projection, Throughput, schema_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime, timedelta

    from dynamoxy.client import Client
    from dynamoxy.expressions import Path

__all__ = ["Billing", "IndexDefinition", "Table", "TableDefinition", "definitions_for"]

type KeyAttribute = tuple[str, Literal["S", "N", "B"]]


@dataclass(frozen=True, slots=True)
class Billing:
    """How a table is billed: :meth:`on_demand` (default) or :meth:`provisioned`.

    Attributes:
        mode: ``"PAY_PER_REQUEST"`` or ``"PROVISIONED"``.
        throughput: Capacity of provisioned tables (and their global indexes).
    """

    mode: Literal["PAY_PER_REQUEST", "PROVISIONED"] = "PAY_PER_REQUEST"
    throughput: Throughput | None = None

    def __post_init__(self) -> None:
        if (self.mode == "PROVISIONED") != (self.throughput is not None):
            raise ValueError("provisioned billing needs throughput; on-demand billing must not have it")

    @classmethod
    def on_demand(cls) -> Billing:
        """Pay per request."""
        return cls()

    @classmethod
    def provisioned(cls, read: int, write: int) -> Billing:
        """Provisioned capacity (also used for global indexes without their own)."""
        return cls("PROVISIONED", Throughput(read, write))


@dataclass(frozen=True, slots=True)
class IndexDefinition:
    """A secondary index of a :class:`TableDefinition` (DynamoDB attribute names).

    Attributes:
        name: Index name.
        kind: ``"global"`` or ``"local"``.
        partition_key: ``(attribute, type)``; for local indexes the table's partition key.
        sort_key: ``(attribute, type)`` or ``None``.
        projection: Copied attributes (DynamoDB attribute names).
        throughput: Capacity, for provisioned tables.
    """

    name: str
    kind: Literal["global", "local"]
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None = None
    projection: Projection = field(default_factory=Projection.all)
    throughput: Throughput | None = None

    def native(self, billing: Billing) -> dict[str, Any]:
        """The native index specification."""
        spec: dict[str, Any] = {
            "index_name": self.name,
            "key_schema": (self.partition_key[0], self.sort_key[0] if self.sort_key else None),
            "projection": (self.projection.type, list(self.projection.attributes)),
        }
        if self.kind == "global" and billing.mode == "PROVISIONED":
            throughput = self.throughput or billing.throughput
            assert throughput is not None
            spec["provisioned_throughput"] = (throughput.read, throughput.write)
        return spec


@dataclass(frozen=True, slots=True)
class TableDefinition:
    """The shape of a table: keys, indexes and TTL attribute (DynamoDB attribute names).

    Attributes:
        name: Logical table name.
        partition_key: ``(attribute, type)``.
        sort_key: ``(attribute, type)`` or ``None``.
        indexes: Secondary indexes.
        ttl_attribute: The Time to Live attribute, if any.
        models: Models stored in the table.
    """

    name: str
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None = None
    indexes: tuple[IndexDefinition, ...] = ()
    ttl_attribute: str | None = None
    models: tuple[type, ...] = ()

    def attribute_definitions(self) -> list[tuple[str, str]]:
        """Types of every key attribute of the table and its indexes."""
        definitions: dict[str, str] = {}
        keys = [self.partition_key, self.sort_key]
        for index in self.indexes:
            keys += [index.partition_key, index.sort_key]
        for key in keys:
            if key is not None:
                definitions[key[0]] = key[1]
        return sorted(definitions.items())


def _key(spec: Any) -> KeyAttribute:
    kind = spec.codec.key_type()
    assert kind in ("S", "N", "B")
    return (spec.attribute, kind)


def _merge(table: str, current: Any, new: Any, what: str, models: Sequence[type]) -> Any:
    if current is not None and new is not None and current != new:
        names = ", ".join(model.__name__ for model in models)
        raise TypeError(f"models sharing table {table!r} ({names}) disagree about {what}: {current} vs {new}")
    return current if current is not None else new


def definitions_for(models: Iterable[type]) -> list[TableDefinition]:
    """Table definitions for models; models sharing a table are merged.

    Raises:
        TypeError: Models of one table disagree about keys, an index or the TTL attribute, or a
            model has no table.
    """
    by_table: dict[str, list[type]] = {}
    for model in models:
        schema = schema_of(model)
        if not schema.is_table or schema.table is None:
            raise TypeError(f"{model.__name__} has no table; declare it with class {model.__name__}(..., table='name')")
        by_table.setdefault(schema.table, []).append(model)
    definitions = []
    for table, members in by_table.items():
        partition: KeyAttribute | None = None
        sort: KeyAttribute | None = None
        sort_seen = False
        indexes: dict[str, IndexDefinition] = {}
        ttl: str | None = None
        for model in members:
            schema = schema_of(model)
            assert schema.partition_key is not None
            partition = _merge(table, partition, _key(schema.partition_key), "the partition key", members)
            model_sort = _key(schema.sort_key) if schema.sort_key else None
            if sort_seen and model_sort != sort:
                _merge(table, sort or ("<none>", "S"), model_sort or ("<none>", "S"), "the sort key", members)
            sort, sort_seen = model_sort, True
            for spec in schema.indexes.values():
                projection = spec.projection
                if projection.type == "INCLUDE":
                    names = tuple(
                        schema.fields[name].attribute if name in schema.fields else name
                        for name in projection.attributes
                    )
                    projection = Projection("INCLUDE", names)
                if schema.type_attribute is not None and projection.type != "ALL":
                    # Reads of the model filter on its entity type, which DynamoDB can only see in
                    # the index entries when the index holds it.
                    held = projection.attributes
                    if schema.type_attribute not in held:
                        projection = Projection("INCLUDE", (*held, schema.type_attribute))
                definition = IndexDefinition(
                    name=spec.name,
                    kind=spec.kind,
                    partition_key=_key(spec.partition_key),
                    sort_key=_key(spec.sort_key) if spec.sort_key else None,
                    projection=projection,
                    throughput=spec.throughput,
                )
                existing = indexes.get(spec.name)
                if existing is not None and (existing.partition_key, existing.sort_key, existing.kind) != (
                    definition.partition_key,
                    definition.sort_key,
                    definition.kind,
                ):
                    _merge(table, existing, definition, f"index {spec.name!r}", members)
                if existing is not None and existing.projection != definition.projection:
                    # Merge INCLUDE projections; any ALL wins.
                    if "ALL" in (existing.projection.type, definition.projection.type):
                        projection = Projection.all()
                    else:
                        merged = tuple(dict.fromkeys(existing.projection.attributes + definition.projection.attributes))
                        projection = Projection("INCLUDE", merged) if merged else Projection.keys_only()
                    definition = dataclasses.replace(definition, projection=projection)
                indexes[spec.name] = definition
            if schema.ttl_field is not None:
                ttl = _merge(table, ttl, schema.ttl_field.attribute, "the TTL attribute", members)
        assert partition is not None
        definitions.append(
            TableDefinition(
                name=table,
                partition_key=partition,
                sort_key=sort,
                indexes=tuple(indexes.values()),
                ttl_attribute=ttl,
                models=tuple(members),
            )
        )
    return definitions


def _describe_key(key: Sequence[tuple[str, str] | None]) -> str:
    """A key's attributes and types for messages: ``sensor (N), seq (S)``."""
    return ", ".join(f"{name} ({kind})" for name, kind in (part for part in key if part is not None))


def _fields(definition: IndexDefinition) -> dict[str, Any]:
    return {
        "name": definition.name,
        "kind": definition.kind,
        "partition_key": definition.partition_key,
        "sort_key": definition.sort_key,
        "projection": definition.projection,
        "throughput": definition.throughput,
    }


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return item


class Table:
    """A table, by its logical name: dict-level items and table management.

    Example::

        table = client.table("events")
        await table.put({"pk": "a", "sk": 1, "payload": {"x": 1}})
        item = await table.get({"pk": "a", "sk": 1})
        async for item in table.query(F("pk") == "a"):
            ...
    """

    def __init__(self, client: Client, name: str) -> None:
        self.client = client
        self.name = name
        self.physical_name = client.table_name(name)

    def __repr__(self) -> str:
        return f"<dynamoxy.Table {self.physical_name!r}>"

    # ---- items ----------------------------------------------------------------------------

    async def get(
        self,
        key: Mapping[str, Any],
        *,
        consistent: bool = False,
        projection: Sequence[str | Path[Any]] | None = None,
    ) -> dict[str, Any] | None:
        """The item with this key, or ``None``."""
        builder = ExpressionBuilder()
        output = await self.client.call(
            "get_item",
            "GetItem",
            table=self.physical_name,
            table_name=self.physical_name,
            key=dict(key),
            consistent_read=consistent,
            projection_expression=builder.projection(projection or ()),
            expression_attribute_names=builder.names or None,
        )
        return output["item"]

    async def put(
        self, item: Mapping[str, Any], *, condition: Condition | bool | None = None, return_old: bool = False
    ) -> dict[str, Any] | None:
        """Writes an item (replacing any item with the same key).

        Returns:
            The previous item with ``return_old=True``, else ``None``.

        Raises:
            ConditionalCheckFailedError: ``condition`` did not hold (``.item`` is the stored item).
        """
        builder = ExpressionBuilder()
        output = await self.client.call(
            "put_item",
            "PutItem",
            table=self.physical_name,
            table_name=self.physical_name,
            item=dict(item),
            condition_expression=builder.condition(as_condition(condition)),
            return_values="ALL_OLD" if return_old else "NONE",
            return_values_on_condition_check_failure="ALL_OLD",
            **builder.arguments(),
        )
        return output["attributes"]

    async def update(
        self,
        key: Mapping[str, Any],
        *actions: UpdateAction,
        condition: Condition | bool | None = None,
        return_values: Literal["NONE", "ALL_OLD", "UPDATED_OLD", "ALL_NEW", "UPDATED_NEW"] = "ALL_NEW",
    ) -> dict[str, Any] | None:
        """Updates (or creates) the item with ``key``: ``table.update(key, F("n").add(1))``.

        Returns:
            The attributes selected by ``return_values``.
        """
        builder = ExpressionBuilder()
        update = builder.update(actions)
        output = await self.client.call(
            "update_item",
            "UpdateItem",
            table=self.physical_name,
            table_name=self.physical_name,
            key=dict(key),
            update_expression=update,
            condition_expression=builder.condition(as_condition(condition)),
            return_values=return_values,
            return_values_on_condition_check_failure="ALL_OLD",
            **builder.arguments(),
        )
        return output["attributes"]

    async def delete(
        self, key: Mapping[str, Any], *, condition: Condition | bool | None = None, return_old: bool = False
    ) -> dict[str, Any] | None:
        """Deletes the item with ``key`` (no error if it does not exist).

        Returns:
            The deleted item with ``return_old=True``, else ``None``.
        """
        builder = ExpressionBuilder()
        output = await self.client.call(
            "delete_item",
            "DeleteItem",
            table=self.physical_name,
            table_name=self.physical_name,
            key=dict(key),
            condition_expression=builder.condition(as_condition(condition)),
            return_values="ALL_OLD" if return_old else "NONE",
            return_values_on_condition_check_failure="ALL_OLD",
            **builder.arguments(),
        )
        return output["attributes"]

    def query(
        self, *key_condition: Condition | bool, index: str | None = None, consistent: bool = False
    ) -> Query[dict[str, Any]]:
        """A query: partition key equality, optionally a sort key condition.

        ``consistent=True`` reads with strong consistency; DynamoDB refuses it on global
        secondary indexes (``ValidationError``).

        Example::

            table.query(F("pk") == "USER#1", F("sk").begins_with("ORDER#")).filter(F("total") > 100)
        """
        conditions = [as_condition(part) for part in key_condition]
        if not conditions:
            raise ValueError("a query needs a key condition such as F('pk') == 'value'")
        combined = conditions[0]
        for part in conditions[1:]:
            assert part is not None
            combined = combined & part if combined is not None else part
        assert combined is not None
        return Query(
            self.client,
            self.physical_name,
            decode=_identity,
            key_condition=combined,
            index=index,
            consistent=consistent,
            key_names=functools.partial(self.client._key_attributes, self.physical_name, index),
        )

    def scan(self, *, index: str | None = None, consistent: bool = False) -> Scan[dict[str, Any]]:
        """A scan of every item (``.filter()``, ``.segments()`` for parallel scans).

        ``consistent=True`` reads with strong consistency; DynamoDB refuses it on global
        secondary indexes (``ValidationError``).
        """
        return Scan(
            self.client,
            self.physical_name,
            decode=_identity,
            index=index,
            consistent=consistent,
            key_names=functools.partial(self.client._key_attributes, self.physical_name, index),
        )

    async def batch_get(
        self, keys: Iterable[Mapping[str, Any]], *, consistent: bool = False, projection: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Reads any number of items by key (managed ``BatchGetItem``); missing keys are skipped.

        Raises:
            UnprocessedItemsError: Some keys stayed unprocessed after every retry round.
        """
        keys = [dict(key) for key in keys]
        if not keys:
            return []
        builder = ExpressionBuilder()
        request: dict[str, Any] = {"keys": keys, "consistent_read": consistent}
        projection_expression = builder.projection(projection or ())
        if projection_expression:
            request["projection_expression"] = projection_expression
            request["expression_attribute_names"] = builder.names
        output = await self.client.call(
            "batch_get", "BatchGetItem", table=self.physical_name, request_items={self.physical_name: request}
        )
        unprocessed = output["unprocessed_keys"].get(self.physical_name)
        if unprocessed and unprocessed["keys"]:
            raise UnprocessedItemsError(
                f"{len(unprocessed['keys'])} keys of {self.physical_name} stayed unprocessed (throttling)",
                unprocessed=[("get", key) for key in unprocessed["keys"]],
            )
        return list(output["items"].get(self.physical_name, []))

    async def batch_write(
        self, *, puts: Iterable[Mapping[str, Any]] = (), deletes: Iterable[Mapping[str, Any]] = ()
    ) -> None:
        """Writes any number of items and deletes (managed ``BatchWriteItem``; not atomic).

        Raises:
            UnprocessedItemsError: Some writes stayed unprocessed after every retry round.
        """
        requests = [(self.physical_name, "put", dict(item)) for item in puts]
        requests += [(self.physical_name, "delete", dict(key)) for key in deletes]
        if not requests:
            return
        output = await self.client.call("batch_write", "BatchWriteItem", table=self.physical_name, requests=requests)
        if output["unprocessed"]:
            raise UnprocessedItemsError(
                f"{len(output['unprocessed'])} writes stayed unprocessed (throttling)",
                unprocessed=[(kind, item) for _, kind, item in output["unprocessed"]],
            )

    # ---- management -----------------------------------------------------------------------

    async def describe(self) -> TableDescription:
        """The table's description.

        Raises:
            ResourceNotFoundError: The table does not exist.
        """
        output = await self.client.call(
            "describe_table", "DescribeTable", table=self.physical_name, table_name=self.physical_name
        )
        return TableDescription.from_core(output)

    async def exists(self) -> bool:
        """Whether the table exists."""
        try:
            await self.describe()
        except ResourceNotFoundError:
            return False
        return True

    async def create(
        self,
        *,
        partition_key: KeyAttribute,
        sort_key: KeyAttribute | None = None,
        indexes: Sequence[IndexDefinition] = (),
        billing: Billing | None = None,
        stream: str | None = None,
        ttl_attribute: str | None = None,
        table_class: Literal["STANDARD", "STANDARD_INFREQUENT_ACCESS"] | None = None,
        deletion_protection: bool = False,
        tags: Mapping[str, str] | None = None,
        encryption_key: str | None = None,
        wait: bool = True,
    ) -> TableDescription:
        """Creates the table.

        Args:
            partition_key: ``(attribute, type)``, e.g. ``("pk", "S")``.
            sort_key: ``(attribute, type)``.
            indexes: Secondary indexes.
            billing: Billing mode (default on-demand).
            stream: Stream view type (``"NEW_AND_OLD_IMAGES"``, ``"NEW_IMAGE"``, ``"OLD_IMAGE"``, ``"KEYS_ONLY"``).
            ttl_attribute: Enable Time to Live on this attribute.
            table_class: Storage class.
            deletion_protection: Turn on deletion protection.
            tags: Tags.
            encryption_key: A customer managed KMS key (id, ARN or alias) for encryption at rest.
            wait: Wait until the table is active. A table with a TTL attribute is waited for
                anyway, since DynamoDB enables Time to Live on active tables only.

        Raises:
            ResourceInUseError: The table exists already.
        """
        definition = TableDefinition(
            name=self.name,
            partition_key=partition_key,
            sort_key=sort_key,
            indexes=tuple(indexes),
            ttl_attribute=ttl_attribute,
        )
        return await self.create_from(
            definition,
            billing=billing,
            stream=stream,
            table_class=table_class,
            deletion_protection=deletion_protection,
            tags=tags,
            encryption_key=encryption_key,
            wait=wait,
        )

    async def create_from(
        self,
        definition: TableDefinition,
        *,
        billing: Billing | None = None,
        stream: str | None = None,
        table_class: str | None = None,
        deletion_protection: bool = False,
        tags: Mapping[str, str] | None = None,
        encryption_key: str | None = None,
        wait: bool = True,
    ) -> TableDescription:
        """Creates the table from a :class:`TableDefinition` (see :meth:`create`)."""
        billing = billing or Billing()
        params: dict[str, Any] = {
            "table_name": self.physical_name,
            "attribute_definitions": definition.attribute_definitions(),
            "key_schema": (definition.partition_key[0], definition.sort_key[0] if definition.sort_key else None),
            "global_secondary_indexes": [
                index.native(billing) for index in definition.indexes if index.kind == "global"
            ],
            "local_secondary_indexes": [index.native(billing) for index in definition.indexes if index.kind == "local"],
            "billing_mode": billing.mode,
            "deletion_protection_enabled": deletion_protection,
            "tags": dict(tags or {}),
        }
        if billing.throughput is not None:
            params["provisioned_throughput"] = (billing.throughput.read, billing.throughput.write)
        if stream is not None:
            params["stream_specification"] = (True, stream)
        if table_class is not None:
            params["table_class"] = table_class
        if encryption_key is not None:
            params["sse_specification"] = (True, "KMS", encryption_key)
        output = await self.client.call("create_table", "CreateTable", table=self.physical_name, **params)
        description = TableDescription.from_core(output)
        if wait or definition.ttl_attribute is not None:  # Time to Live needs an active table
            description = await self.wait_until_active()
        if definition.ttl_attribute is not None:
            await self.enable_time_to_live(definition.ttl_attribute)
        return description

    async def sync_with(self, definition: TableDefinition, *, wait: bool = True) -> bool:
        """Adds missing global secondary indexes and enables TTL to match ``definition``.

        DynamoDB changes a table one step at a time: each step waits until the table and its
        indexes are active, when an earlier step (of this call with ``wait=False``, or of an
        earlier call whose waiter gave up during a long backfill) is still in progress. With
        ``wait=False``, only the last step is not waited for.

        Returns:
            Whether anything changed.

        Raises:
            TypeError: The table's keys, the key of one of its indexes or its TTL attribute differ
                from the definition (none of them can be changed in place).
        """
        description = await self.describe()
        types = description.attribute_types

        def typed(name: str | None) -> tuple[str, str] | None:
            return None if name is None else (name, types.get(name, "?"))

        keys = (typed(description.partition_key), typed(description.sort_key))
        expected = (definition.partition_key, definition.sort_key)
        if keys != expected:
            raise TypeError(
                f"table {self.physical_name} has the key {_describe_key(keys)}, the models expect "
                f"{_describe_key(expected)}; a table's key cannot change (create a new table and migrate)"
            )
        changed = False
        billing = (
            Billing()
            if description.billing_mode == "PAY_PER_REQUEST"
            else Billing.provisioned(description.read_capacity or 1, description.write_capacity or 1)
        )
        missing: list[IndexDefinition] = []
        for index in definition.indexes:
            existing = description.indexes.get(index.name)
            if existing is not None:
                found = (existing.kind, typed(existing.partition_key), typed(existing.sort_key))
                wanted = (index.kind, index.partition_key, index.sort_key)
                if found != wanted:
                    raise TypeError(
                        f"table {self.physical_name} has the {found[0]} index {index.name!r} with the key "
                        f"{_describe_key(found[1:])}, the models expect a {wanted[0]} index with "
                        f"{_describe_key(wanted[1:])}; an index's key cannot change: give the new index another "
                        "name (and delete the old one with delete_index())"
                    )
                continue
            if index.kind == "local":
                raise TypeError(
                    f"table {self.physical_name} lacks the local index {index.name!r}; local indexes can only "
                    "be created with the table"
                )
            missing.append(index)
        if definition.ttl_attribute is not None:
            ttl = await self.time_to_live()
            if not ttl.enabled:
                # Before the indexes, so that it does not wait for their backfills.
                if description.status != "ACTIVE":
                    description = await self.wait_until_active()
                await self.enable_time_to_live(definition.ttl_attribute)
                changed = True
            elif ttl.attribute != definition.ttl_attribute:
                raise TypeError(
                    f"table {self.physical_name} has Time to Live on {ttl.attribute!r}, the models expect "
                    f"{definition.ttl_attribute!r}: a table has one TTL attribute; disable it first "
                    "(table.disable_time_to_live(); DynamoDB takes up to an hour) or keep the attribute"
                )
        in_progress = not description.active
        for index in missing:
            if in_progress:  # DynamoDB adds one index at a time
                await self.wait_until_active()
            await self.add_index(index, billing=billing, wait=wait)
            in_progress = not wait
            changed = True
        return changed

    async def delete_table(self, *, wait: bool = True, missing_ok: bool = False) -> None:
        """Deletes the table.

        Raises:
            ResourceNotFoundError: The table does not exist (unless ``missing_ok``).
        """
        try:
            await self.client.call(
                "delete_table", "DeleteTable", table=self.physical_name, table_name=self.physical_name
            )
        except ResourceNotFoundError:
            if not missing_ok:
                raise
            return
        if wait:
            await self.wait_until_deleted()

    async def wait_until_active(self, *, timeout: float | timedelta = 300.0) -> TableDescription:
        """Waits until the table and its global indexes are ``ACTIVE`` (``timeout`` in seconds)."""
        limit = seconds(timeout, "timeout", allow_zero=True)
        core = await self.client._core()
        raw = await core.wait_for_table(self.physical_name, exists=True, timeout=limit)
        assert raw is not None
        return TableDescription.from_core(raw)

    async def wait_until_deleted(self, *, timeout: float | timedelta = 300.0) -> None:
        """Waits until the table no longer exists (``timeout`` in seconds)."""
        limit = seconds(timeout, "timeout", allow_zero=True)
        core = await self.client._core()
        await core.wait_for_table(self.physical_name, exists=False, timeout=limit)

    async def update_settings(
        self,
        *,
        billing: Billing | None = None,
        stream: str | Literal[False] | None = None,
        deletion_protection: bool | None = None,
        table_class: Literal["STANDARD", "STANDARD_INFREQUENT_ACCESS"] | None = None,
        wait: bool = True,
    ) -> TableDescription:
        """Changes table settings (``stream=False`` disables the stream).

        A switch to provisioned billing gives every global index the table's capacity, since
        DynamoDB needs capacity for each of them: change an index's afterwards with
        ``client.call("update_table", "UpdateTable", ...)`` and a ``{"update": ...}`` index change.
        """
        params: dict[str, Any] = {"table_name": self.physical_name}
        if billing is not None:
            params["billing_mode"] = billing.mode
            if billing.throughput is not None:
                throughput = (billing.throughput.read, billing.throughput.write)
                params["provisioned_throughput"] = throughput
                current = await self.describe()
                if current.billing_mode != "PROVISIONED":
                    params["global_secondary_index_updates"] = [
                        {"update": {"index_name": index.name, "provisioned_throughput": throughput}}
                        for index in current.indexes.values()
                        if index.kind == "global"
                    ]
        if stream is False:
            params["stream_specification"] = (False, None)
        elif stream is not None:
            params["stream_specification"] = (True, stream)
        if deletion_protection is not None:
            params["deletion_protection_enabled"] = deletion_protection
        if table_class is not None:
            params["table_class"] = table_class
        output = await self.client.call("update_table", "UpdateTable", table=self.physical_name, **params)
        return await self.wait_until_active() if wait else TableDescription.from_core(output)

    async def add_index(
        self, index: IndexDefinition, *, billing: Billing | None = None, wait: bool = True
    ) -> TableDescription:
        """Adds a global secondary index (DynamoDB backfills it from existing items).

        Without ``billing``, the index of a provisioned table gets the table's capacity (unless the
        definition gives its own), since DynamoDB refuses an index without capacity there.
        """
        if index.kind != "global":
            raise ValueError("only global secondary indexes can be added to an existing table")
        if billing is None and index.throughput is None:
            current = await self.describe()
            if current.billing_mode == "PROVISIONED":
                billing = Billing.provisioned(current.read_capacity or 1, current.write_capacity or 1)
        attributes = {index.partition_key[0]: index.partition_key[1]}
        if index.sort_key is not None:
            attributes[index.sort_key[0]] = index.sort_key[1]
        output = await self.client.call(
            "update_table",
            "UpdateTable",
            table=self.physical_name,
            table_name=self.physical_name,
            attribute_definitions=sorted(attributes.items()),
            global_secondary_index_updates=[{"create": index.native(billing or Billing())}],
        )
        return await self.wait_until_active() if wait else TableDescription.from_core(output)

    async def delete_index(self, name: str, *, wait: bool = True) -> TableDescription:
        """Deletes a global secondary index."""
        output = await self.client.call(
            "update_table",
            "UpdateTable",
            table=self.physical_name,
            table_name=self.physical_name,
            global_secondary_index_updates=[{"delete": name}],
        )
        return await self.wait_until_active() if wait else TableDescription.from_core(output)

    async def time_to_live(self) -> TimeToLive:
        """The Time to Live state."""
        status, attribute = await self.client.call(
            "describe_time_to_live", "DescribeTimeToLive", table=self.physical_name, table_name=self.physical_name
        )
        return TimeToLive(status=status, attribute=attribute)

    async def enable_time_to_live(self, attribute: str) -> TimeToLive:
        """Enables Time to Live on ``attribute`` (epoch seconds); a no-op when already enabled on it.

        Raises:
            ValidationError: TTL is enabled on another attribute.
        """
        current = await self.time_to_live()
        if current.enabled:
            if current.attribute == attribute:
                return current
            from dynamoxy.errors import validation_error

            enabled = current.attribute
            raise validation_error(
                f"TTL of {self.physical_name} is enabled on {enabled!r}; disable it before using {attribute!r}"
            )
        status, name = await self.client.call(
            "update_time_to_live",
            "UpdateTimeToLive",
            table=self.physical_name,
            table_name=self.physical_name,
            enabled=True,
            attribute_name=attribute,
        )
        return TimeToLive(status=status, attribute=name)

    async def disable_time_to_live(self) -> TimeToLive:
        """Disables Time to Live (a no-op when disabled)."""
        current = await self.time_to_live()
        if not current.enabled or current.attribute is None:
            return current
        status, name = await self.client.call(
            "update_time_to_live",
            "UpdateTimeToLive",
            table=self.physical_name,
            table_name=self.physical_name,
            enabled=False,
            attribute_name=current.attribute,
        )
        return TimeToLive(status=status, attribute=name)

    async def _arn(self) -> str:
        description = await self.describe()
        if description.arn is None:
            raise ResourceNotFoundError(f"table {self.physical_name} has no ARN")
        return description.arn

    async def tags(self) -> dict[str, str]:
        """The table's tags."""
        return await self.client.call("list_tags_of_resource", "ListTagsOfResource", resource_arn=await self._arn())

    async def tag(self, tags: Mapping[str, str]) -> None:
        """Adds or replaces tags."""
        await self.client.call("tag_resource", "TagResource", resource_arn=await self._arn(), tags=dict(tags))

    async def untag(self, *keys: str) -> None:
        """Removes tags."""
        await self.client.call("untag_resource", "UntagResource", resource_arn=await self._arn(), tag_keys=list(keys))

    async def continuous_backups(self) -> ContinuousBackups:
        """Point-in-time recovery state."""
        output = await self.client.call(
            "describe_continuous_backups",
            "DescribeContinuousBackups",
            table=self.physical_name,
            table_name=self.physical_name,
        )
        return ContinuousBackups.from_core(output)

    async def enable_point_in_time_recovery(self, *, recovery_period_days: int | None = None) -> ContinuousBackups:
        """Enables point-in-time recovery (1-35 days of history, 35 by default)."""
        output = await self.client.call(
            "update_continuous_backups",
            "UpdateContinuousBackups",
            table=self.physical_name,
            table_name=self.physical_name,
            enabled=True,
            recovery_period_in_days=recovery_period_days,
        )
        return ContinuousBackups.from_core(output)

    async def disable_point_in_time_recovery(self) -> ContinuousBackups:
        """Disables point-in-time recovery."""
        output = await self.client.call(
            "update_continuous_backups",
            "UpdateContinuousBackups",
            table=self.physical_name,
            table_name=self.physical_name,
            enabled=False,
        )
        return ContinuousBackups.from_core(output)

    async def create_backup(self, name: str) -> Backup:
        """Creates an on-demand backup."""
        output = await self.client.call(
            "create_backup", "CreateBackup", table=self.physical_name, table_name=self.physical_name, backup_name=name
        )
        return Backup.from_core({**output, "table_name": self.physical_name})

    async def backups(self) -> list[Backup]:
        """The table's backups (all pages)."""
        backups: list[Backup] = []
        start: str | None = None
        while True:
            output = await self.client.call(
                "list_backups",
                "ListBackups",
                table=self.physical_name,
                table_name=self.physical_name,
                exclusive_start_backup_arn=start,
            )
            backups.extend(Backup.from_core(backup) for backup in output["backups"])
            start = output["last_evaluated_backup_arn"]
            if start is None:
                return backups

    async def delete_backup(self, backup_arn: str) -> Backup:
        """Deletes a backup."""
        output = await self.client.call("delete_backup", "DeleteBackup", backup_arn=backup_arn)
        return Backup.from_core(output)

    async def restore_backup(
        self, backup_arn: str, *, billing: Billing | None = None, wait: bool = True
    ) -> TableDescription:
        """Creates this table from a backup (the table must not exist).

        Raises:
            BackupError: The backup does not exist or the table exists.
        """
        params: dict[str, Any] = {"target_table_name": self.physical_name, "backup_arn": backup_arn}
        if billing is not None:
            params["billing_mode"] = billing.mode
            if billing.throughput is not None:
                params["provisioned_throughput"] = (billing.throughput.read, billing.throughput.write)
        output = await self.client.call(
            "restore_table_from_backup", "RestoreTableFromBackup", table=self.physical_name, **params
        )
        return await self.wait_until_active() if wait else TableDescription.from_core(output)

    async def restore_to(self, target: str, *, at: datetime | None = None, wait: bool = True) -> TableDescription:
        """Restores this table into a new table ``target`` (a logical name).

        The table is restored as it was at ``at`` (default: the latest restorable time). Needs
        point-in-time recovery.
        """
        target_name = self.client.table_name(target)
        output = await self.client.call(
            "restore_table_to_point_in_time",
            "RestoreTableToPointInTime",
            table=target_name,
            source_table_name=self.physical_name,
            target_table_name=target_name,
            use_latest_restorable_time=at is None,
            restore_date_time=at.timestamp() if at is not None else None,
        )
        if not wait:
            return TableDescription.from_core(output)
        return await Table(self.client, target).wait_until_active()
