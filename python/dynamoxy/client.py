"""The :class:`Client`: the entry point of dynamoxy."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self, overload

from dynamoxy import _core
from dynamoxy._core import CoreClient, MemoryStore
from dynamoxy._utils import LoopLocal, count, seconds
from dynamoxy.errors import DynamoxyError, ResourceInUseError, UnprocessedItemsError, unprocessed_items_error
from dynamoxy.events import EventDispatcher, EventHook, EventKind, LoggingConfig
from dynamoxy.middleware import HookContext, HookRegistry, Middleware, Request, Response
from dynamoxy.results import CallStats, ConsumedCapacity, MetricsSnapshot, OperationRecord, capacity_split
from dynamoxy.telemetry import Collector, Telemetry, TelemetryRecorder, active_collectors, collect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from datetime import timedelta
    from types import TracebackType

    from dynamoxy.batch import BatchWriter
    from dynamoxy.collection import Collection
    from dynamoxy.repository import Repository
    from dynamoxy.schema import HookPoint
    from dynamoxy.table import Billing, Table
    from dynamoxy.transactions import Get, Transaction

__all__ = ["Client", "Credentials", "RetryPolicy"]

_FORK_HOOK_REGISTERED = False

# Events taken from the core at a time; a call's events are all delivered, in such batches.
_DRAIN_BATCH = 1024

# Native methods that accept `return_consumed_capacity`.
_CAPACITY_METHODS = frozenset(
    {
        "get_item",
        "put_item",
        "update_item",
        "delete_item",
        "query",
        "scan",
        "batch_get_item",
        "batch_write_item",
        "batch_get",
        "batch_write",
        "transact_get_items",
        "transact_write_items",
        "execute_statement",
        "batch_execute_statement",
        "execute_transaction",
    }
)


def _register_fork_hook() -> None:
    global _FORK_HOOK_REGISTERED
    if not _FORK_HOOK_REGISTERED and hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_core._reset_after_fork)
        _FORK_HOOK_REGISTERED = True


@dataclass(frozen=True, slots=True)
class Credentials:
    """Static AWS credentials (prefer the default chain: environment, profiles, SSO, roles).

    Attributes:
        access_key_id: Access key id.
        secret_access_key: Secret access key (hidden in ``repr``).
        session_token: Session token of temporary credentials (hidden in ``repr``).
    """

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __repr__(self) -> str:
        token = ", session_token='***'" if self.session_token else ""
        return f"Credentials(access_key_id={self.access_key_id!r}, secret_access_key='***'{token})"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How failed requests are retried (throttling, 5xx errors, transaction conflicts, timeouts).

    Attributes:
        max_attempts: Attempts per request, the first included (1 disables retries).
        base_delay: Seconds (or a ``timedelta``) before the first retry; doubles each time, with
            full jitter. Stored as float seconds.
        max_delay: Longest delay between attempts, in seconds (or a ``timedelta``).
        retry_timeouts: Retry requests that timed out or lost their connection. Such a write may
            have been applied already: transactions (with their idempotency token) and writes
            that apply once even when sent twice are sent again; other writes, and answers that
            the earlier attempt may have caused (a failed condition, old values), raise
            ``WriteOutcomeUnknownError``. When ``False``, nothing is sent again after a
            timeout or a lost connection: writes raise ``WriteOutcomeUnknownError``, reads,
            batch writes and table operations a retryable ``TransportError``.
    """

    max_attempts: int = 4
    base_delay: float | timedelta = 0.05
    max_delay: float | timedelta = 5.0
    retry_timeouts: bool = True

    def __post_init__(self) -> None:
        count(self.max_attempts, "max_attempts", hint=" (1 disables retries)")
        # Durations are kept as float seconds, as the core takes them.
        base_delay = seconds(self.base_delay, "base_delay", allow_zero=True)
        max_delay = seconds(self.max_delay, "max_delay", allow_zero=True)
        object.__setattr__(self, "base_delay", base_delay)
        object.__setattr__(self, "max_delay", max_delay)
        if max_delay < base_delay:
            raise ValueError(f"max_delay ({max_delay}) must be >= base_delay ({base_delay})")

    @classmethod
    def none(cls) -> RetryPolicy:
        """No retries."""
        return cls(max_attempts=1)

    def to_core(self) -> dict[str, Any]:
        """The core configuration (``RetryConfig`` in ``crates/dynamoxy-core/src/config.rs``)."""
        return {
            "max_attempts": self.max_attempts,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "retry_timeouts": self.retry_timeouts,
        }


def _items_of(operation: str, params: Mapping[str, Any], output: Any) -> int:
    if not isinstance(output, dict):
        return 0
    if operation == "GetItem":
        return int(output.get("item") is not None)
    if operation in ("Query", "Scan"):
        return int(output.get("count", 0))
    if operation in ("PutItem", "UpdateItem", "DeleteItem"):
        return 1
    if operation == "BatchGetItem":
        found = output.get("items") or output.get("responses") or {}
        return sum(len(items) for items in found.values())
    if operation == "BatchWriteItem":
        requests = params.get("requests")
        if requests is not None:  # a managed batch: the writes made
            return len(requests) - len(output.get("unprocessed", ()))
        return sum(len(entries) for entries in params.get("request_items", {}).values())
    if operation == "TransactGetItems":
        return sum(item is not None for item in output.get("responses", ()))
    if operation == "TransactWriteItems":
        return len(params.get("transact_items", ()))
    if operation == "ExecuteStatement":
        return len(output.get("items", ()))
    return 0


