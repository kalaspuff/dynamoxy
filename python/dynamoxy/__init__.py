"""dynamoxy: a typed, async DynamoDB ORM for Python, powered by a Rust core.

Quick start::

    from datetime import datetime

    import dynamoxy


    class User(dynamoxy.Model, table="users"):
        user_id: dynamoxy.Attr[str] = dynamoxy.partition_key()
        email: dynamoxy.Attr[str] = dynamoxy.field(unique=True)
        name: dynamoxy.Attr[str]
        visits: dynamoxy.Attr[int] = dynamoxy.field(default=0)
        created_at: dynamoxy.Attr[datetime] = dynamoxy.created_at()


    async def main() -> None:
        async with dynamoxy.Client(region="eu-west-1") as client:
            await client.ensure_tables(User)
            user = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
            await user.increment(User.visits)  # atomic, through the repository that wrote it
            user.name = "Ada Lovelace"
            await user.save()

Read ``dynamoxy guide`` (or ``python -m dynamoxy guide``) for the full usage guide.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any

# Public submodules: `from x import m as m` (or `__all__`) makes type checkers see `dynamoxy.errors`
# and the others after `import dynamoxy`; `from dynamoxy.errors import ...` alone does not.
from dynamoxy import attributes as attributes
from dynamoxy import batch as batch
from dynamoxy import client as client
from dynamoxy import codecs, testing
from dynamoxy import collection as collection
from dynamoxy import constraints as constraints
from dynamoxy import errors as errors
from dynamoxy import events as events
from dynamoxy import expressions as expressions
from dynamoxy import middleware as middleware
from dynamoxy import models as models
from dynamoxy import query as query
from dynamoxy import repository as repository
from dynamoxy import results as results
from dynamoxy import schema as schema
from dynamoxy import streams as streams
from dynamoxy import table as table
from dynamoxy import telemetry as telemetry
from dynamoxy import transactions as transactions
from dynamoxy._core import MemoryStore, from_wire, to_wire
from dynamoxy._core import __version__ as __version__
from dynamoxy.attributes import (
    TTL,
    Attr,
    AttributeName,
    CreatedAt,
    FieldPath,
    PartitionKey,
    SortKey,
    Template,
    Unique,
    UpdatedAt,
    Version,
    created_at,
    field,
    partition_key,
    sort_key,
    ttl,
    updated_at,
    version,
)
from dynamoxy.batch import BatchWriter
from dynamoxy.client import Client, Credentials, RetryPolicy
from dynamoxy.codecs import Codec, UseCodec
from dynamoxy.collection import Collection
from dynamoxy.constraints import DuplicateValue, MissingGuard, OrphanedGuard, UniqueRepair, UniqueReport
from dynamoxy.errors import (
    AccessDeniedError,
    BackupError,
    CancellationReason,
    ConditionalCheckFailedError,
    ConfigurationError,
    CredentialsError,
    DuplicateItemError,
    DynamoxyError,
    FieldError,
    IdempotentParameterMismatchError,
    InternalServerError,
    ItemAlreadyExistsError,
    ItemCollectionSizeLimitExceededError,
    ItemDecodeError,
    ItemNotFoundError,
    LimitExceededError,
    ResourceInUseError,
    ResourceNotFoundError,
    ServiceError,
    ShutdownError,
    StreamRecordError,
    ThrottlingError,
    TransactionCanceledError,
    TransactionConflictError,
    TransactionInProgressError,
    TransportError,
    UniqueConstraintError,
    UnprocessedItemsError,
    ValidationError,
    VersionConflictError,
    WriteOutcomeUnknownError,
    cancellation_reasons,
)
from dynamoxy.events import Event, EventHook, EventKind, LoggingConfig
from dynamoxy.expressions import Condition, F, Path, UpdateAction, and_, attr, condition, not_, or_
from dynamoxy.middleware import CallNext, HookContext, Middleware, Request, Response
from dynamoxy.models import (
    Document,
    Model,
    after_delete,
    after_load,
    after_save,
    after_update,
    before_delete,
    before_save,
    before_update,
    validator,
)
from dynamoxy.query import Query, Scan
from dynamoxy.repository import Repository
from dynamoxy.results import (
    Backup,
    CallStats,
    ConsumedCapacity,
    ContinuousBackups,
    IndexDescription,
    MetricsSnapshot,
    OperationMetrics,
    OperationRecord,
    Page,
    TableDescription,
    TimeToLive,
)
from dynamoxy.schema import GlobalIndex, KeyTemplate, LocalIndex, Projection, Throughput, schema_of
from dynamoxy.table import Billing, IndexDefinition, Table, TableDefinition
from dynamoxy.telemetry import Collector, Telemetry, collect
from dynamoxy.transactions import Get, Transaction

if TYPE_CHECKING:
    from dynamoxy._pydantic import PydanticModel

__all__ = [
    "TTL",
    "AccessDeniedError",
    "Attr",
    "AttributeName",
    "Backup",
    "BackupError",
    "BatchWriter",
    "Billing",
    "CallNext",
    "CallStats",
    "CancellationReason",
    "Client",
    "Codec",
    "Collection",
    "Collector",
    "Condition",
    "ConditionalCheckFailedError",
    "ConfigurationError",
    "ConsumedCapacity",
    "ContinuousBackups",
    "CreatedAt",
    "Credentials",
    "CredentialsError",
    "Document",
    "DuplicateItemError",
    "DuplicateValue",
    "DynamoxyError",
    "Event",
    "EventHook",
    "EventKind",
    "F",
    "FieldError",
    "FieldPath",
    "Get",
    "GlobalIndex",
    "HookContext",
    "IdempotentParameterMismatchError",
    "IndexDefinition",
    "IndexDescription",
    "InternalServerError",
    "ItemAlreadyExistsError",
    "ItemCollectionSizeLimitExceededError",
    "ItemDecodeError",
    "ItemNotFoundError",
    "KeyTemplate",
    "LimitExceededError",
    "LocalIndex",
    "LoggingConfig",
    "MemoryStore",
    "MetricsSnapshot",
    "Middleware",
    "MissingGuard",
    "Model",
    "OperationMetrics",
    "OperationRecord",
    "OrphanedGuard",
    "Page",
    "PartitionKey",
    "Path",
    "Projection",
    "Query",
    "Repository",
    "Request",
    "ResourceInUseError",
    "ResourceNotFoundError",
    "Response",
    "RetryPolicy",
    "Scan",
    "ServiceError",
    "ShutdownError",
    "SortKey",
    "StreamRecordError",
    "Table",
    "TableDefinition",
    "TableDescription",
    "Telemetry",
    "Template",
    "ThrottlingError",
    "Throughput",
    "TimeToLive",
    "Transaction",
    "TransactionCanceledError",
    "TransactionConflictError",
    "TransactionInProgressError",
    "TransportError",
    "Unique",
    "UniqueConstraintError",
    "UniqueRepair",
    "UniqueReport",
    "UnprocessedItemsError",
    "UpdateAction",
    "UpdatedAt",
    "UseCodec",
    "ValidationError",
    "Version",
    "VersionConflictError",
    "WriteOutcomeUnknownError",
    "__version__",
    "after_delete",
    "after_load",
    "after_save",
    "after_update",
    "and_",
    "attr",
    "before_delete",
    "before_save",
    "before_update",
    "cancellation_reasons",
    "codecs",
    "collect",
    "condition",
    "created_at",
    "field",
    "from_wire",
    "not_",
    "or_",
    "partition_key",
    "schema_of",
    "sort_key",
    "testing",
    "to_wire",
    "ttl",
    "updated_at",
    "validator",
    "version",
]

# `from dynamoxy import *` imports every name of `__all__`: PydanticModel only with pydantic
# installed (pip install dynamoxy[pydantic]), where it loads pydantic.
if importlib.util.find_spec("pydantic") is not None:  # pragma: no branch - depends on the environment
    __all__ += ["PydanticModel"]


if not TYPE_CHECKING:
    # Hidden from type checkers, which would type every unknown name of the package as `Any`.

    def __getattr__(name: str) -> Any:
        # PydanticModel imports pydantic: load it only when used (pip install dynamoxy[pydantic]).
        if name == "PydanticModel":
            try:
                from dynamoxy._pydantic import PydanticModel
            except ImportError as error:  # pragma: no cover - depends on the environment
                raise ImportError("dynamoxy.PydanticModel needs pydantic: pip install 'dynamoxy[pydantic]'") from error
            return PydanticModel
        raise AttributeError(f"module 'dynamoxy' has no attribute {name!r}")
