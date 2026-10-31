"""Telemetry: OpenTelemetry traces and metrics, and on-demand collection.

**OpenTelemetry** (``pip install dynamoxy[otel]``): with ``Client(telemetry=Telemetry())`` (the
default) every DynamoDB call becomes a ``CLIENT`` span named like ``PutItem users`` with the
database and AWS semantic-convention attributes (``db.system.name``, ``db.operation.name``,
``db.collection.name``, ``aws.dynamodb.*``, ``aws.request_id``, ``error.type``), and records
the metrics ``db.client.operation.duration`` (histogram, seconds),
``db.client.response.returned_rows`` (histogram), ``dynamoxy.consumed_capacity`` (counter,
capacity units by ``capacity.type`` read/write), ``dynamoxy.request.attempts`` and
``dynamoxy.request.throttles`` (counters). Spans and metrics go wherever the application's
OpenTelemetry SDK sends them. Without the ``opentelemetry-api`` package nothing is recorded.

**On demand**: :func:`collect` gathers every operation made inside a block, for per-request
cost accounting or tests::

    with dynamoxy.collect() as usage:
        await users.get("u1")
    usage.read_capacity_units, usage.calls, usage.records

``client.metrics()`` returns counters kept by the Rust core since the client started.
"""

from __future__ import annotations

import contextlib
import json
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from dynamoxy.results import OperationRecord

__all__ = ["Collector", "Telemetry", "collect"]
_UNKNOWN: Any = object()


@dataclass(frozen=True, slots=True)
class Telemetry:
    """OpenTelemetry settings.

    Attributes:
        tracing: Create a span per DynamoDB call.
        metrics: Record OpenTelemetry metrics per call.
        tracer_provider: Tracer provider (default: the global one).
        meter_provider: Meter provider (default: the global one).
        record_expressions: Add the expressions (``db.query.text``; placeholders only, never
            values) to spans.
    """

    tracing: bool = True
    metrics: bool = True
    tracer_provider: Any = None
    meter_provider: Any = None
    record_expressions: bool = False

    @classmethod
    def disabled(cls) -> Telemetry:
        """No spans and no OpenTelemetry metrics."""
        return cls(tracing=False, metrics=False)