_PARTIQL = frozenset({"ExecuteStatement", "BatchExecuteStatement", "ExecuteTransaction"})


#: Operations whose items are returned rows (``db.client.response.returned_rows``): reads, and
#: PartiQL ``SELECT`` statements. The items of writes are not rows.
_RETURNS_ROWS = frozenset({"GetItem", "Query", "Scan", "BatchGetItem", "TransactGetItems", "ExecuteStatement"})


def _returns_rows(operation: str, params: Mapping[str, Any]) -> bool:
    """Whether the items of a successful call are returned rows (a read, not a write)."""
    return operation in _RETURNS_ROWS and not _statement_writes(operation, params)


def _statement_writes(operation: str, params: Mapping[str, Any]) -> bool | None:
    """Whether a PartiQL request writes (``None`` for other operations): any statement but ``SELECT``."""
    if operation not in _PARTIQL:
        return None
    if operation == "ExecuteStatement":
        texts = [params.get("statement", "")]
    else:
        texts = [statement.get("statement", "") for statement in params.get("statements", ())]
    return any(not str(text).lstrip()[:6].upper().startswith("SELECT") for text in texts)


def _unprocessed(method: str, output: Any) -> list[tuple[str, dict[str, Any]]]:
    """What a managed batch (``batch_write``, ``batch_get``) left unprocessed after every round."""
    if not isinstance(output, dict):
        return []
    if method == "batch_write":
        return [(kind, item) for _, kind, item in output.get("unprocessed", ())]
    if method == "batch_get":
        return [("get", key) for keys in output.get("unprocessed_keys", {}).values() for key in keys.get("keys", ())]
    return []


def _capacities(output: Any) -> list[ConsumedCapacity]:
    if not isinstance(output, dict):
        return []
    raw = output.get("consumed_capacity")
    if not raw:
        return []
    reports = raw if isinstance(raw, list) else [raw]
    return [ConsumedCapacity.from_core(report) for report in reports]


