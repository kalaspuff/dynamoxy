# Type stubs for the native module built from crates/dynamoxy-py.
# Keep in sync with the Rust bindings: scripts/check_consistency.py verifies the names.
#
# Every operation resolves to `(output, stats)`: `output` mirrors the DynamoDB response with
# snake_case keys and plain Python values; `stats` is (attempts, throttles, requests, seconds).

from collections.abc import Awaitable, Iterable, Mapping, Sequence
from typing import Any, Literal, TypedDict, final

from dynamoxy.errors import CancellationReason, FieldError

__version__: str

class DynamoxyError(Exception):
    retryable: bool
    code: str | None
    message: str | None
    operation: str | None
    request_id: str | None
    status_code: int | None
    attempts: int | None
    throttles: int | None
    requests: int | None
    duration: float | None

class ConfigurationError(DynamoxyError): ...

class ValidationError(DynamoxyError):
    # Added by dynamoxy.errors: the field errors of model validation (empty otherwise).
    errors: tuple[FieldError, ...]

class ServiceError(DynamoxyError): ...

class ConditionalCheckFailedError(ServiceError):
    item: dict[str, Any] | None
    # Added by dynamoxy.errors: the primary key of the item a model write targeted (None otherwise).
    key: dict[str, Any] | None

class TransactionCanceledError(ServiceError):
    reasons: list[tuple[str, str | None, dict[str, Any] | None]]
    # Added by dynamoxy.errors: explained reasons and the specific errors of model operations.
    @property
    def cancellation_reasons(self) -> list[CancellationReason]: ...
    errors: tuple[DynamoxyError, ...]

class TransactionConflictError(ServiceError): ...
class TransactionInProgressError(ServiceError): ...
class IdempotentParameterMismatchError(ServiceError): ...
class ThrottlingError(ServiceError): ...
class ResourceNotFoundError(ServiceError): ...
class ResourceInUseError(ServiceError): ...
class LimitExceededError(ServiceError): ...
class ItemCollectionSizeLimitExceededError(ServiceError): ...
class DuplicateItemError(ServiceError): ...
class BackupError(ServiceError): ...
class AccessDeniedError(ServiceError): ...
class InternalServerError(ServiceError): ...
class TransportError(DynamoxyError): ...

class WriteOutcomeUnknownError(TransportError):
    item: dict[str, Any] | None

class CredentialsError(DynamoxyError): ...
class ShutdownError(DynamoxyError): ...

type Item = dict[str, Any]
type Stats = tuple[int, int, int, float]
type RawEvent = tuple[str, str, dict[str, str]]
type KeySchemaTuple = tuple[str, str | None]
type ProjectionTuple = tuple[str, list[str]]
type Throughput = tuple[int, int]
type OnDemand = tuple[int | None, int | None]

class CapacityDict(TypedDict):
    read_capacity_units: float
    write_capacity_units: float
    capacity_units: float

class ConsumedCapacityDict(TypedDict):
    table_name: str
    capacity_units: float
    read_capacity_units: float
    write_capacity_units: float
    table: CapacityDict | None
    global_secondary_indexes: dict[str, CapacityDict]
    local_secondary_indexes: dict[str, CapacityDict]

class ItemCollectionMetricsDict(TypedDict):
    item_collection_key: Item
    size_estimate_range_gb: list[float]

class GetItemOutput(TypedDict):
    item: Item | None
    consumed_capacity: ConsumedCapacityDict | None

class WriteItemOutput(TypedDict):
    attributes: Item | None
    consumed_capacity: ConsumedCapacityDict | None
    item_collection_metrics: ItemCollectionMetricsDict | None

class QueryOutput(TypedDict):
    items: list[Item]
    count: int
    scanned_count: int
    last_evaluated_key: Item | None
    consumed_capacity: ConsumedCapacityDict | None

class KeysAndAttributesDict(TypedDict):
    keys: list[Item]
    consistent_read: bool
    projection_expression: str | None
    expression_attribute_names: dict[str, str]

