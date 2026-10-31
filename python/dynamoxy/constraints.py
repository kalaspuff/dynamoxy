"""Unique constraints with guard items.

DynamoDB only guarantees uniqueness of primary keys. For a ``unique=True`` attribute, dynamoxy
stores one *guard item* per value, in the same table, keyed by the value::

    pk = "_unique#User#email#ada@example.com"   (sk = "_unique" when the table has a sort key)

Writes that set or change a unique value run in a transaction: the item itself plus
``Put guard (attribute_not_exists)`` for the new value and ``Delete guard`` for the old one. If
the value is taken, the transaction is canceled and dynamoxy raises
:class:`dynamoxy.UniqueConstraintError`. Deletes remove the guards in the same transaction.
A guard records its owner's key (``_owner``), and is only deleted when it is missing or owned by
the item written: a write that would delete another item's guard (two items hold the value,
which stored data allows) raises :class:`dynamoxy.UniqueConstraintError` too.

Requirements and costs: the table's partition key must be a string; such writes are
transactional (2x write capacity) and, when the instance was not loaded, need one consistent
read to learn the old values. Batch writes cannot keep guards consistent and are refused for
models with unique attributes.

Guards and items can disagree: items stored before a field became unique, or written by other
tools, have no guards, and guards can outlive their items. ``Repository.verify_unique()``
compares them (:class:`UniqueReport`) and ``Repository.repair_unique()`` fixes what it can
(:class:`UniqueRepair`).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from dynamoxy._utils import number_text, stored_identity

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dynamoxy.schema import FieldSpec, ModelSchema

__all__ = [
    "GUARD_PREFIX",
    "GUARD_TYPE",
    "DuplicateValue",
    "MissingGuard",
    "OrphanedGuard",
    "UniqueRepair",
    "UniqueReport",
    "guard_item",
    "guard_key",
    "is_guard",
]

#: Partition key prefix of guard items.
GUARD_PREFIX = "_unique#"
#: Value of the type attribute (``_type``) of guard items.
GUARD_TYPE = "_unique"


def _text(value: Any) -> str:
    """A stored unique value as guard key text.

    Numbers are written as DynamoDB stores them (``5``, not ``5.0`` or ``Decimal("5.00")``), so
    the text is the same whether it comes from a new value or from an item read back.
    """
    if isinstance(value, (bytes, bytearray)):
        return "b64:" + base64.b64encode(bytes(value)).decode()
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return number_text(value)
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
    """The guard item of a value, recording the key of the item that owns it.

    Raises:
        ValueError: The value is a collection (of an ``Any`` field), which has no canonical text.
    """
    if isinstance(stored_value, (set, frozenset, list, tuple, dict)):
        raise ValueError(
            f"{schema.model.__name__}.{field.name} is unique: only strings, numbers, binary values and booleans "
            f"can be unique, not a {type(stored_value).__name__}"
        )
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


# ---- verification -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissingGuard:
    """A unique value that one item holds and no guard item records.

    Attributes:
        field: The Python name of the unique field.
        value: The value (decoded; as stored when it does not decode).
        stored: The value as stored.
        key: The key of the item that holds it.
    """

    field: str
    value: Any
    stored: Any
    key: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrphanedGuard:
    """A guard item that is not owned by the item holding its value.

    Attributes:
        key: The key of the guard item.
        attribute: The stored name of the attribute the guard is for.
        field: The Python name of the unique field (``None``: the attribute is not unique).
        reason: Why it is orphaned: its owner is not stored (``"owner missing"``), holds another
            value (``"owner changed"``), the guard names no complete owner key (``"no owner"``),
            or the attribute is not unique in the model (``"not unique"``).
        owner: The owner key the guard records (``None``: none, or not a complete key).
        holder: The key of the one item that holds the value, which a repair makes the owner
            (``None``: no item holds it, and a repair deletes the guard).
        stored: The holder's value as stored (``None`` without a holder).
        owner_found: Whether the owner was stored, as an item of this model, at the scan.
        owner_value: The owner's value of the attribute at the scan (as stored; ``None`` when it
            holds none or was not found).
    """

    key: dict[str, Any]
    attribute: str
    field: str | None
    reason: Literal["owner missing", "owner changed", "no owner", "not unique"]
    owner: dict[str, Any] | None
    holder: dict[str, Any] | None = None
    stored: Any = None
    owner_found: bool = False
    owner_value: Any = None


@dataclass(frozen=True, slots=True)
class DuplicateValue:
    """A unique value that several items hold, which only a change of the data can resolve.

    Attributes:
        field: The Python name of the unique field.
        value: The value (decoded; as stored when it does not decode).
        keys: The keys of the items that hold it.
        owner: The key of the item among them that owns the value's guard (``None``: none does).
    """

    field: str
    value: Any
    keys: tuple[dict[str, Any], ...]
    owner: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class UniqueReport:
    """How the unique values of a model's items and its guard items agree (``verify_unique()``).

    Attributes:
        model: The model class.
        items: Items of the model scanned.
        guards: Guard items of the model scanned.
        missing: Values that one item holds without a guard (``repair_unique()`` creates it).
        orphaned: Guards that the item holding the value does not own (``repair_unique()``
            deletes them, or makes that item the owner).
        duplicates: Values that several items hold: change the data (``repair_unique()`` leaves
            them, and raises unless asked to skip them).
        invalid: Values that cannot be unique (collections stored in ``Any`` fields): change
            them.
    """

    model: type
    items: int
    guards: int
    missing: tuple[MissingGuard, ...] = ()
    orphaned: tuple[OrphanedGuard, ...] = ()
    duplicates: tuple[DuplicateValue, ...] = ()
    invalid: tuple[MissingGuard, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every unique value has its guard, and every guard its item."""
        return not (self.missing or self.orphaned or self.duplicates or self.invalid)


