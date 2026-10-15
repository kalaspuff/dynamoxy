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
from dynamoxy.schema import schema_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dynamoxy.client import Client
    from dynamoxy.repository import Repository

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
        repository = self._repositories.get(str(item.get(self.type_attribute)))
        if repository is None:  # filtered out by the query; defensive
            raise TypeError(f"item of unknown entity {item.get(self.type_attribute)!r} in a collection query")
        return repository._load(item)

    def _type_filter(self) -> Any:
        return F(self.type_attribute).is_in(list(self._repositories))

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
            query: Query[M] = Query(
                self.client,
                self.table_name,
                decode=self._decode,
                index=index,
                global_index=spec is not None and spec.kind == "global",
                key_condition=F(partition.attribute) == value,
                consistent=consistent,
            )
            return query.filter(self._type_filter())
        raise ValueError(f"collection query: cannot build the partition key from {sorted(key_values)}: {last_error}")

    def scan(self, *, index: str | None = None, consistent: bool = False) -> Scan[M]:
        """Every item of the collection's models in the table.

        ``consistent=True`` reads with strong consistency (not on global secondary indexes).
        """
        scan: Scan[M] = Scan(
            self.client,
            self.table_name,
            decode=self._decode,
            index=index,
            global_index=self._is_global(index),
            consistent=consistent,
        )
        return scan.filter(self._type_filter())

    def _is_global(self, index: str | None) -> bool:
        """Whether the models declare ``index`` as a global secondary index."""
        if index is not None:
            for repository in self._repositories.values():
                spec = repository.schema.indexes.get(index)
                if spec is not None:
                    return spec.kind == "global"
        return False
