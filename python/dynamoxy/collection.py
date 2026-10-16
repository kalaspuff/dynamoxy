"""Collections: queries over several models that share a table (single-table design).

In single-table design one partition holds items of several entity types (a customer, their
orders and invoices). A collection reads them in one query and decodes each item into its model
by the type attribute (``_type``, set by ``entity=``)::

    customer_view = client.collection(Customer, Order, Invoice)
    for item in await customer_view.query(pk="CUSTOMER#c1").all():
        match item:
            case Customer():
                ...
            case Order():
                ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dynamoxy.expressions import F
from dynamoxy.query import Query, Scan
from dynamoxy.repository import index_key_attributes, projects_attribute, projects_every_field
from dynamoxy.schema import schema_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dynamoxy.client import Client
    from dynamoxy.expressions import Condition
    from dynamoxy.repository import Repository
    from dynamoxy.schema import IndexSpec, ModelSchema

__all__ = ["Collection"]


class Collection[M]:
    """Several models of one table, read together (see the module documentation).

    Raises:
        TypeError: The models do not share a table or lack an entity type.
    """

    def __init__(self, client: Client, models: Sequence[type[M]]) -> None:
        if not models:
            raise TypeError("a collection needs at least one model")
        schemas = [schema_of(model) for model in models]
        tables = {schema.table for schema in schemas}
        if len(tables) != 1 or None in tables:
            raise TypeError(f"the models of a collection must share one table, got {sorted(map(str, tables))}")
        attributes = {schema.type_attribute for schema in schemas}
        if len(attributes) != 1 or None in attributes:
            raise TypeError("every model of a collection needs entity=... and the same type_attribute")
        self.client = client
        self.models = tuple(models)
        self.type_attribute: str = next(iter(attributes))  # pyrefly: ignore[bad-assignment]
        self._repositories: dict[str, Repository[Any]] = {}
        for model, schema in zip(models, schemas, strict=True):
            assert schema.entity is not None
            self._repositories[schema.entity] = client.repository(model)
        self._first = self._repositories[next(iter(self._repositories))]
        self.table_name = self._first.table_name

    def _decode(self, item: dict[str, Any]) -> M:
        entity = item.get(self.type_attribute)
        repository = self._repositories.get(str(entity))
        if repository is None:
            if entity is None:
                raise TypeError(
                    f"an item read through this collection has no {self.type_attribute!r} attribute, so its entity "
                    "is unknown: read the table or an index that holds the type attribute (Projection.all(), or "
                    "an index created by dynamoxy 0.3 or later, which projects it)"
                )
            raise TypeError(f"item of unknown entity {entity!r} in a collection query")  # filtered out; defensive
        return repository._load(item)

    def _type_filter(self, schema: ModelSchema, spec: IndexSpec | None) -> Condition:
        """The entities of the collection; an index that may not hold the type attribute keeps entries without one."""
        path = F(self.type_attribute)
        known = path.is_in(list(self._repositories))
        if spec is None or projects_attribute(schema, spec, self.type_attribute):
            return known
        return path.not_exists() | known

    def query(self, *, index: str | None = None, consistent: bool = False, **key_values: Any) -> Query[M]:
        """Every item of the collection's models in one partition.

        Give the partition key (``pk=...``) or the fields of a model's key template.
        ``consistent=True`` reads with strong consistency (not on global secondary indexes).
        """
        last_error: Exception | None = None
        for repository in self._repositories.values():
            schema = repository.schema
            spec = schema.indexes.get(index) if index is not None else None
            if index is not None and spec is None:
                continue
            partition = spec.partition_key if spec is not None else schema.partition_key
            assert partition is not None
            remaining = dict(key_values)
            used: set[str] = set()
            try:
                value = schema._key_value(partition, remaining, [], used)
            except ValueError as error:
                last_error = error
                continue
            leftover = set(remaining) - used
            if leftover:
                raise ValueError(f"collection query: unexpected arguments {', '.join(sorted(leftover))}")
            return Query(
                self.client,
                self.table_name,
                decode=self._decode,
                index=index,
                global_index=spec is not None and spec.kind == "global",
                key_condition=F(partition.attribute) == value,
                consistent=consistent,
                key_names=index_key_attributes(schema, spec),
                auto_filter=self._type_filter(schema, spec),
                **self._completeness(spec),
            )
        raise ValueError(f"collection query: cannot build the partition key from {sorted(key_values)}: {last_error}")

    def scan(self, *, index: str | None = None, consistent: bool = False) -> Scan[M]:
        """Every item of the collection's models in the table.

        ``consistent=True`` reads with strong consistency (not on global secondary indexes).
        """
        schema, spec = self._index_spec(index)
        return Scan(
            self.client,
            self.table_name,
            decode=self._decode,
            index=index,
            global_index=spec is not None and spec.kind == "global",
            consistent=consistent,
            key_names=index_key_attributes(schema, spec),
            auto_filter=self._type_filter(schema, spec),
            **self._completeness(spec),
        )

    def _completeness(self, spec: IndexSpec | None) -> dict[str, Any]:
        """How reads of an index get complete items of every model of the collection."""
        if spec is None:
            return {}
        complete = all(
            spec.name not in repository.schema.indexes
            or projects_every_field(repository.schema, repository.schema.indexes[spec.name])
            for repository in self._repositories.values()
        )
        if complete:
            return {}
        return {"partial": True} if spec.kind == "global" else {"select_all": True}

    def _index_spec(self, index: str | None) -> tuple[ModelSchema, IndexSpec | None]:
        """The declaration of ``index`` by one of the models, and that model's schema."""
        if index is not None:
            for repository in self._repositories.values():
                spec = repository.schema.indexes.get(index)
                if spec is not None:
                    return repository.schema, spec
        return self._first.schema, None