class BatchGetItemOutput(TypedDict):
    responses: dict[str, list[Item]]
    unprocessed_keys: dict[str, KeysAndAttributesDict]
    consumed_capacity: list[ConsumedCapacityDict]

class BatchWriteItemOutput(TypedDict):
    unprocessed_items: dict[str, list[tuple[str, Item]]]
    consumed_capacity: list[ConsumedCapacityDict]
    item_collection_metrics: dict[str, list[ItemCollectionMetricsDict]]

class BatchGetOutput(TypedDict):
    items: dict[str, list[Item]]
    unprocessed_keys: dict[str, KeysAndAttributesDict]
    consumed_capacity: list[ConsumedCapacityDict]
    error: DynamoxyError | None

class BatchWriteOutput(TypedDict):
    unprocessed: list[tuple[str, str, Item]]
    consumed_capacity: list[ConsumedCapacityDict]
    error: DynamoxyError | None

class TransactGetItemsOutput(TypedDict):
    responses: list[Item | None]
    consumed_capacity: list[ConsumedCapacityDict]

class TransactWriteItemsOutput(TypedDict):
    consumed_capacity: list[ConsumedCapacityDict]
    item_collection_metrics: dict[str, list[ItemCollectionMetricsDict]]

class ExecuteStatementOutput(TypedDict):
    items: list[Item]
    next_token: str | None
    last_evaluated_key: Item | None
    consumed_capacity: ConsumedCapacityDict | None

class BatchStatementResponseDict(TypedDict):
    error: tuple[str, str | None, Item | None] | None
    table_name: str | None
    item: Item | None

class BatchExecuteStatementOutput(TypedDict):
    responses: list[BatchStatementResponseDict]
    consumed_capacity: list[ConsumedCapacityDict]

class ExecuteTransactionOutput(TypedDict):
    responses: list[Item | None]
    consumed_capacity: list[ConsumedCapacityDict]

class IndexDescriptionDict(TypedDict):
    index_name: str
    key_schema: KeySchemaTuple
    projection: ProjectionTuple
    index_status: str | None
    item_count: int
    index_size_bytes: int
    index_arn: str | None
    provisioned_throughput: Throughput | None
    backfilling: bool

class TableDescriptionDict(TypedDict):
    table_name: str
    table_arn: str | None
    table_id: str | None
    table_status: str
    creation_date_time: float | None
    key_schema: KeySchemaTuple
    attribute_definitions: list[tuple[str, str]]
    item_count: int
    table_size_bytes: int
    billing_mode: str
    provisioned_throughput: Throughput | None
    on_demand_throughput: OnDemand | None
    global_secondary_indexes: list[IndexDescriptionDict]
    local_secondary_indexes: list[IndexDescriptionDict]
    stream_specification: tuple[bool, str | None] | None
    latest_stream_arn: str | None
    latest_stream_label: str | None
    sse_description: tuple[str | None, str | None, str | None] | None
    table_class: str | None
    deletion_protection_enabled: bool

class ListTablesOutput(TypedDict):
    table_names: list[str]
    last_evaluated_table_name: str | None

class ContinuousBackupsDict(TypedDict):
    continuous_backups_status: str
    point_in_time_recovery_status: str
    recovery_period_in_days: int | None
    earliest_restorable_date_time: float | None
    latest_restorable_date_time: float | None

class BackupDetailsDict(TypedDict):
    backup_arn: str
    backup_name: str
    backup_size_bytes: int | None
    backup_status: str
    backup_type: str
    backup_creation_date_time: float
    backup_expiry_date_time: float | None

class BackupDescriptionDict(BackupDetailsDict):
    table_name: str
    table_arn: str | None
    table_id: str | None
    key_schema: KeySchemaTuple | None
    item_count: int | None

class BackupSummaryDict(BackupDetailsDict):
    table_name: str
    table_arn: str | None

class ListBackupsOutput(TypedDict):
    backups: list[BackupSummaryDict]
    last_evaluated_backup_arn: str | None

class OperationMetricsDict(TypedDict):
    calls: int
    errors: int
    attempts: int
    throttles: int
    duration: float
    read_capacity_units: float
    write_capacity_units: float
    latency_buckets: list[int]