class Client:
    """Entry point of dynamoxy. Create one per process (after any ``fork()``) and share it.

    Nothing happens on construction: the client connects on first use or in ``async with``.
    Credentials and region come from the standard AWS chain unless given.

    Example::

        async with dynamoxy.Client(region="eu-west-1") as client:
            users = client.model(User)
            await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))

    Args:
        region: AWS region (default: from the environment or profile).
        endpoint_url: A DynamoDB-compatible endpoint (DynamoDB Local, MiniStack, VPC endpoint).
        profile: Named AWS profile.
        credentials: Static credentials (default: the AWS credential chain).
        timeout: Time limit of one attempt of a request (seconds or ``timedelta``).
        connect_timeout: Time limit to establish a connection.
        retry: Retry policy for throttling, server errors, conflicts and timeouts.
        table_prefix: Prefix added to every table name of models and ``client.table()``
            (``"prod-"``), to separate environments sharing an account.
        table_names: Explicit physical names for logical table names (overrides the prefix).
        consumed_capacity: Capacity reporting requested from DynamoDB: ``"TOTAL"`` (default;
            powers metrics, spans and ``collect()``), ``"INDEXES"`` or ``"NONE"``.
        batch_concurrency: Requests of one managed batch sent in parallel.
        batch_max_rounds: Rounds for unprocessed batch items before they are reported.
        slow_operation_threshold: Calls slower than this emit ``request.slow``.
        telemetry: OpenTelemetry settings (``None`` disables spans and OpenTelemetry metrics).
        logging: How events are logged (default: every event to ``dynamoxy.*`` loggers).
        event_buffer: Core events buffered for hooks before new ones are dropped.
        store: Use this in-memory DynamoDB instead of AWS (see ``dynamoxy.testing``).
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        profile: str | None = None,
        credentials: Credentials | None = None,
        timeout: float | timedelta = 10.0,
        connect_timeout: float | timedelta = 3.1,
        retry: RetryPolicy | None = None,
        table_prefix: str = "",
        table_names: Mapping[str, str] | None = None,
        consumed_capacity: Literal["NONE", "TOTAL", "INDEXES"] = "TOTAL",
        batch_concurrency: int = 4,
        batch_max_rounds: int = 8,
        slow_operation_threshold: float | timedelta | None = None,
        telemetry: Telemetry | None = Telemetry(),  # noqa: B008 - frozen dataclass
        logging: LoggingConfig | None = None,
        event_buffer: int = 10_000,
        store: MemoryStore | None = None,
    ) -> None:
        if endpoint_url is not None and not endpoint_url.startswith(("http://", "https://")):
            raise ValueError(f"endpoint_url must start with http:// or https://, got {endpoint_url!r}")
        if consumed_capacity not in ("NONE", "TOTAL", "INDEXES"):
            raise ValueError(f"consumed_capacity must be 'NONE', 'TOTAL' or 'INDEXES', got {consumed_capacity!r}")
        if retry is not None and not isinstance(retry, RetryPolicy):
            raise TypeError(f"retry must be a RetryPolicy, got {type(retry).__name__}")
        count(batch_concurrency, "batch_concurrency")
        count(batch_max_rounds, "batch_max_rounds")
        count(event_buffer, "event_buffer")
        if store is not None and endpoint_url is not None:
            raise ValueError("pass either store= (in-memory DynamoDB) or endpoint_url=, not both")
        _register_fork_hook()
        self.region = region
        self.endpoint_url = endpoint_url
        self.profile = profile
        self.credentials = credentials
        self.timeout = seconds(timeout, "timeout")
        self.connect_timeout = seconds(connect_timeout, "connect_timeout")
        self.retry = retry if retry is not None else RetryPolicy()
        self.table_prefix = table_prefix
        self.table_names = dict(table_names or {})
        self.consumed_capacity = consumed_capacity
        self.batch_concurrency = batch_concurrency
        self.batch_max_rounds = batch_max_rounds
        self.slow_operation_threshold = seconds(slow_operation_threshold, "slow_operation_threshold")
        self.telemetry = telemetry
        self.event_buffer = event_buffer
        self.store = store
        self.events = EventDispatcher(logging)
        self.events.on_change(self._update_event_filter)
        self.hooks = HookRegistry()
        self._middleware: list[Middleware] = []
        self._recorder = TelemetryRecorder(telemetry, region=region, endpoint=endpoint_url)
        self._core_client: CoreClient | None = None
        self._connect_lock = LoopLocal(asyncio.Lock)
        self._key_attribute_cache: dict[tuple[str, str | None], tuple[str, ...]] = {}
        self._models: dict[type, Repository[Any]] = {}
        self._pump_task: asyncio.Task[None] | None = None
        self._entered = 0
        self._closed = False
        self._closing = False

    def __repr__(self) -> str:
        target = "memory" if self.store is not None else (self.endpoint_url or self.region or "aws")
        return f"<dynamoxy.Client {target}>"

    def to_core(self) -> dict[str, Any]:
        """The core configuration (``ClientConfig`` in ``crates/dynamoxy-core/src/config.rs``)."""
        credentials = self.credentials
        return {
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "profile": self.profile,
            "access_key_id": credentials.access_key_id if credentials else None,
            "secret_access_key": credentials.secret_access_key if credentials else None,
            "session_token": credentials.session_token if credentials else None,
            "connect_timeout": self.connect_timeout,
            "timeout": self.timeout,
            "retry": self.retry.to_core(),
            "batch_max_rounds": self.batch_max_rounds,
            "batch_concurrency": self.batch_concurrency,
            "slow_operation_threshold": self.slow_operation_threshold or 0.0,
            "event_capacity": self.event_buffer,
        }

    # ---- lifecycle ------------------------------------------------------------------------

    @property
    def backend(self) -> Literal["memory", "aws"]:
        """``"memory"`` for in-memory clients, ``"aws"`` otherwise."""
        return "memory" if self.store is not None else "aws"

    async def connect(self) -> Self:
        """Connects now (otherwise the first operation does). Safe to call repeatedly."""
        await self._core()
        return self

    async def _core(self) -> CoreClient:
        if self._closed:
            raise _core.ShutdownError("this dynamoxy client is closed; create a new Client")
        core = self._core_client
        if core is not None:
            return core
        async with self._connect_lock.get():
            if self._core_client is None:
                if self.store is not None:
                    core = CoreClient.memory(self.to_core(), self.store)
                else:
                    core = await CoreClient.connect(self.to_core())
                self._core_client = core
                self._update_event_filter()
                self.events.emit(
                    EventKind.CLIENT_CONNECTED,
                    f"connected to {'the in-memory DynamoDB' if self.store is not None else 'DynamoDB'}",
                    {"backend": core.backend, "region": self.region or "", "endpoint": self.endpoint_url or ""},
                )
        assert self._core_client is not None
        return self._core_client

    def _update_event_filter(self) -> None:
        if self._core_client is not None:
            self._core_client.set_event_filter([kind.value for kind in self.events.wanted()])

    async def __aenter__(self) -> Self:
        core = await self._core()
        self._entered += 1
        if self._pump_task is None:
            self._pump_task = asyncio.get_running_loop().create_task(self._pump(core), name="dynamoxy-events")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._entered -= 1
        if self._entered <= 0:
            await self.close()

    async def close(self) -> None:
        """Waits for async hooks, delivers the remaining events and refuses further calls.

        Calls in progress finish, retries included. Async hooks of this event loop that are still
        running may use the client until they finish (an ``operation.failed`` hook that writes an
        audit item, say): the client closes after them. A hook that calls ``close()`` itself is
        not waited for, and a ``close()`` while another one waits for hooks returns at once.
        """
        if self._closed or self._closing:
            return
        self._closing = True
        try:
            # Deliver what the core buffered, and wait for the hooks it starts, until none is left.
            while True:
                if self._core_client is not None:
                    self._dispatch_buffered(self._core_client)
                pending = self.events.pending()
                if not pending:
                    break
                await asyncio.wait(pending)
        finally:
            self._closing = False
        self._closed = True
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        core = self._core_client
        if core is not None:
            self._dispatch_buffered(core)
            core.close()
        self._models.clear()
        self.events.emit(EventKind.CLIENT_CLOSED, "client closed")
        await self.events.drain()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called."""
        return self._closed

    async def _pump(self, core: CoreClient) -> None:
        # Delivers events while calls are still running (retries, batch rounds). Events are only
        # ever taken synchronously here and in _dispatch_buffered, so their order is kept.
        try:
            while True:
                await core.wait_events(1.0)
                self._dispatch_buffered(core)
        except asyncio.CancelledError:
            self._dispatch_buffered(core)
            raise

    def _dispatch_raw(self, raw: list[tuple[str, str, dict[str, str]]]) -> None:
        if raw:
            self.events.dispatch(raw)

    def _report_dropped(self, core: CoreClient) -> None:
        dropped = core.take_dropped_events()
        if dropped:
            self.events.emit(
                EventKind.EVENTS_DROPPED,
                f"{dropped} events were dropped because the buffer was full",
                {"dropped": dropped, "event_buffer": self.event_buffer},
            )

    def _dispatch_buffered(self, core: CoreClient) -> None:
        # Runs after every call too, so a call's events reach hooks before the call returns: all
        # of them, in batches (the buffer holds at most `event_buffer` events).
        while True:
            raw = core.drain_events(_DRAIN_BATCH)
            self._dispatch_raw(raw)
            if len(raw) < _DRAIN_BATCH:
                break
        self._report_dropped(core)

    # ---- extension points -----------------------------------------------------------------

    @overload
    def on(self, pattern: str | EventKind) -> Callable[[EventHook], EventHook]: ...
    @overload
    def on(self, pattern: str | EventKind, hook: EventHook) -> Callable[[], None]: ...
    def on(self, pattern: str | EventKind, hook: EventHook | None = None) -> Any:
        """Calls ``hook`` for events matching ``pattern``.

        ``pattern`` is a kind such as ``"request.retry"`` or a glob such as ``"request.*"``. Works
        as a decorator; otherwise returns an unsubscribe function.

        Hooks may be sync or async; a failing hook emits ``hook.failed`` and never breaks a call.
        """
        if hook is None:

            def decorator(function: EventHook) -> EventHook:
                self.events.subscribe(pattern, function)
                return function

            return decorator
        return self.events.subscribe(pattern, hook)

    def use(self, middleware: Middleware) -> Callable[[], None]:
        """Adds middleware around every DynamoDB call (first added runs outermost).

        A call runs the middleware registered when it starts: adding or removing one (also from
        inside a middleware, or from another task) applies to calls that start afterwards.

        Returns:
            A function that removes it.
        """
        self._middleware.append(middleware)

        def remove() -> None:
            if middleware in self._middleware:
                self._middleware.remove(middleware)

        return remove

    def hook(self, point: HookPoint, model: type | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator registering a lifecycle hook for ``model`` (or every model).

        Points: ``before_save``, ``after_save``, ``before_update``, ``after_update``,
        ``before_delete``, ``after_delete`` and ``after_load``. The hook receives
        ``(instance, context)`` and may be sync or async (``after_load`` hooks must be sync).

        Example::

            @client.hook("before_save", User)
            def normalize(user: User, context: dynamoxy.HookContext) -> None:
                user.email = user.email.lower()
        """

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            self.hooks.add(point, function, model)
            return function

        return decorator

    def hook_context(self, operation: str, model: type) -> HookContext:
        """The context passed to client-level hooks."""
        return HookContext(operation=operation, model=model, client=self)

    # ---- calls ----------------------------------------------------------------------------

    def table_name(self, logical: str) -> str:
        """The physical name of a logical table name (``table_names``, else ``table_prefix``)."""
        return self.table_names.get(logical, f"{self.table_prefix}{logical}")

    async def call(
        self,
        method: str,
        operation: str,
        *,
        table: str | None = None,
        model: type | None = None,
        **params: Any,
    ) -> Any:
        """Performs one native call through middleware, telemetry and events.

        Low-level: repositories and tables use it. ``method`` is a ``CoreClient`` method and
        ``params`` its keyword arguments.
        """
        if method in _CAPACITY_METHODS and "return_consumed_capacity" not in params:
            params["return_consumed_capacity"] = self.consumed_capacity
        if not self._middleware:
            output, _ = await self._perform(method, operation, table, model, params)
            return output
        if self._core_client is None or self._closed:
            await self._core()
        request = Request(operation=operation, method=method, table=table, params=params, model=model)
        # The chain as the call starts: middleware added or removed meanwhile (a one-shot
        # middleware removing itself, another task) applies to later calls only.
        response = await self._chain(tuple(self._middleware), 0, request)
        return response.output

    async def _chain(self, chain: tuple[Middleware, ...], index: int, request: Request) -> Response:
        if index < len(chain):
            return await chain[index](request, lambda next_request: self._chain(chain, index + 1, next_request))
        return await self._invoke(request)

    async def _invoke(self, request: Request) -> Response:
        output, stats = await self._perform(
            request.method, request.operation, request.table, request.model, request.params
        )
        return Response(output=output, stats=CallStats.from_core(stats))

    async def _perform(
        self, method: str, operation: str, table: str | None, model: type | None, params: dict[str, Any]
    ) -> tuple[Any, tuple[int, int, int, float]]:
        core = self._core_client
        if core is None or self._closed:
            core = await self._core()
        if self._recorder.recording_tracer() is not None:
            return await self._perform_traced(core, method, operation, table, model, params)
        started = time.perf_counter()
        try:
            output, stats = await getattr(core, method)(**params)
        except DynamoxyError as error:
            self._failed(core, operation, table, model, started, error)
            raise
        incomplete = _incomplete(method, output, stats)
        if incomplete is not None:
            self._finish(core, _success_record(operation, table, model, params, output, stats, incomplete), incomplete)
            return output, stats
        self._succeeded(core, operation, table, model, params, output, stats)
        return output, stats

    async def _perform_traced(
        self,
        core: CoreClient,
        method: str,
        operation: str,
        table: str | None,
        model: type | None,
        params: dict[str, Any],
    ) -> tuple[Any, tuple[int, int, int, float]]:
        started = time.perf_counter()
        native = getattr(core, method)
        with self._recorder.span(operation, table, params) as span:
            try:
                output, stats = await native(**params)
            except DynamoxyError as error:
                entry = _failure_record(operation, table, model, started, error)
                self._recorder.finish_span(span, None, entry, error)
                self._finish(core, entry, error)
                raise
            incomplete = _incomplete(method, output, stats)
            entry = _success_record(operation, table, model, params, output, stats, incomplete)
            self._recorder.finish_span(span, output, entry, incomplete)
        self._finish(core, entry, incomplete, returns_rows=_returns_rows(operation, params))
        return output, stats

    def _succeeded(
        self,
        core: CoreClient,
        operation: str,
        table: str | None,
        model: type | None,
        params: dict[str, Any],
        output: Any,
        stats: tuple[int, int, int, float],
    ) -> None:
        # Like _finish, but the record is only built when metrics, collectors or events want it.
        entry: OperationRecord | None = None
        if self._recorder.recording_metrics():
            entry = _success_record(operation, table, model, params, output, stats)
            self._recorder.record_metrics(entry, returns_rows=_returns_rows(operation, params))
        collectors = active_collectors()
        if collectors:
            if entry is None:
                entry = _success_record(operation, table, model, params, output, stats)
            for collector in collectors:
                collector.records.append(entry)
        self._dispatch_buffered(core)
        if self.events.wants(EventKind.OPERATION_SUCCEEDED):
            if entry is None:
                entry = _success_record(operation, table, model, params, output, stats)
            self._emit_succeeded(entry)

    def _failed(
        self,
        core: CoreClient,
        operation: str,
        table: str | None,
        model: type | None,
        started: float,
        error: DynamoxyError,
    ) -> None:
        entry: OperationRecord | None = None
        if self._recorder.recording_metrics():
            entry = _failure_record(operation, table, model, started, error)
            self._recorder.record_metrics(entry)
        collectors = active_collectors()
        if collectors:
            if entry is None:
                entry = _failure_record(operation, table, model, started, error)
            for collector in collectors:
                collector.records.append(entry)
        self._dispatch_buffered(core)
        if self.events.wants(EventKind.OPERATION_FAILED):
            if entry is None:
                entry = _failure_record(operation, table, model, started, error)
            self._emit_failed(entry, error)

    def _finish(
        self, core: CoreClient, entry: OperationRecord, error: DynamoxyError | None, *, returns_rows: bool = False
    ) -> None:
        self._recorder.record_metrics(entry, returns_rows=returns_rows)
        for collector in active_collectors():
            collector.records.append(entry)
        self._dispatch_buffered(core)
        if error is None:
            if self.events.wants(EventKind.OPERATION_SUCCEEDED):
                self._emit_succeeded(entry)
        elif self.events.wants(EventKind.OPERATION_FAILED):
            self._emit_failed(entry, error)

    def _emit_succeeded(self, entry: OperationRecord) -> None:
        self.events.emit(
            EventKind.OPERATION_SUCCEEDED,
            f"{entry.operation} {entry.table or ''}".rstrip() + f" took {entry.duration * 1000:.1f} ms",
            _fields(entry),
        )

    def _emit_failed(self, entry: OperationRecord, error: DynamoxyError) -> None:
        fields = _fields(entry)
        reasons = getattr(error, "reasons", None)
        if reasons:
            fields["reasons"] = ",".join(str(reason[0]) for reason in reasons)
        self.events.emit(
            EventKind.OPERATION_FAILED,
            f"{entry.operation} {entry.table or ''}".rstrip() + f" failed: {entry.error}",
            fields,
        )

    async def _key_attributes(self, table: str, index: str | None) -> tuple[str, ...]:
        """The key attributes of an index entry (table key, then index key) of a physical table.

        Dict-level reads use them to continue right after the last item of a page cut short by
        ``limit()``. Looked up once per table and index (``DescribeTable``).
        """
        cached = self._key_attribute_cache.get((table, index))
        if cached is not None:
            return cached
        from dynamoxy.results import TableDescription

        description = TableDescription.from_core(
            await self.call("describe_table", "DescribeTable", table=table, table_name=table)
        )
        names = [description.partition_key, *([description.sort_key] if description.sort_key else [])]
        if index is not None:
            found = description.indexes.get(index)
            if found is None:
                raise ValueError(f"table {table} has no index {index!r}")
            names += [name for name in (found.partition_key, found.sort_key) if name and name not in names]
        resolved = self._key_attribute_cache[(table, index)] = tuple(names)
        return resolved

    # ---- factories ------------------------------------------------------------------------

    def model[M](self, model: type[M]) -> Repository[M]:
        """The typed operations of a model on this client.

        ``client.model(User)`` reads, queries and writes ``User`` items: ``get``, ``query``,
        ``create``, ``save``, ``update``, ``delete``, ... It is a plain
        :class:`dynamoxy.Repository`; subclass ``Repository[User]`` to add domain queries.
        The client builds it on first use, without I/O, and returns the same one afterwards, so
        calling it where you need it costs a dictionary lookup. ``User.create(client, ...)``,
        ``User.get(client, ...)`` and ``User.query(client, ...)`` go through it.

        Example::

            users = client.model(User)
            user = await users.get("u1")
            orders = await client.model(Order).query(customer_id="c1").all()

        Raises:
            TypeError: ``model`` is not a dynamoxy model class, or not one stored in a table
                (abstract, or no ``table=``).
        """
        try:
            return self._models[model]
        except (KeyError, TypeError):  # not built yet, or not a class (instances are unhashable)
            pass
        return self._build_model(model)  # outside the handler: its errors carry no KeyError context

    def _build_model[M](self, model: type[M]) -> Repository[M]:
        if not isinstance(model, type):
            raise TypeError(
                f"client.model() takes a model class, such as client.model(User), got an instance of "
                f"{type(model).__name__}"
            )
        from dynamoxy.repository import Repository

        repository = Repository(self, model)
        if not self._closed:  # a closed client keeps nothing (its calls raise ShutdownError)
            self._models[model] = repository
        return repository

    def table(self, name: str) -> Table:
        """Dict-level access to a table by its logical name (prefix and mapping apply)."""
        from dynamoxy.table import Table

        return Table(self, name)

    # The overloads give mypy the union of the models; with ``*models: type[M]`` alone it infers
    # their common base class (pyright and pyrefly infer the union either way).
    @overload
    def collection[A, B](self, a: type[A], b: type[B], /) -> Collection[A | B]: ...
    @overload
    def collection[A, B, C](self, a: type[A], b: type[B], c: type[C], /) -> Collection[A | B | C]: ...
    @overload
    def collection[A, B, C, D](
        self, a: type[A], b: type[B], c: type[C], d: type[D], /
    ) -> Collection[A | B | C | D]: ...
    @overload
    def collection[A, B, C, D, E](
        self, a: type[A], b: type[B], c: type[C], d: type[D], e: type[E], /
    ) -> Collection[A | B | C | D | E]: ...
    @overload
    def collection[A, B, C, D, E, F](
        self, a: type[A], b: type[B], c: type[C], d: type[D], e: type[E], f: type[F], /
    ) -> Collection[A | B | C | D | E | F]: ...
    @overload
    def collection[M](self, *models: type[M]) -> Collection[M]: ...
    def collection(self, *models: type[Any]) -> Collection[Any]:
        """Queries over several models sharing a table (single-table design).

        Each item is decoded into its model by the type attribute.
        """
        from dynamoxy.collection import Collection

        return Collection(self, models)

    def transaction(self, *, token: str | None = None) -> Transaction:
        """A write transaction: all operations succeed or none.

        It commits when the ``async with`` block exits without an exception.

        Args:
            token: Idempotency token (``ClientRequestToken``, 1 to 36 characters, unique per logical
                operation across the account); generated when omitted. Retrying an identical
                transaction with the same token within 10 minutes does not apply it twice.

        Raises:
            ValueError: The token is empty or longer than 36 characters.
        """
        from dynamoxy.transactions import Transaction

        return Transaction(self, token=token)

    # Overloads type each item of up to six reads (``customer, order = await ...``).
    @overload
    async def transact_get[A](self, a: Get[A], /) -> tuple[A | None]: ...
    @overload
    async def transact_get[A, B](self, a: Get[A], b: Get[B], /) -> tuple[A | None, B | None]: ...
    @overload
    async def transact_get[A, B, C](
        self, a: Get[A], b: Get[B], c: Get[C], /
    ) -> tuple[A | None, B | None, C | None]: ...
    @overload
    async def transact_get[A, B, C, D](
        self, a: Get[A], b: Get[B], c: Get[C], d: Get[D], /
    ) -> tuple[A | None, B | None, C | None, D | None]: ...
    @overload
    async def transact_get[A, B, C, D, E](
        self, a: Get[A], b: Get[B], c: Get[C], d: Get[D], e: Get[E], /
    ) -> tuple[A | None, B | None, C | None, D | None, E | None]: ...
    @overload
    async def transact_get[A, B, C, D, E, F](
        self, a: Get[A], b: Get[B], c: Get[C], d: Get[D], e: Get[E], f: Get[F], /
    ) -> tuple[A | None, B | None, C | None, D | None, E | None, F | None]: ...
    @overload
    async def transact_get(self, *gets: Get[Any]) -> tuple[Any, ...]: ...
    async def transact_get(self, *gets: Get[Any]) -> tuple[Any, ...]:
        """Reads up to 100 items in one consistent snapshot (``TransactGetItems``).

        Example::

            customer, order = await client.transact_get(
                dynamoxy.Get(Customer, "c1"), dynamoxy.Get(Order, customer_id="c1", order_id="o1")
            )

        Returns:
            A tuple of one model instance (or ``None``) per :class:`dynamoxy.Get`, in order.
        """
        from dynamoxy.transactions import transact_get

        return await transact_get(self, gets)

    def batch_writer(self, *, flush_size: int = 100) -> BatchWriter:
        """Buffers puts and deletes and writes them with managed batches (``async with``)."""
        from dynamoxy.batch import BatchWriter

        return BatchWriter(self, flush_size=flush_size)

    # ---- tables of models -----------------------------------------------------------------

    async def create_tables(
        self,
        *models: type,
        billing: Billing | None = None,
        wait: bool = True,
        exist_ok: bool = False,
        stream: str | None = None,
        deletion_protection: bool = False,
        tags: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Creates the tables of ``models`` (models sharing a table are merged).

        Also enables TTL on their TTL attributes and waits until the tables are active.

        Args:
            models: Model classes.
            billing: Billing mode (default on-demand).
            wait: Wait until each table is active. Tables with a TTL attribute are waited for
                anyway, since DynamoDB enables Time to Live on active tables only.
            exist_ok: Skip tables that exist instead of raising ``ResourceInUseError``, also tables
                that another writer creates at the same time (waited for with ``wait``).
            stream: Enable a DynamoDB stream with this view type (``"NEW_AND_OLD_IMAGES"``, ...).
            deletion_protection: Turn on deletion protection.
            tags: Tags for every table.

        Returns:
            The physical names of the tables created.
        """
        from dynamoxy.table import definitions_for

        created: list[str] = []
        for definition in definitions_for(models):
            table = self.table(definition.name)
            if exist_ok and await table.exists():
                continue
            try:
                await table.create_from(
                    definition,
                    billing=billing,
                    wait=wait,
                    stream=stream,
                    deletion_protection=deletion_protection,
                    tags=tags,
                )
            except ResourceInUseError as error:
                if not exist_ok or error.operation != "CreateTable":
                    raise
                # Another writer (a replica starting at the same time) created it since exists().
                if wait:
                    await table.wait_until_active()
                continue
            created.append(table.physical_name)
        return created

    async def ensure_tables(self, *models: type, billing: Billing | None = None, wait: bool = True) -> list[str]:
        """Creates missing tables, global secondary indexes and TTL settings.

        Indexes are added one at a time, as DynamoDB requires. Never deletes anything. A table
        that another writer creates at the same time (replicas starting together) is checked like
        an existing one.

        Returns:
            The physical names of the tables that were created or changed.

        Raises:
            TypeError: A table's key, the key of an index with the same name (their attributes
                or types) or the TTL attribute differs from the models, or an index projects
                fewer attributes than the models declare (none of them can change in place).
                Raised before that table is changed (tables earlier in the call may already
                be); an index that projects more is accepted.
        """
        from dynamoxy.table import definitions_for

        changed: list[str] = []
        for definition in definitions_for(models):
            table = self.table(definition.name)
            if not await table.exists():
                try:
                    await table.create_from(definition, billing=billing, wait=wait)
                except ResourceInUseError as error:
                    if error.operation != "CreateTable":
                        raise
                    # Another writer created it since exists(): check it like an existing table.
                    if wait:
                        await table.wait_until_active()
                else:
                    changed.append(table.physical_name)
                    continue
            if await table.sync_with(definition, wait=wait):
                changed.append(table.physical_name)
        return changed

    async def delete_tables(self, *models: type, wait: bool = True, missing_ok: bool = True) -> None:
        """Deletes the tables of ``models``."""
        from dynamoxy.table import definitions_for

        for definition in definitions_for(models):
            await self.table(definition.name).delete_table(wait=wait, missing_ok=missing_ok)

    async def list_tables(self) -> list[str]:
        """Every table name of the account and region (all pages)."""
        names: list[str] = []
        start: str | None = None
        while True:
            output = await self.call("list_tables", "ListTables", exclusive_start_table_name=start)
            names.extend(output["table_names"])
            start = output["last_evaluated_table_name"]
            if start is None:
                return names

    # ---- PartiQL --------------------------------------------------------------------------

    async def execute(self, statement: str, *parameters: Any, consistent: bool = False) -> list[dict[str, Any]]:
        """Runs a PartiQL statement (``?`` placeholders) and returns every item (all pages).

        Example::

            rows = await client.execute('SELECT * FROM "users" WHERE user_id = ?', "u1")
        """
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            output = await self.call(
                "execute_statement",
                "ExecuteStatement",
                statement=statement,
                parameters=list(parameters),
                consistent_read=consistent,
                next_token=token,
            )
            items.extend(output["items"])
            token = output["next_token"]
            if token is None:
                return items

    async def execute_transaction(
        self, statements: Sequence[tuple[str, Sequence[Any]]], *, token: str | None = None
    ) -> list[dict[str, Any] | None]:
        """Runs PartiQL statements as one transaction (all reads or all writes).

        Args:
            statements: ``(statement, parameters)`` pairs.
            token: Idempotency token, 1 to 36 characters (generated for writes when omitted).
        """
        from dynamoxy.transactions import check_token

        check_token(token)
        output = await self.call(
            "execute_transaction",
            "ExecuteTransaction",
            statements=[
                {"statement": statement, "parameters": list(parameters)} for statement, parameters in statements
            ],
            client_request_token=token,
        )
        return list(output["responses"])

    async def batch_execute(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[dict[str, Any]]:
        """Runs up to 25 PartiQL statements; each succeeds or fails on its own.

        Returns:
            One dict per statement: ``{"error": (code, message, item) | None, "table_name": ..., "item": ...}``.
        """
        output = await self.call(
            "batch_execute_statement",
            "BatchExecuteStatement",
            statements=[
                {"statement": statement, "parameters": list(parameters)} for statement, parameters in statements
            ],
        )
        return [dict(response) for response in output["responses"]]

    # ---- observability --------------------------------------------------------------------

    def metrics(self) -> MetricsSnapshot:
        """Counters kept by the core since the client connected (or :meth:`reset_metrics`)."""
        if self._core_client is None:
            return MetricsSnapshot(operations={}, in_flight=0, latency_buckets_ms=())
        return MetricsSnapshot.from_core(self._core_client.metrics())

    def reset_metrics(self) -> None:
        """Resets the counters of :meth:`metrics`."""
        if self._core_client is not None:
            self._core_client.reset_metrics()

    @contextlib.contextmanager
    def collect(self) -> Iterator[Collector]:
        """Collects every operation made in the block (see :func:`dynamoxy.collect`)."""
        with collect() as collector:
            yield collector


def _incomplete(method: str, output: Any, stats: tuple[int, int, int, float]) -> UnprocessedItemsError | None:
    """The error of a managed batch that left items unprocessed, which its caller raises.

    The call is recorded as failed with it (collectors, metrics, ``operation.failed``, the span),
    although the response came back.
    """
    unprocessed = _unprocessed(method, output)
    if not unprocessed:
        return None
    error = unprocessed_items_error("keys" if method == "batch_get" else "writes", unprocessed, output.get("error"))
    error.attempts, error.throttles, error.requests, error.duration = stats
    return error


def _success_record(
    operation: str,
    table: str | None,
    model: type | None,
    params: Mapping[str, Any],
    output: Any,
    stats: tuple[int, int, int, float],
    incomplete: UnprocessedItemsError | None = None,
) -> OperationRecord:
    read, write = capacity_split(operation, _capacities(output), writes=_statement_writes(operation, params))
    return OperationRecord(
        operation=operation,
        table=table,
        model=model.__name__ if model else None,
        duration=stats[3],
        attempts=stats[0],
        throttles=stats[1],
        requests=stats[2],
        read_capacity_units=read,
        write_capacity_units=write,
        items=_items_of(operation, params, output),
        error=None if incomplete is None else type(incomplete).__name__,
        timestamp=time.time(),
    )


def _failure_record(
    operation: str, table: str | None, model: type | None, started: float, error: DynamoxyError
) -> OperationRecord:
    return OperationRecord(
        operation=operation,
        table=table,
        model=model.__name__ if model else None,
        duration=error.duration if error.duration is not None else time.perf_counter() - started,
        attempts=error.attempts or 1,
        throttles=getattr(error, "throttles", None) or 0,
        requests=getattr(error, "requests", None) or 1,
        read_capacity_units=0.0,
        write_capacity_units=0.0,
        items=0,
        error=error.code or type(error).__name__,
        timestamp=time.time(),
    )


def _fields(entry: OperationRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "operation": entry.operation,
        "duration_ms": round(entry.duration * 1000, 3),
        "attempts": entry.attempts,
    }
    if entry.table:
        fields["table"] = entry.table
    if entry.model:
        fields["model"] = entry.model
    if entry.read_capacity_units:
        fields["read_units"] = entry.read_capacity_units
    if entry.write_capacity_units:
        fields["write_units"] = entry.write_capacity_units
    if entry.items:
        fields["items"] = entry.items
    if entry.error:
        fields["error"] = entry.error
    return fields