@dataclass(eq=False)
class Collector:
    """Operations made while it was active (see :func:`collect`).

    Attributes:
        records: Every operation, in completion order.
    """

    records: list[OperationRecord] = field(default_factory=list)

    @property
    def calls(self) -> int:
        """Number of operations."""
        return len(self.records)

    @property
    def requests(self) -> int:
        """Requests sent (managed batches send several per operation)."""
        return sum(record.requests for record in self.records)

    @property
    def duration(self) -> float:
        """Total seconds spent in operations."""
        return sum(record.duration for record in self.records)

    @property
    def read_capacity_units(self) -> float:
        """Read capacity units consumed."""
        return sum(record.read_capacity_units for record in self.records)

    @property
    def write_capacity_units(self) -> float:
        """Write capacity units consumed."""
        return sum(record.write_capacity_units for record in self.records)

    @property
    def errors(self) -> int:
        """Operations that failed."""
        return sum(record.error is not None for record in self.records)

    @property
    def retries(self) -> int:
        """Attempts beyond the first of each request (a managed batch sends several requests)."""
        return sum(max(record.attempts - record.requests, 0) for record in self.records)

    def by_operation(self) -> dict[str, int]:
        """Number of calls per operation."""
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.operation] = counts.get(record.operation, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        """A JSON-friendly summary (for logs and response headers)."""
        return {
            "calls": self.calls,
            "requests": self.requests,
            "duration_ms": round(self.duration * 1000, 3),
            "read_capacity_units": self.read_capacity_units,
            "write_capacity_units": self.write_capacity_units,
            "errors": self.errors,
            "retries": self.retries,
            "operations": self.by_operation(),
        }


_COLLECTORS: ContextVar[tuple[Collector, ...]] = ContextVar("dynamoxy_collectors", default=())


@contextlib.contextmanager
def collect() -> Iterator[Collector]:
    """Collects every dynamoxy operation made in the block (and in tasks it starts).

    Collectors nest: an operation is recorded by every active collector.

    Example::

        with dynamoxy.collect() as usage:
            await orders.query(customer_id="c1").all()
        logger.info("dynamodb usage", extra=usage.summary())
    """
    collector = Collector()
    token = _COLLECTORS.set((*_COLLECTORS.get(), collector))
    try:
        yield collector
    finally:
        _COLLECTORS.reset(token)


def record(entry: OperationRecord) -> None:
    """Adds an operation to the active collectors."""
    for collector in _COLLECTORS.get():
        collector.records.append(entry)


def active_collectors() -> tuple[Collector, ...]:
    """The collectors of the current context (an operation is recorded by each of them)."""
    return _COLLECTORS.get()


class TelemetryRecorder:
    """Creates spans and records OpenTelemetry metrics (no-op without ``opentelemetry-api``)."""

    def __init__(self, settings: Telemetry | None, *, region: str | None, endpoint: str | None) -> None:
        self.settings = settings
        self.region = region
        self.endpoint = endpoint
        self._server: tuple[str, int | None] | None = None
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.hostname:
                self._server = (parsed.hostname, parsed.port)
        self._tracer: Any = None
        self._instruments: dict[str, Any] | None = None
        self._otel: Any = None
        self._otel_metrics: Any = None
        self._trace_globals: dict[str, Any] = {}
        self._metrics_globals: dict[str, Any] = {}
        # The global proxy providers while no OpenTelemetry SDK is configured: their spans and
        # instruments record nothing, so the work around them is skipped until one is set.
        self._tracer_proxy: Any = None
        self._meter_proxy: Any = None
        if settings is None or not (settings.tracing or settings.metrics):
            return
        try:
            from opentelemetry import metrics, trace
        except ImportError:
            return
        from dynamoxy import __version__

        self._otel = trace
        self._otel_metrics = metrics
        # Where the API keeps the global providers (`set_*_provider`); the proxies check the same.
        self._trace_globals = vars(trace)
        self._metrics_globals = vars(sys.modules.get("opentelemetry.metrics._internal", metrics))
        if settings.tracing:
            self._tracer = trace.get_tracer("dynamoxy", __version__, tracer_provider=settings.tracer_provider)
            if settings.tracer_provider is None:
                provider = trace.get_tracer_provider()
                if isinstance(provider, trace.ProxyTracerProvider):
                    self._tracer_proxy = provider
        if settings.metrics:
            if settings.meter_provider is None:
                provider = metrics.get_meter_provider()
                if type(provider).__name__ == "_ProxyMeterProvider":
                    self._meter_proxy = provider
            meter = metrics.get_meter("dynamoxy", __version__, meter_provider=settings.meter_provider)
            self._instruments = {
                "duration": meter.create_histogram(
                    "db.client.operation.duration", unit="s", description="Duration of DynamoDB operations."
                ),
                "rows": meter.create_histogram(
                    "db.client.response.returned_rows", unit="{row}", description="Items returned by an operation."
                ),
                "capacity": meter.create_counter(
                    "dynamoxy.consumed_capacity", unit="{capacity_unit}", description="Capacity units consumed."
                ),
                "attempts": meter.create_counter(
                    "dynamoxy.request.attempts", unit="{attempt}", description="Attempts sent, retries included."
                ),
                "throttles": meter.create_counter(
                    "dynamoxy.request.throttles", unit="{attempt}", description="Attempts DynamoDB throttled."
                ),
            }

    @property
    def tracing(self) -> bool:
        """Whether spans are created."""
        return self._tracer is not None

    def recording_tracer(self) -> Any:
        """The tracer, unless spans would record nothing (no tracer, or no SDK configured yet)."""
        proxy = self._tracer_proxy
        if proxy is not None:
            # Like the API's proxy tracer, look at the global provider without a function call
            # (`get_tracer_provider()` also reads the environment on every call).
            current = self._trace_globals.get("_TRACER_PROVIDER", _UNKNOWN)
            if current is None or (current is _UNKNOWN and self._otel.get_tracer_provider() is proxy):
                return None
            self._tracer_proxy = None  # an SDK provider was set; it cannot be unset
        return self._tracer

    def recording_metrics(self) -> bool:
        """Whether metrics are recorded (instruments exist and an SDK is configured)."""
        if self._instruments is None:
            return False
        proxy = self._meter_proxy
        if proxy is not None:
            current = self._metrics_globals.get("_METER_PROVIDER", _UNKNOWN)
            if current is None or (current is _UNKNOWN and self._otel_metrics.get_meter_provider() is proxy):
                return False
            self._meter_proxy = None
        return True

    def base_attributes(self, operation: str, table: str | None) -> dict[str, Any]:
        """Attributes every span and metric of an operation carries."""
        attributes: dict[str, Any] = {
            "db.system.name": "aws.dynamodb",
            "db.operation.name": operation,
            "rpc.system": "aws-api",
            "rpc.service": "DynamoDB",
            "rpc.method": operation,
        }
        if table:
            attributes["db.collection.name"] = table
            attributes["aws.dynamodb.table_names"] = [table]
        if self.region:
            attributes["cloud.region"] = self.region
        if self._server is not None:
            attributes["server.address"], port = self._server
            if port is not None:
                attributes["server.port"] = port
        return attributes

    @contextlib.contextmanager
    def span(self, operation: str, table: str | None, params: Mapping[str, Any]) -> Iterator[Any]:
        """A span around one call (yields ``None`` when tracing is off)."""
        if self._tracer is None:
            yield None
            return
        attributes = self.base_attributes(operation, table)
        for parameter, attribute in (
            ("index_name", "aws.dynamodb.index_name"),
            ("limit", "aws.dynamodb.limit"),
            ("select", "aws.dynamodb.select"),
            ("consistent_read", "aws.dynamodb.consistent_read"),
            ("scan_index_forward", "aws.dynamodb.scan_forward"),
            ("segment", "aws.dynamodb.segment"),
            ("total_segments", "aws.dynamodb.total_segments"),
            ("projection_expression", "aws.dynamodb.projection"),
        ):
            value = params.get(parameter)
            if value is not None:
                attributes[attribute] = value
        settings = self.settings
        if settings is not None and settings.record_expressions:
            expressions = [
                f"{name}: {params[name]}"
                for name in (
                    "key_condition_expression",
                    "filter_expression",
                    "condition_expression",
                    "update_expression",
                )
                if params.get(name)
            ]
            if params.get("statement"):
                expressions.append(str(params["statement"]))
            if expressions:
                attributes["db.query.text"] = "; ".join(expressions)
        name = f"{operation} {table}" if table else operation
        kind = self._otel.SpanKind.CLIENT
        with self._tracer.start_as_current_span(name, kind=kind, attributes=attributes, record_exception=False) as span:
            yield span

    def finish_span(
        self,
        span: Any,
        output: Any,
        entry: OperationRecord,
        error: BaseException | None,
    ) -> None:
        """Adds the outcome to a span (``output`` is the native response, usually a dict)."""
        if span is None or not span.is_recording():
            return
        span.set_attribute("dynamoxy.attempts", entry.attempts)
        if entry.throttles:
            span.set_attribute("dynamoxy.throttles", entry.throttles)
        if entry.model:
            span.set_attribute("dynamoxy.model", entry.model)
        if isinstance(output, dict):
            if "count" in output:
                span.set_attribute("aws.dynamodb.count", output["count"])
            if "scanned_count" in output:
                span.set_attribute("aws.dynamodb.scanned_count", output["scanned_count"])
            capacity = output.get("consumed_capacity")
            if capacity:
                reports = capacity if isinstance(capacity, list) else [capacity]
                span.set_attribute(
                    "aws.dynamodb.consumed_capacity", [json.dumps(report, sort_keys=True) for report in reports]
                )
        if error is not None:
            code = getattr(error, "code", None) or type(error).__name__
            span.set_attribute("error.type", code)
            request_id = getattr(error, "request_id", None)
            if request_id:
                span.set_attribute("aws.request_id", request_id)
            span.record_exception(error)
            span.set_status(self._otel.Status(self._otel.StatusCode.ERROR, str(error)[:500]))

    def record_metrics(self, entry: OperationRecord, *, returns_rows: bool = False) -> None:
        """Records the metrics of one finished call (``returns_rows``: a read, whose items are rows)."""
        if not self.recording_metrics():
            return
        instruments = self._instruments
        assert instruments is not None
        attributes = self.base_attributes(entry.operation, entry.table)
        attributes.pop("aws.dynamodb.table_names", None)
        if entry.error is not None:
            attributes["error.type"] = entry.error
        instruments["duration"].record(entry.duration, attributes)
        instruments["attempts"].add(entry.attempts, attributes)
        if entry.throttles:
            instruments["throttles"].add(entry.throttles, attributes)
        if entry.error is None and returns_rows:
            instruments["rows"].record(entry.items, attributes)
        if entry.read_capacity_units:
            instruments["capacity"].add(entry.read_capacity_units, {**attributes, "capacity.type": "read"})
        if entry.write_capacity_units:
            instruments["capacity"].add(entry.write_capacity_units, {**attributes, "capacity.type": "write"})