class MetricsDict(TypedDict):
    in_flight: int
    latency_buckets_ms: list[float | None]
    operations: dict[str, OperationMetricsDict]

type ReturnValues = Literal["NONE", "ALL_OLD", "UPDATED_OLD", "ALL_NEW", "UPDATED_NEW"]
type ReturnOnFailure = Literal["NONE", "ALL_OLD"]
type ReturnCapacity = Literal["NONE", "TOTAL", "INDEXES"]
type SelectValue = Literal["ALL_ATTRIBUTES", "ALL_PROJECTED_ATTRIBUTES", "SPECIFIC_ATTRIBUTES", "COUNT"]

@final
class MemoryStore:
    def __init__(self, *, region: str = "us-east-1", account_id: str = "000000000000") -> None: ...
    def inject_fault(
        self,
        *,
        operation: str | None = None,
        table: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
        unprocessed: bool = False,
        latency: float | None = None,
        lose_response: bool = False,
        refuse_connection: bool = False,
        times: int = 1,
    ) -> None: ...
    def clear_faults(self) -> None: ...
    def purge_expired(self, at: float | None = None) -> int: ...
    def reset(self) -> None: ...

@final
class CoreClient:
    @staticmethod
    def memory(config: Mapping[str, object], store: MemoryStore | None = None) -> CoreClient: ...
    @staticmethod
    def connect(config: Mapping[str, object]) -> Awaitable[CoreClient]: ...
    @property
    def backend(self) -> str: ...
    @property
    def store(self) -> MemoryStore | None: ...
    @property
    def closed(self) -> bool: ...
    def close(self) -> None: ...
    def get_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        *,
        consistent_read: bool = False,
        projection_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
    ) -> Awaitable[tuple[GetItemOutput, Stats]]: ...
    def put_item(
        self,
        table_name: str,
        item: Mapping[str, Any],
        *,
        condition_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        expression_attribute_values: Mapping[str, Any] | None = None,
        return_values: ReturnValues = "NONE",
        return_values_on_condition_check_failure: ReturnOnFailure = "NONE",
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_item_collection_metrics: bool = False,
    ) -> Awaitable[tuple[WriteItemOutput, Stats]]: ...
    def update_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        *,
        update_expression: str | None = None,
        condition_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        expression_attribute_values: Mapping[str, Any] | None = None,
        return_values: ReturnValues = "NONE",
        return_values_on_condition_check_failure: ReturnOnFailure = "NONE",
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_item_collection_metrics: bool = False,
    ) -> Awaitable[tuple[WriteItemOutput, Stats]]: ...
    def delete_item(
        self,
        table_name: str,
        key: Mapping[str, Any],
        *,
        condition_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        expression_attribute_values: Mapping[str, Any] | None = None,
        return_values: ReturnValues = "NONE",
        return_values_on_condition_check_failure: ReturnOnFailure = "NONE",
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_item_collection_metrics: bool = False,
    ) -> Awaitable[tuple[WriteItemOutput, Stats]]: ...
    def query(
        self,
        table_name: str,
        *,
        key_condition_expression: str,
        index_name: str | None = None,
        filter_expression: str | None = None,
        projection_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        expression_attribute_values: Mapping[str, Any] | None = None,
        select: SelectValue | None = None,
        limit: int | None = None,
        consistent_read: bool = False,
        scan_index_forward: bool = True,
        exclusive_start_key: Mapping[str, Any] | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
    ) -> Awaitable[tuple[QueryOutput, Stats]]: ...
    def scan(
        self,
        table_name: str,
        *,
        index_name: str | None = None,
        filter_expression: str | None = None,
        projection_expression: str | None = None,
        expression_attribute_names: Mapping[str, str] | None = None,
        expression_attribute_values: Mapping[str, Any] | None = None,
        select: SelectValue | None = None,
        limit: int | None = None,
        consistent_read: bool = False,
        exclusive_start_key: Mapping[str, Any] | None = None,
        segment: int | None = None,
        total_segments: int | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
    ) -> Awaitable[tuple[QueryOutput, Stats]]: ...
    def batch_get_item(
        self, request_items: Mapping[str, Mapping[str, Any]], *, return_consumed_capacity: ReturnCapacity = "NONE"
    ) -> Awaitable[tuple[BatchGetItemOutput, Stats]]: ...
    def batch_write_item(
        self,
        request_items: Mapping[str, Iterable[tuple[str, Mapping[str, Any]]]],
        *,
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_item_collection_metrics: bool = False,
    ) -> Awaitable[tuple[BatchWriteItemOutput, Stats]]: ...
    def batch_get(
        self, request_items: Mapping[str, Mapping[str, Any]], *, return_consumed_capacity: ReturnCapacity = "NONE"
    ) -> Awaitable[tuple[BatchGetOutput, Stats]]: ...
    def batch_write(
        self,
        requests: Iterable[tuple[str, str, Mapping[str, Any]]],
        *,
        return_consumed_capacity: ReturnCapacity = "NONE",
    ) -> Awaitable[tuple[BatchWriteOutput, Stats]]: ...
    def transact_get_items(
        self, transact_items: Sequence[Mapping[str, Any]], *, return_consumed_capacity: ReturnCapacity = "NONE"
    ) -> Awaitable[tuple[TransactGetItemsOutput, Stats]]: ...
    def transact_write_items(
        self,
        transact_items: Sequence[Mapping[str, Any]],
        *,
        client_request_token: str | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_item_collection_metrics: bool = False,
    ) -> Awaitable[tuple[TransactWriteItemsOutput, Stats]]: ...
    def execute_statement(
        self,
        statement: str,
        *,
        parameters: list[Any] | tuple[Any, ...] | None = None,
        consistent_read: bool = False,
        next_token: str | None = None,
        limit: int | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
        return_values_on_condition_check_failure: ReturnOnFailure = "NONE",
    ) -> Awaitable[tuple[ExecuteStatementOutput, Stats]]: ...
    def batch_execute_statement(
        self, statements: Sequence[Mapping[str, Any]], *, return_consumed_capacity: ReturnCapacity = "NONE"
    ) -> Awaitable[tuple[BatchExecuteStatementOutput, Stats]]: ...
    def execute_transaction(
        self,
        statements: Sequence[Mapping[str, Any]],
        *,
        client_request_token: str | None = None,
        return_consumed_capacity: ReturnCapacity = "NONE",
    ) -> Awaitable[tuple[ExecuteTransactionOutput, Stats]]: ...
    def create_table(
        self,
        table_name: str,
        *,
        attribute_definitions: Sequence[tuple[str, str]],
        key_schema: KeySchemaTuple,
        local_secondary_indexes: Sequence[Mapping[str, Any]] = ...,
        global_secondary_indexes: Sequence[Mapping[str, Any]] = ...,
        billing_mode: str = "PAY_PER_REQUEST",
        provisioned_throughput: Throughput | None = None,
        on_demand_throughput: OnDemand | None = None,
        stream_specification: tuple[bool, str | None] | None = None,
        sse_specification: tuple[bool, str | None, str | None] | None = None,
        tags: Mapping[str, str] = ...,
        table_class: str | None = None,
        deletion_protection_enabled: bool = False,
    ) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def delete_table(self, table_name: str) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def describe_table(self, table_name: str) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def update_table(
        self,
        table_name: str,
        *,
        attribute_definitions: Sequence[tuple[str, str]] = ...,
        billing_mode: str | None = None,
        provisioned_throughput: Throughput | None = None,
        on_demand_throughput: OnDemand | None = None,
        global_secondary_index_updates: list[dict[str, Any]] | None = None,
        stream_specification: tuple[bool, str | None] | None = None,
        sse_specification: tuple[bool, str | None, str | None] | None = None,
        table_class: str | None = None,
        deletion_protection_enabled: bool | None = None,
    ) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def list_tables(
        self, *, exclusive_start_table_name: str | None = None, limit: int | None = None
    ) -> Awaitable[tuple[ListTablesOutput, Stats]]: ...
    def wait_for_table(
        self, table_name: str, *, exists: bool = True, timeout: float = 300.0
    ) -> Awaitable[TableDescriptionDict | None]: ...
    def update_time_to_live(
        self, table_name: str, *, enabled: bool, attribute_name: str
    ) -> Awaitable[tuple[tuple[str, str | None], Stats]]: ...
    def describe_time_to_live(self, table_name: str) -> Awaitable[tuple[tuple[str, str | None], Stats]]: ...
    def tag_resource(self, resource_arn: str, tags: Mapping[str, str]) -> Awaitable[tuple[None, Stats]]: ...
    def untag_resource(self, resource_arn: str, tag_keys: Sequence[str]) -> Awaitable[tuple[None, Stats]]: ...
    def list_tags_of_resource(self, resource_arn: str) -> Awaitable[tuple[dict[str, str], Stats]]: ...
    def describe_continuous_backups(self, table_name: str) -> Awaitable[tuple[ContinuousBackupsDict, Stats]]: ...
    def update_continuous_backups(
        self, table_name: str, *, enabled: bool, recovery_period_in_days: int | None = None
    ) -> Awaitable[tuple[ContinuousBackupsDict, Stats]]: ...
    def create_backup(self, table_name: str, backup_name: str) -> Awaitable[tuple[BackupDetailsDict, Stats]]: ...
    def describe_backup(self, backup_arn: str) -> Awaitable[tuple[BackupDescriptionDict, Stats]]: ...
    def delete_backup(self, backup_arn: str) -> Awaitable[tuple[BackupDescriptionDict, Stats]]: ...
    def list_backups(
        self,
        *,
        table_name: str | None = None,
        limit: int | None = None,
        exclusive_start_backup_arn: str | None = None,
        backup_type: str | None = None,
        time_range_lower_bound: float | None = None,
        time_range_upper_bound: float | None = None,
    ) -> Awaitable[tuple[ListBackupsOutput, Stats]]: ...
    def restore_table_from_backup(
        self,
        target_table_name: str,
        backup_arn: str,
        *,
        billing_mode: str | None = None,
        provisioned_throughput: Throughput | None = None,
        sse_specification: tuple[bool, str | None, str | None] | None = None,
    ) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def restore_table_to_point_in_time(
        self,
        source_table_name: str,
        target_table_name: str,
        *,
        use_latest_restorable_time: bool = True,
        restore_date_time: float | None = None,
        billing_mode: str | None = None,
        provisioned_throughput: Throughput | None = None,
        sse_specification: tuple[bool, str | None, str | None] | None = None,
    ) -> Awaitable[tuple[TableDescriptionDict, Stats]]: ...
    def set_event_filter(self, kinds: list[str]) -> None: ...
    def drain_events(self, max: int) -> list[RawEvent]: ...
    def wait_events(self, timeout: float) -> Awaitable[bool]: ...
    def take_dropped_events(self) -> int: ...
    def metrics(self) -> MetricsDict: ...
    def reset_metrics(self) -> None: ...

def event_kinds() -> list[tuple[str, str, str]]: ...
def operation_names() -> list[str]: ...
def to_wire(item: Mapping[str, Any], *, base64_binary: bool = False) -> dict[str, dict[str, Any]]: ...
def from_wire(item: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]: ...
def value_to_wire(value: Any, *, base64_binary: bool = False) -> dict[str, Any]: ...
def value_from_wire(value: Mapping[str, Any]) -> Any: ...
def is_reserved_word(name: str) -> bool: ...
def _check_expressions(
    *,
    condition: str | None = None,
    filter: str | None = None,
    key_condition: str | None = None,
    update: str | None = None,
    projection: str | None = None,
    names: Mapping[str, str] | None = None,
    values: Mapping[str, Any] | None = None,
) -> None: ...
def _apply_update(
    item: Mapping[str, Any],
    update: str,
    *,
    names: Mapping[str, str] | None = None,
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]: ...
def _reset_after_fork() -> None: ...
def _shutdown() -> None: ...
