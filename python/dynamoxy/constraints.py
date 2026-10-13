"""Unique constraints with guard items.

DynamoDB only guarantees uniqueness of primary keys. For a ``unique=True`` attribute, dynamoxy
stores one *guard item* per value, in the same table, keyed by the value::

    pk = "_unique#User#email#ada@example.com"   (sk = "_unique" when the table has a sort key)

Writes that set or change a unique value run in a transaction: the item itself plus
``Put guard (attribute_not_exists)`` for the new value and ``Delete guard`` for the old one. If
the value is taken, the transaction is canceled and dynamoxy raises
:class:`dynamoxy.UniqueConstraintError`. Deletes remove the guards in the same transaction.

Requirements and costs: the table's partition key must be a string; such writes are
transactional (2x write capacity) and, when the instance was not loaded, need one consistent
read to learn the old values. Batch writes cannot keep guards consistent and are refused for
models with unique attributes.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dynamoxy.schema import FieldSpec, ModelSchema

__all__ = ["GUARD_PREFIX", "GUARD_TYPE", "guard_item", "guard_key", "is_guard"]

#: Partition key prefix of guard items.
GUARD_PREFIX = "_unique#"
#: Value of the type attribute (``_type``) of guard items.
GUARD_TYPE = "_unique"


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "b64:" + base64.b64encode(bytes(value)).decode()
    return str(value)


def guard_key(schema: ModelSchema, field: FieldSpec, stored_value: Any) -> dict[str, Any]:
    """The key of the guard item of ``stored_value`` (the encoded value of ``field``).

    Raises:
        TypeError: The table's keys cannot hold guard items (non-string partition key).
    """
    partition = schema.partition_key
    if partition is None or partition.codec.key_type() != "S":
        raise TypeError(
            f"{schema.model.__name__}: unique attributes need a table whose partition key is a string "
            f"({partition.attribute if partition else '?'} is not)"
        )
    entity = schema.entity or schema.model.__name__
    key: dict[str, Any] = {partition.attribute: f"{GUARD_PREFIX}{entity}#{field.attribute}#{_text(stored_value)}"}
    sort = schema.sort_key
    if sort is not None:
        kind = sort.codec.key_type()
        key[sort.attribute] = {"S": "_unique", "N": 0, "B": b"_unique"}[kind or "S"]
    return key


def guard_item(schema: ModelSchema, field: FieldSpec, stored_value: Any, owner: dict[str, Any]) -> dict[str, Any]:
    """The guard item of a value, recording the key of the item that owns it."""
    item = guard_key(schema, field, stored_value)
    item["_type"] = GUARD_TYPE
    item["_owner"] = dict(owner)
    item["_attribute"] = field.attribute
    return item


def is_guard(schema: ModelSchema, item: dict[str, Any]) -> bool:
    """Whether a stored item is a guard item."""
    partition = schema.partition_key
    value = item.get(partition.attribute) if partition is not None else None
    return isinstance(value, str) and value.startswith(GUARD_PREFIX)
