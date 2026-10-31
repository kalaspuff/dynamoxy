"""Results of operations: consumed capacity, table descriptions, pages, backups, metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from dynamoxy import _core

__all__ = [
    "Backup",
    "CallStats",
    "ConsumedCapacity",
    "ContinuousBackups",
    "IndexDescription",
    "MetricsSnapshot",
    "OperationMetrics",
    "OperationRecord",
    "Page",
    "TableDescription",
    "TimeToLive",
]


def _timestamp(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


@dataclass(frozen=True, slots=True)
class CallStats:
    """What one call cost.

    Attributes:
        attempts: Attempts sent (1 when the first succeeded).
        throttles: Attempts DynamoDB throttled.
        requests: Requests sent: 1 for a single call, however many attempts it took; managed
            batches send one per chunk and round of unprocessed items.
        duration: Seconds, retries included.
    """

    attempts: int = 0
    throttles: int = 0
    requests: int = 0
    duration: float = 0.0

    @classmethod
    def from_core(cls, stats: tuple[int, int, int, float]) -> CallStats:
        """From the native ``(attempts, throttles, requests, seconds)`` tuple."""
        return cls(*stats)


@dataclass(frozen=True, slots=True)
class ConsumedCapacity:
    """Capacity a request consumed on one table.

    Attributes:
        table: Table name.
        capacity_units: Total units.
        read_capacity_units: Read units (may be 0 when DynamoDB only reports the total).
        write_capacity_units: Write units (may be 0 when DynamoDB only reports the total).
        indexes: Units per index (when requested with ``"INDEXES"``).
    """

    table: str
    capacity_units: float = 0.0
    read_capacity_units: float = 0.0
    write_capacity_units: float = 0.0
    indexes: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def from_core(cls, raw: _core.ConsumedCapacityDict) -> ConsumedCapacity:
        """From the native dict."""
        indexes = {
            name: capacity["capacity_units"]
            for group in (raw["global_secondary_indexes"], raw["local_secondary_indexes"])
            for name, capacity in group.items()
        }
        return cls(
            table=raw["table_name"],
            capacity_units=raw["capacity_units"],
            read_capacity_units=raw["read_capacity_units"],
            write_capacity_units=raw["write_capacity_units"],
            indexes=indexes,
        )

    @classmethod
    def total(cls, capacities: Sequence[ConsumedCapacity]) -> ConsumedCapacity | None:
        """The sum of several reports (``None`` when there are none)."""
        if not capacities:
            return None
        indexes: dict[str, float] = {}
        for capacity in capacities:
            for name, units in capacity.indexes.items():
                indexes[name] = indexes.get(name, 0.0) + units
        tables = {capacity.table for capacity in capacities}
        return cls(
            table=next(iter(tables)) if len(tables) == 1 else "*",
            capacity_units=sum(capacity.capacity_units for capacity in capacities),
            read_capacity_units=sum(capacity.read_capacity_units for capacity in capacities),
            write_capacity_units=sum(capacity.write_capacity_units for capacity in capacities),
            indexes=indexes,
        )


def capacity_split(
    operation: str, capacities: Sequence[ConsumedCapacity], *, writes: bool | None = None
) -> tuple[float, float]:  # pragma: no cover - trivial
    """``(read units, write units)``, attributing reported totals by operation kind.

    ``writes`` says whether the request writes, for operations whose kind does not say it
    (PartiQL statements); by default only the write operations write.
    """
    if writes is None:
        writes = operation in {"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}
    read = write = 0.0
    for capacity in capacities:
        units = capacity.capacity_units or (capacity.read_capacity_units + capacity.write_capacity_units)
        if capacity.read_capacity_units or capacity.write_capacity_units:
            read += capacity.read_capacity_units
            write += capacity.write_capacity_units
        elif writes:
            write += units
        else:
            read += units
    return read, write


@dataclass(frozen=True, slots=True)
class IndexDescription:
    """A secondary index of a table.

    Attributes:
        name: Index name.
        kind: ``"global"`` or ``"local"``.
        partition_key: Partition key attribute.
        sort_key: Sort key attribute.
        projection: ``"ALL"``, ``"KEYS_ONLY"`` or ``"INCLUDE"``.
        projected_attributes: Non-key attributes of ``INCLUDE`` projections.
        status: ``"ACTIVE"``, ``"CREATING"``, ... (global indexes).
        item_count: Approximate number of items.
        size_bytes: Approximate size.
        backfilling: Whether DynamoDB is still filling a new index.
    """

    name: str
    kind: Literal["global", "local"]
    partition_key: str
    sort_key: str | None
    projection: str
    projected_attributes: tuple[str, ...] = ()
    status: str | None = None
    item_count: int = 0
    size_bytes: int = 0
    backfilling: bool = False


@dataclass(frozen=True, slots=True)
class TableDescription:
    """A table, as ``DescribeTable`` reports it.

    Attributes:
        name: Table name.
        status: ``"ACTIVE"``, ``"CREATING"``, ``"UPDATING"``, ``"DELETING"``, ...
        arn: ARN.
        partition_key: Partition key attribute.
        sort_key: Sort key attribute.
        attribute_types: Types of key attributes (``{"pk": "S"}``).
        billing_mode: ``"PAY_PER_REQUEST"`` or ``"PROVISIONED"``.
        read_capacity: Provisioned read units.
        write_capacity: Provisioned write units.
        item_count: Approximate number of items (DynamoDB updates it about every six hours).
        size_bytes: Approximate size.
        indexes: Secondary indexes by name.
        stream_view_type: The stream's view type when a stream is enabled.
        stream_arn: ARN of the latest stream.
        table_class: ``"STANDARD"`` or ``"STANDARD_INFREQUENT_ACCESS"``.
        deletion_protection: Whether deletion protection is on.
        created: Creation time.
    """

    name: str
    status: str
    arn: str | None
    partition_key: str
    sort_key: str | None
    attribute_types: Mapping[str, str]
    billing_mode: str
    read_capacity: int | None
    write_capacity: int | None
    item_count: int
    size_bytes: int
    indexes: Mapping[str, IndexDescription]
    stream_view_type: str | None
    stream_arn: str | None
    table_class: str | None
    deletion_protection: bool
    created: datetime | None

    @property
    def active(self) -> bool:
        """Whether the table and every index are ``ACTIVE``."""
        return self.status == "ACTIVE" and all(index.status in (None, "ACTIVE") for index in self.indexes.values())

    @classmethod
    def from_core(cls, raw: _core.TableDescriptionDict) -> TableDescription:
        """From the native dict."""
        indexes: dict[str, IndexDescription] = {}
        for kind, entries in (("global", raw["global_secondary_indexes"]), ("local", raw["local_secondary_indexes"])):
            for entry in entries:
                indexes[entry["index_name"]] = IndexDescription(
                    name=entry["index_name"],
                    kind=kind,  # pyrefly: ignore[bad-argument-type]
                    partition_key=entry["key_schema"][0],
                    sort_key=entry["key_schema"][1],
                    projection=entry["projection"][0],
                    projected_attributes=tuple(entry["projection"][1]),
                    status=entry["index_status"],
                    item_count=entry["item_count"],
                    size_bytes=entry["index_size_bytes"],
                    backfilling=entry["backfilling"],
                )
        throughput = raw["provisioned_throughput"]
        stream = raw["stream_specification"]
        return cls(
            name=raw["table_name"],
            status=raw["table_status"],
            arn=raw["table_arn"],
            partition_key=raw["key_schema"][0],
            sort_key=raw["key_schema"][1],
            attribute_types=dict(raw["attribute_definitions"]),
            billing_mode=raw["billing_mode"],
            read_capacity=throughput[0] if throughput else None,
            write_capacity=throughput[1] if throughput else None,
            item_count=raw["item_count"],
            size_bytes=raw["table_size_bytes"],
            indexes=indexes,
            stream_view_type=stream[1] if stream and stream[0] else None,
            stream_arn=raw["latest_stream_arn"],
            table_class=raw["table_class"],
            deletion_protection=raw["deletion_protection_enabled"],
            created=_timestamp(raw["creation_date_time"]),
        )


@dataclass(frozen=True, slots=True)
class TimeToLive:
    """Time to Live state of a table.

    Attributes:
        status: ``"ENABLED"``, ``"DISABLED"``, ``"ENABLING"`` or ``"DISABLING"``.
        attribute: The TTL attribute.
    """

    status: str
    attribute: str | None

    @property
    def enabled(self) -> bool:
        """Whether TTL is enabled (or being enabled)."""
        return self.status in ("ENABLED", "ENABLING")


@dataclass(frozen=True, slots=True)
class Backup:
    """An on-demand backup.

    Attributes:
        arn: Backup ARN.
        name: Backup name.
        status: ``"CREATING"``, ``"AVAILABLE"`` or ``"DELETED"``.
        type: ``"USER"``, ``"SYSTEM"`` or ``"AWS_BACKUP"``.
        table: Source table name.
        size_bytes: Size.
        created: Creation time.
    """

    arn: str
    name: str
    status: str
    type: str
    table: str | None
    size_bytes: int | None
    created: datetime | None

    @classmethod
    def from_core(cls, raw: Mapping[str, Any]) -> Backup:
        """From a native backup dict."""
        return cls(
            arn=raw["backup_arn"],
            name=raw["backup_name"],
            status=raw["backup_status"],
            type=raw["backup_type"],
            table=raw.get("table_name"),
            size_bytes=raw.get("backup_size_bytes"),
            created=_timestamp(raw.get("backup_creation_date_time")),
        )


@dataclass(frozen=True, slots=True)
class ContinuousBackups:
    """Continuous backups and point-in-time recovery state.

    Attributes:
        point_in_time_recovery: Whether point-in-time recovery is enabled.
        recovery_period_days: Days of recovery history.
        earliest_restorable: Earliest time a restore can target.
        latest_restorable: Latest time a restore can target.
    """

    point_in_time_recovery: bool
    recovery_period_days: int | None
    earliest_restorable: datetime | None
    latest_restorable: datetime | None

    @classmethod
    def from_core(cls, raw: _core.ContinuousBackupsDict) -> ContinuousBackups:
        """From the native dict."""
        return cls(
            point_in_time_recovery=raw["point_in_time_recovery_status"] == "ENABLED",
            recovery_period_days=raw["recovery_period_in_days"],
            earliest_restorable=_timestamp(raw["earliest_restorable_date_time"]),
            latest_restorable=_timestamp(raw["latest_restorable_date_time"]),
        )


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a query or scan.

    Attributes:
        items: Items of the page (may be empty when a filter removed every item).
        cursor: Opaque text to continue after this page (``None`` on the last page). Safe to send
            to API clients; pass it back with ``.after(cursor)``.
        count: Items that matched (after the filter).
        scanned_count: Items read (before the filter).
        consumed_capacity: Capacity the request consumed.
        last_evaluated_key: The raw ``LastEvaluatedKey`` (``cursor`` encodes it).
    """

    items: list[T]
    cursor: str | None
    count: int
    scanned_count: int
    consumed_capacity: ConsumedCapacity | None = None
    last_evaluated_key: dict[str, Any] | None = None

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def has_more(self) -> bool:
        """Whether another page may follow."""
        return self.cursor is not None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One operation, as collected by :func:`dynamoxy.collect` and reported in events.

    Attributes:
        operation: DynamoDB operation (``"PutItem"``).
        table: Physical table name (``None`` for multi-table operations).
        model: Model class name, when a repository made the call.
        duration: Seconds, retries included.
        attempts: Attempts sent.
        throttles: Attempts throttled.
        requests: Requests sent: 1 for a single call, however many attempts it took; managed
            batches send one per chunk and round of unprocessed items. Retries are ``attempts -
            requests``.
        read_capacity_units: Read units consumed (when reported).
        write_capacity_units: Write units consumed (when reported).
        items: Items returned or written.
        error: Error code (``"ConditionalCheckFailedException"``) or class name, if it failed.
        timestamp: When the operation finished (seconds since the epoch).
    """

    operation: str
    table: str | None
    model: str | None
    duration: float
    attempts: int
    throttles: int
    requests: int
    read_capacity_units: float
    write_capacity_units: float
    items: int
    error: str | None
    timestamp: float


@dataclass(frozen=True, slots=True)
class OperationMetrics:
    """Counters of one operation (since the client started or ``reset_metrics()``).

    Attributes:
        calls: Completed calls.
        errors: Calls that failed.
        attempts: Attempts (calls plus retries).
        throttles: Throttled attempts.
        duration: Total seconds.
        read_capacity_units: Read units consumed.
        write_capacity_units: Write units consumed.
        latency_buckets: Calls per latency bucket (see ``MetricsSnapshot.latency_buckets_ms``).
    """

    calls: int
    errors: int
    attempts: int
    throttles: int
    duration: float
    read_capacity_units: float
    write_capacity_units: float
    latency_buckets: tuple[int, ...]

    @property
    def average_latency(self) -> float:
        """Mean seconds per call."""
        return self.duration / self.calls if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Counters of a client, read on demand with ``client.metrics()``.

    Attributes:
        operations: Counters per operation (only operations called at least once).
        in_flight: Calls in progress.
        latency_buckets_ms: Upper bounds of the latency buckets (``None`` = unbounded).
    """

    operations: Mapping[str, OperationMetrics]
    in_flight: int
    latency_buckets_ms: tuple[float | None, ...]

    @classmethod
    def from_core(cls, raw: _core.MetricsDict) -> MetricsSnapshot:
        """From the native dict."""
        return cls(
            operations={
                name: OperationMetrics(
                    calls=entry["calls"],
                    errors=entry["errors"],
                    attempts=entry["attempts"],
                    throttles=entry["throttles"],
                    duration=entry["duration"],
                    read_capacity_units=entry["read_capacity_units"],
                    write_capacity_units=entry["write_capacity_units"],
                    latency_buckets=tuple(entry["latency_buckets"]),
                )
                for name, entry in raw["operations"].items()
            },
            in_flight=raw["in_flight"],
            latency_buckets_ms=tuple(raw["latency_buckets_ms"]),
        )

    @property
    def calls(self) -> int:
        """Calls of every operation."""
        return sum(entry.calls for entry in self.operations.values())

    @property
    def read_capacity_units(self) -> float:
        """Read units of every operation."""
        return sum(entry.read_capacity_units for entry in self.operations.values())

    @property
    def write_capacity_units(self) -> float:
        """Write units of every operation."""
        return sum(entry.write_capacity_units for entry in self.operations.values())
