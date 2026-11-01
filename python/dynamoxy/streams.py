"""DynamoDB Streams records as changes of models.

A table's stream records every write as an ``INSERT``, ``MODIFY`` or ``REMOVE`` record with the
item's key and, depending on the stream's view type, its new and old images in DynamoDB's typed
JSON. :func:`changes` turns the records of a Lambda event into :class:`Change` objects that hold
model instances::

    from dynamoxy import streams


    def handler(event, context):
        try:
            for change in streams.changes(event, Customer, Order):
                match change.new:
                    case Customer():
                        ...
                    case Order():
                        ...
        except dynamoxy.StreamRecordError as error:
            return {"batchItemFailures": [{"itemIdentifier": error.sequence_number}]}
        return {"batchItemFailures": []}

Records of guard items (unique values, see :mod:`dynamoxy.constraints`) and of entities that were
not given are skipped. Each image is decoded into the model its type attribute (``_type``) names;
items without one into the model without an entity type. Records without images (``KEYS_ONLY``
streams, a ``REMOVE`` in a ``NEW_IMAGE`` stream) go to the model whose key templates their key
fits.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from dynamoxy._core import from_wire
from dynamoxy.constraints import GUARD_PREFIX
from dynamoxy.errors import ItemDecodeError, StreamRecordError, ValidationError
from dynamoxy.schema import schema_of

if TYPE_CHECKING:
    from dynamoxy.client import Client
    from dynamoxy.repository import Repository
    from dynamoxy.schema import ModelSchema

__all__ = ["Change", "EventName", "StreamEvent", "changes"]

#: The kind of write a record reports.
type EventName = Literal["INSERT", "MODIFY", "REMOVE"]
#: What :func:`changes` reads: a Lambda event (``{"Records": [...]}``, also the response of the
#: streams API's ``GetRecords``), records, or one record.
type StreamEvent = Mapping[str, Any] | Iterable[Mapping[str, Any]]

_EVENT_NAMES = frozenset(("INSERT", "MODIFY", "REMOVE"))
#: The principal of deletions by Time to Live.
_TTL_PRINCIPAL = "dynamodb.amazonaws.com"
_KEY_TYPES: dict[str, tuple[type, ...]] = {"S": (str,), "N": (int, Decimal), "B": (bytes,)}


@dataclass(frozen=True, slots=True)
class Change[M]:
    """One write of an item of a model, from a DynamoDB Streams record.

    Attributes:
        event_name: ``"INSERT"``, ``"MODIFY"`` or ``"REMOVE"``.
        model: The item's model.
        key: The item's primary key, as stored.
        new: The item after the write (``None`` for ``REMOVE``, and when the stream's view type
            leaves out new images: ``KEYS_ONLY``, ``OLD_IMAGE``).
        old: The item before the write (``None`` for ``INSERT``, and when the stream's view type
            leaves out old images: ``KEYS_ONLY``, ``NEW_IMAGE``).
        expired: Whether Time to Live deleted the item (a ``REMOVE`` by DynamoDB itself).
        sequence_number: The record's ``SequenceNumber`` (the item identifier of a Lambda
            function's ``batchItemFailures``).
        event_id: The record's ``eventID``.
        table: The table's name, from ``eventSourceARN``.
        created_at: When the write was made (``ApproximateCreationDateTime``), in UTC.
        view_type: The stream's view type (``"NEW_AND_OLD_IMAGES"``, ``"NEW_IMAGE"``,
            ``"OLD_IMAGE"`` or ``"KEYS_ONLY"``).
        record: The record as received.
    """

    event_name: EventName
    model: type[M]
    key: dict[str, Any]
    new: M | None
    old: M | None
    expired: bool = False
    sequence_number: str | None = None
    event_id: str | None = None
    table: str | None = None
    created_at: datetime | None = None
    view_type: str | None = None
    record: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


# The overloads give mypy the union of the models; with ``*models: type[M]`` alone it infers
# their common base class (pyright and pyrefly infer the union either way).
@overload
def changes[A](event: StreamEvent, a: type[A], /, *, client: Client | None = None) -> Iterator[Change[A]]: ...
@overload
def changes[A, B](
    event: StreamEvent, a: type[A], b: type[B], /, *, client: Client | None = None
) -> Iterator[Change[A | B]]: ...
@overload
def changes[A, B, C](
    event: StreamEvent, a: type[A], b: type[B], c: type[C], /, *, client: Client | None = None
) -> Iterator[Change[A | B | C]]: ...
@overload
def changes[A, B, C, D](
    event: StreamEvent, a: type[A], b: type[B], c: type[C], d: type[D], /, *, client: Client | None = None
) -> Iterator[Change[A | B | C | D]]: ...
@overload
def changes[A, B, C, D, E](
    event: StreamEvent,
    a: type[A],
    b: type[B],
    c: type[C],
    d: type[D],
    e: type[E],
    /,
    *,
    client: Client | None = None,
) -> Iterator[Change[A | B | C | D | E]]: ...
@overload
def changes[A, B, C, D, E, F](
    event: StreamEvent,
    a: type[A],
    b: type[B],
    c: type[C],
    d: type[D],
    e: type[E],
    f: type[F],
    /,
    *,
    client: Client | None = None,
) -> Iterator[Change[A | B | C | D | E | F]]: ...
@overload
def changes[M](event: StreamEvent, *models: type[M], client: Client | None = None) -> Iterator[Change[M]]: ...
def changes(event: StreamEvent, *models: type[Any], client: Client | None = None) -> Iterator[Change[Any]]:
    """The changes of models in DynamoDB Streams records, in the records' order.

    Records of guard items and of entities not in ``models`` are skipped (see the module
    documentation). The models and the event are checked when this is called; each record when
    the iterator reaches it, so the changes before a record that fails are processed first.

    Args:
        event: A Lambda event (``{"Records": [...]}``), a list of records, or one record.
        *models: The models to decode. Models of one table need an entity type each
            (``entity=...``), except one, which takes the items without a type attribute.
        client: Decode into instances bound to this client's repositories (``save()`` and
            ``update()`` on them write through it; ``after_load`` hooks run), and route records
            by their table: only records of the tables of ``models`` (by ``eventSourceARN``, with
            the client's table names) are read. Without it, instances are unbound and every
            record is read as one of ``models``.

    Raises:
        TypeError: No models, a model not stored in a table, models whose items cannot be told
            apart, or an event that is not a mapping or a list of records.
        StreamRecordError: (While iterating) a record is malformed, an image does not fit its
            model, or a record without images fits the keys of several models. Its
            ``sequence_number`` says which record.
    """
    router = _Router(models, client)
    return router.changes(_records(event))


def _records(event: StreamEvent) -> Iterable[Any]:
    """The records of an event."""
    if isinstance(event, Mapping):
        event = cast("Mapping[str, Any]", event)
        records = event.get("Records")
        if records is not None:
            if isinstance(records, (str, bytes, Mapping)) or not isinstance(records, Iterable):
                raise TypeError(f"the event's 'Records' is a {type(records).__name__}, not a list of records")
            return cast("Iterable[Any]", records)
        if "dynamodb" in event or "eventName" in event:
            return (event,)
        raise TypeError(
            "streams.changes() takes a Lambda event ({'Records': [...]}), a list of records or one record "
            "(with 'eventName' and 'dynamodb'); got a mapping without them"
        )
    if isinstance(event, (str, bytes)) or not isinstance(event, Iterable):
        raise TypeError(
            "streams.changes() takes a Lambda event ({'Records': [...]}), a list of records or one record, "
            f"not a {type(event).__name__}"
        )
    return event


class _Group:
    """The models of one table, and how to tell their items apart."""

    def __init__(self) -> None:
        self.models: list[tuple[type[Any], ModelSchema]] = []
        self.typed: dict[str, dict[str, type[Any]]] = {}  # type attribute -> entity -> model
        self.plain: type[Any] | None = None
        self.partitions: set[str] = set()

    def add(self, model: type[Any], schema: ModelSchema, *, routed: bool) -> None:
        if schema.type_attribute is not None and schema.entity is not None:
            entities = self.typed.setdefault(schema.type_attribute, {})
            other = entities.get(schema.entity)
            if other is not None:
                raise TypeError(
                    f"{other.__name__} and {model.__name__} store the same entity type, {schema.entity!r}, "
                    f"so streams.changes() cannot tell their items apart{_ROUTE_HINT if not routed else ''}"
                )
            entities[schema.entity] = model
        elif self.plain is not None:
            raise TypeError(
                f"{self.plain.__name__} and {model.__name__} have no entity type, so streams.changes() cannot "
                "tell their items apart: declare entity=... on the models that share a table"
                f"{_ROUTE_HINT if not routed else ''}"
            )
        else:
            self.plain = model
        if schema.partition_key is not None:
            self.partitions.add(schema.partition_key.attribute)
        self.models.append((model, schema))

    def is_guard(self, item: Mapping[str, Any]) -> bool:
        for attribute in self.partitions:
            value = item.get(attribute)
            if isinstance(value, str) and value.startswith(GUARD_PREFIX):
                return True
        return False

    def image_model(self, item: Mapping[str, Any]) -> type[Any] | None:
        """The model of an image, or ``None`` for an item of no model given."""
        if self.is_guard(item):
            return None
        other_entity = False
        for attribute, entities in self.typed.items():
            value = item.get(attribute)
            if value is None:
                continue
            model = entities.get(value) if isinstance(value, str) else None
            if model is not None:
                return model
            other_entity = True
        return None if other_entity else self.plain

    def key_models(self, key: Mapping[str, Any]) -> list[type[Any]]:
        """The models whose keys ``key`` fits (by attributes, types and key templates)."""
        if self.is_guard(key):
            return []
        return [model for model, schema in self.models if _key_fits(schema, key)]


_ROUTE_HINT = ", or pass client= to read the records of each table as its models"


def _key_fits(schema: ModelSchema, key: Mapping[str, Any]) -> bool:
    """Whether a stored key can be one of the model's: its attributes, types and key templates."""
    specs = schema.key_fields()
    if len(key) != len(specs):
        return False
    for spec in specs:
        value = key.get(spec.attribute)
        if value is None:
            return False
        template = spec.template
        if template is not None:
            if not isinstance(value, str):
                return False
            if template.is_constant:
                if value != template.template:
                    return False
            elif not value.startswith(template.prefix):
                return False
            continue
        kind = spec.codec.key_type()
        if kind is not None and not isinstance(value, _KEY_TYPES[kind]):
            return False
    return True


class _Router:
    """The models of each table, and the decoding of records into changes."""

    def __init__(self, models: tuple[type[Any], ...], client: Client | None) -> None:
        if not models:
            raise TypeError("streams.changes() needs at least one model: streams.changes(event, User)")
        self.client = client
        self.groups: dict[str | None, _Group] = {}
        self.repositories: dict[type[Any], Repository[Any]] = {}
        for model in dict.fromkeys(models):
            if not isinstance(model, type):
                raise TypeError(f"streams.changes() takes model classes, not {model!r}")
            schema = schema_of(model)
            if not schema.is_table or schema.table is None:
                raise TypeError(f"{model.__name__} is not stored in a table; declare table='...' on the class")
            table = client.table_name(schema.table) if client is not None else None
            self.groups.setdefault(table, _Group()).add(model, schema, routed=client is not None)
            if client is not None:
                self.repositories[model] = client.model(model)

    def changes(self, records: Iterable[Any]) -> Iterator[Change[Any]]:
        for record in records:
            change = self._change(record)
            if change is not None:
                yield change

    def _group(self, table: str | None, error: _RecordError) -> _Group | None:
        if self.client is None:
            return self.groups[None]
        if table is None:
            if len(self.groups) == 1:
                return next(iter(self.groups.values()))
            raise error(
                "it has no eventSourceARN, so its table is unknown, and the models given are stored in several "
                f"tables ({', '.join(sorted(str(name) for name in self.groups))})"
            )
        return self.groups.get(table)

    def _change(self, record: Any) -> Change[Any] | None:
        if not isinstance(record, Mapping):
            raise StreamRecordError(f"a stream record is a mapping, not a {type(record).__name__}")
        record = cast("Mapping[str, Any]", record)
        data = record.get("dynamodb")
        sequence_number = data.get("SequenceNumber") if isinstance(data, Mapping) else None
        event_id = record.get("eventID")
        event_name = record.get("eventName")
        error = _RecordError(sequence_number, event_id, event_name)
        source = record.get("eventSource")
        if source is not None and source != "aws:dynamodb":
            raise error(
                f"it is not a DynamoDB Streams record (eventSource is {source!r}); records of Kinesis Data "
                "Streams for DynamoDB are not supported"
            )
        if not isinstance(data, Mapping):
            raise error("it has no 'dynamodb' object")
        if event_name not in _EVENT_NAMES:
            raise error(f"its eventName is {event_name!r}, not 'INSERT', 'MODIFY' or 'REMOVE'")
        table = _table_of(record.get("eventSourceARN"))
        group = self._group(table, error)
        if group is None:
            return None  # a table none of the models is stored in

        keys = _image(data, "Keys", error)
        new_item = _image(data, "NewImage", error)
        old_item = _image(data, "OldImage", error)
        new_model = group.image_model(new_item) if new_item is not None else None
        old_model = group.image_model(old_item) if old_item is not None else None
        if new_item is not None or old_item is not None:
            model = new_model or old_model
            if model is None:
                return None  # a guard item, or an entity not asked for
        else:
            if keys is None:
                raise error("it has no Keys, NewImage or OldImage")
            candidates = group.key_models(keys)
            if not candidates:
                return None
            if len(candidates) > 1:
                raise error(
                    "it has no images, and its key fits several models: "
                    f"{', '.join(candidate.__name__ for candidate in candidates)}; give their keys templates "
                    "with distinct prefixes, or use a stream that holds images (NEW_AND_OLD_IMAGES)",
                    key=keys,
                )
            model = candidates[0]
        if keys is None:
            keys = schema_of(model).key_values(new_item if new_item is not None else old_item or {})
        user = record.get("userIdentity")
        expired = (
            event_name == "REMOVE"
            and isinstance(user, Mapping)
            and user.get("type") == "Service"
            and user.get("principalId") == _TTL_PRINCIPAL
        )
        return Change(
            event_name=cast("EventName", event_name),
            model=model,
            key=keys,
            new=self._decode(new_model, new_item, keys, "NewImage", error),
            old=self._decode(old_model, old_item, keys, "OldImage", error),
            expired=expired,
            sequence_number=sequence_number if isinstance(sequence_number, str) else None,
            event_id=event_id if isinstance(event_id, str) else None,
            table=table,
            created_at=_created_at(data.get("ApproximateCreationDateTime")),
            view_type=data.get("StreamViewType"),
            record=record,
        )

    def _decode(
        self,
        model: type[Any] | None,
        item: dict[str, Any] | None,
        key: dict[str, Any],
        name: str,
        error: _RecordError,
    ) -> Any:
        if model is None or item is None:
            return None
        try:
            if self.client is not None:
                return self.repositories[model].from_item(item)
            return schema_of(model)._decode(item, item)
        except ItemDecodeError as cause:
            problems = "; ".join(str(problem) for problem in cause.errors) or str(cause)
            raised = error(f"its {name} does not decode as {model.__name__}: {problems}", model=model, key=key)
            raised.errors = cause.errors
            raise raised from cause


class _RecordError:
    """Builds the :class:`StreamRecordError` of one record."""

    def __init__(self, sequence_number: Any, event_id: Any, event_name: Any) -> None:
        self.sequence_number = sequence_number if isinstance(sequence_number, str) else None
        self.event_id = event_id if isinstance(event_id, str) else None
        self.event_name = event_name

    def __call__(
        self, problem: str, *, model: type | None = None, key: dict[str, Any] | None = None
    ) -> StreamRecordError:
        name = self.sequence_number or self.event_id
        label = f"stream record {name}" if name else "a stream record"
        if isinstance(self.event_name, str) and self.event_name in _EVENT_NAMES:
            label += f" ({self.event_name})"
        return StreamRecordError(
            f"{label}{' (see .key)' if key else ''}: {problem}",
            model=model,
            key=key,
            sequence_number=self.sequence_number,
            event_id=self.event_id,
        )


def _image(data: Mapping[str, Any], name: str, error: _RecordError) -> dict[str, Any] | None:
    """An image (or the keys) of a record as Python values."""
    image = data.get(name)
    if image is None:
        return None
    if not isinstance(image, Mapping):
        raise error(f"its {name} is a {type(image).__name__}, not an item in DynamoDB's typed JSON")
    try:
        return from_wire(cast("Mapping[str, Mapping[str, Any]]", image))
    except (ValidationError, ValueError, TypeError) as cause:
        raise error(f"its {name} is not an item in DynamoDB's typed JSON: {cause}") from cause


def _table_of(arn: Any) -> str | None:
    """The table name of a stream's ARN (``arn:aws:dynamodb:<region>:<account>:table/<name>/stream/<label>``)."""
    if not isinstance(arn, str):
        return None
    parts = arn.split(":", 5)
    if len(parts) != 6 or not parts[5].startswith("table/"):
        return None
    return parts[5].removeprefix("table/").split("/", 1)[0] or None


def _created_at(value: Any) -> datetime | None:
    """``ApproximateCreationDateTime``: seconds since the epoch in Lambda events, a datetime from boto3."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC)
    return None