@dataclass(frozen=True, slots=True)
class UniqueRepair:
    """What ``repair_unique()`` did (or, with ``dry_run=True``, would do).

    Attributes:
        report: The report the repair worked from.
        dry_run: Whether nothing was written.
        created: Guards created for values without one.
        deleted: Orphaned guards deleted.
        reassigned: Guards whose owner became the item that holds the value.
        skipped: Fixes not applied because the data changed since the scan (their conditions
            failed): verify again.
        duplicates: Values that several items hold, left as they are.
    """

    report: UniqueReport
    dry_run: bool
    created: tuple[MissingGuard, ...] = ()
    deleted: tuple[OrphanedGuard, ...] = ()
    reassigned: tuple[OrphanedGuard, ...] = ()
    skipped: tuple[MissingGuard | OrphanedGuard, ...] = ()
    duplicates: tuple[DuplicateValue, ...] = ()


def _decoded(spec: FieldSpec, value: Any) -> Any:
    try:
        return spec.codec.decode(value)
    except Exception:  # noqa: BLE001 - a report shows what is stored when it does not decode
        return value


def _complete_key(schema: ModelSchema, owner: Any) -> dict[str, Any] | None:
    if not isinstance(owner, dict) or set(owner) != set(schema._key_attributes):
        return None
    return dict(owner)


def audit(schema: ModelSchema, scanned: Iterable[dict[str, Any]]) -> UniqueReport:
    """Compares the items of a model with its guard items, from a scan of its table.

    ``scanned`` holds every item of the table (with at least the key attributes, the type
    attribute, the unique attributes and the guards' ``_owner`` and ``_attribute``).
    """
    partition = schema.partition_key
    assert partition is not None
    entity = schema.entity or schema.model.__name__
    prefix = f"{GUARD_PREFIX}{entity}#"
    unique = {spec.attribute: spec for spec in schema.unique_fields}
    typed = schema.type_attribute is not None and schema.entity is not None
    items: dict[Any, dict[str, Any]] = {}  # model items by key identity
    guards: dict[Any, dict[str, Any]] = {}  # guards of the model by key identity
    for item in scanned:
        identity = stored_identity(schema.key_values(item))
        pk = item.get(partition.attribute)
        if isinstance(pk, str) and pk.startswith(GUARD_PREFIX):
            if pk.startswith(prefix):
                guards[identity] = item
        elif not typed or item.get(schema.type_attribute) == schema.entity:  # type: ignore[arg-type]
            items[identity] = item
    # Values held by items: (guard key identity) -> (spec, stored value, holder keys).
    held: dict[Any, tuple[FieldSpec, Any, list[dict[str, Any]]]] = {}
    invalid: list[MissingGuard] = []
    for item in items.values():
        key = schema.key_values(item)
        for attribute, spec in unique.items():
            value = item.get(attribute)
            if value is None:
                continue
            if isinstance(value, (set, frozenset, list, tuple, dict)):
                invalid.append(MissingGuard(spec.name, value, value, key))
                continue
            guard = stored_identity(guard_key(schema, spec, value))
            held.setdefault(guard, (spec, value, []))[2].append(key)
    missing: list[MissingGuard] = []
    duplicates: list[DuplicateValue] = []
    claimed: dict[Any, dict[str, Any] | None] = {}  # guard identity -> its single holder (None: several)
    for guard_identity, (spec, value, holders) in held.items():
        guard = guards.get(guard_identity)
        owner = _complete_key(schema, guard.get("_owner")) if guard is not None else None
        if len(holders) > 1:
            owners = [key for key in holders if owner is not None and stored_identity(key) == stored_identity(owner)]
            duplicates.append(
                DuplicateValue(spec.name, _decoded(spec, value), tuple(holders), owners[0] if owners else None)
            )
            claimed[guard_identity] = None
        elif guard is None:
            missing.append(MissingGuard(spec.name, _decoded(spec, value), value, holders[0]))
        else:
            claimed[guard_identity] = holders[0]
    orphaned: list[OrphanedGuard] = []
    for guard_identity, guard in guards.items():
        key = schema.key_values(guard)
        owner = _complete_key(schema, guard.get("_owner"))
        attribute = guard.get("_attribute")
        spec = unique.get(attribute) if isinstance(attribute, str) else None
        if spec is None and not isinstance(attribute, str):
            pk = guard[partition.attribute]
            spec = next((s for a, s in unique.items() if pk.startswith(f"{prefix}{a}#")), None)
            attribute = spec.attribute if spec is not None else pk[len(prefix) :].partition("#")[0]
        assert isinstance(attribute, str)
        owner_item = items.get(stored_identity(owner)) if owner is not None else None
        owner_state = {"owner_found": owner_item is not None, "owner_value": (owner_item or {}).get(attribute)}
        if spec is None:
            orphaned.append(OrphanedGuard(key, attribute, None, "not unique", owner, **owner_state))
            continue
        if guard_identity in claimed:
            holder = claimed[guard_identity]
            if holder is None or (owner is not None and stored_identity(holder) == stored_identity(owner)):
                continue  # owned by its holder, or a duplicate
            stored = items[stored_identity(holder)][attribute]
        else:
            holder = stored = None
        reason: Literal["owner missing", "owner changed", "no owner"]
        if owner is None:
            reason = "no owner"
        elif owner_item is None:
            reason = "owner missing"
        else:
            reason = "owner changed"
        orphaned.append(OrphanedGuard(key, attribute, spec.name, reason, owner, holder, stored, **owner_state))
    return UniqueReport(
        model=schema.model,
        items=len(items),
        guards=len(guards),
        missing=tuple(missing),
        orphaned=tuple(orphaned),
        duplicates=tuple(duplicates),
        invalid=tuple(invalid),
    )
