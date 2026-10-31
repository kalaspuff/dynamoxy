"""OpenTelemetry spans and metrics (with the OpenTelemetry SDK's in-memory exporters)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import dynamoxy
from dynamoxy import testing
from tests.models import Event, User


@dataclass
class Telemetry:
    client: dynamoxy.Client
    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def points(self, name: str) -> list[Any]:
        data = self.metrics.get_metrics_data()
        assert data is not None
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


@pytest.fixture
async def otel() -> AsyncIterator[Telemetry]:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    settings = dynamoxy.Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)
    async with testing.memory_client(telemetry=settings, region="eu-north-1") as client:
        await client.create_tables(User, Event)
        exporter.clear()
        yield Telemetry(client, exporter, reader)
    meter_provider.shutdown()
    tracer_provider.shutdown()


async def test_spans_follow_the_semantic_conventions(otel: Telemetry) -> None:
    events = otel.client.model(Event)
    await events.put(Event(stream="s", seq=1, kind="k"))
    await events.query(stream="s").consistent().limit(5).all()
    put, query = otel.finished()
    assert put.name == "PutItem events"
    assert put.kind is SpanKind.CLIENT
    attributes = dict(put.attributes or {})
    assert attributes["db.system.name"] == "aws.dynamodb"
    assert attributes["db.operation.name"] == "PutItem"
    assert attributes["db.collection.name"] == "events"
    assert attributes["aws.dynamodb.table_names"] == ("events",)
    assert attributes["cloud.region"] == "eu-north-1"
    assert attributes["dynamoxy.model"] == "Event"
    assert attributes["dynamoxy.attempts"] == 1
    (capacity,) = cast("tuple[str, ...]", attributes["aws.dynamodb.consumed_capacity"])
    assert json.loads(capacity)["table_name"] == "events"
    assert "db.query.text" not in attributes, "expressions are only recorded on request"
    attributes = dict(query.attributes or {})
    assert attributes["aws.dynamodb.consistent_read"] is True
    assert attributes["aws.dynamodb.limit"] == 5
    assert attributes["aws.dynamodb.count"] == 1
    assert attributes["aws.dynamodb.scanned_count"] == 1


async def test_failures_mark_spans_as_errors(otel: Telemetry) -> None:
    users = otel.client.model(User)
    await users.create(User(user_id="u1", email="a@example.com", name="Ada"))
    otel.spans.clear()
    with pytest.raises(dynamoxy.ItemAlreadyExistsError):
        await users.create(User(user_id="u1", email="b@example.com", name="Bob"))
    (span,) = otel.finished()
    assert span.name == "TransactWriteItems users", "the item and its guard item share the table"
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes or {})["error.type"] == "TransactionCanceledException"
    assert [event.name for event in span.events] == ["exception"]


async def test_spans_nest_under_the_application_span(otel: Telemetry) -> None:
    tracer = otel.client._recorder._tracer  # the client's tracer, from the test's provider
    with tracer.start_as_current_span("handler") as parent:
        await otel.client.model(Event).get("s", 1)
    get, handler = otel.finished()
    assert handler.name == "handler"
    assert get.parent is not None
    assert get.parent.span_id == parent.get_span_context().span_id


async def test_metrics(otel: Telemetry) -> None:
    events = otel.client.model(Event)
    await events.put(Event(stream="s", seq=1, kind="k"))
    await events.get("s", 1)
    testing.inject_fault(otel.client, operation="GetItem", error="throttling")
    await events.get("s", 1)
    durations = {point.attributes["db.operation.name"]: point for point in otel.points("db.client.operation.duration")}
    assert durations["GetItem"].count == 2
    assert durations["PutItem"].count == 1
    assert durations["GetItem"].attributes["db.collection.name"] == "events"
    capacity = {
        (point.attributes["db.operation.name"], point.attributes["capacity.type"]): point.value
        for point in otel.points("dynamoxy.consumed_capacity")
    }
    assert capacity[("PutItem", "write")] == 3
    assert capacity[("GetItem", "read")] == 1
    attempts = {
        point.attributes["db.operation.name"]: point.value for point in otel.points("dynamoxy.request.attempts")
    }
    assert attempts["GetItem"] == 3
    throttles = [point.value for point in otel.points("dynamoxy.request.throttles")]
    assert throttles == [1]
    rows = {
        point.attributes["db.operation.name"]: point.sum for point in otel.points("db.client.response.returned_rows")
    }
    assert rows == {"GetItem": 2}, "the items of writes are not returned rows"


async def test_failed_calls_record_their_throttles(otel: Telemetry) -> None:
    testing.inject_fault(otel.client, operation="GetItem", error="throttling", times=10)
    with pytest.raises(dynamoxy.ThrottlingError):
        await otel.client.model(Event).get("s", 1)
    (span,) = otel.finished()
    attributes = dict(span.attributes or {})
    attempts = otel.client.retry.max_attempts
    assert (attributes["dynamoxy.attempts"], attributes["dynamoxy.throttles"]) == (attempts, attempts)
    assert [point.value for point in otel.points("dynamoxy.request.throttles")] == [attempts]


async def test_batches_that_leave_items_unprocessed_are_failures(otel: Telemetry) -> None:
    testing.inject_fault(otel.client, operation="BatchWriteItem", unprocessed=True, times=100)
    with pytest.raises(dynamoxy.UnprocessedItemsError):
        await otel.client.model(Event).batch_put([Event(stream="s", seq=seq, kind="k") for seq in range(3)])
    (span,) = otel.finished()
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes or {})["error.type"] == "UnprocessedItemsError"
    (duration,) = [
        point
        for point in otel.points("db.client.operation.duration")
        if point.attributes["db.operation.name"] == "BatchWriteItem"
    ]
    assert duration.attributes["error.type"] == "UnprocessedItemsError"


async def test_expressions_are_recorded_without_values() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = dynamoxy.Telemetry(tracer_provider=provider, metrics=False, record_expressions=True)
    async with testing.memory_client(telemetry=settings, endpoint_url=None) as client:
        await client.create_tables(Event)
        await client.model(Event).query(stream="secret-stream").filter(Event.kind == "secret-kind").all()
    (query,) = [span for span in exporter.get_finished_spans() if span.name.startswith("Query")]
    text = str(dict(query.attributes or {})["db.query.text"])
    assert "key_condition_expression: " in text
    assert "filter_expression: " in text
    assert "secret" not in text


async def test_disabled_telemetry_records_nothing() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = dynamoxy.Telemetry(tracer_provider=provider, tracing=False, metrics=False)
    async with testing.memory_client(telemetry=settings) as client:
        await client.create_tables(Event)
    async with testing.memory_client(telemetry=None) as client:
        await client.create_tables(Event)
    assert exporter.get_finished_spans() == ()
    assert dynamoxy.Telemetry.disabled() == dynamoxy.Telemetry(tracing=False, metrics=False)


def test_server_attributes_come_from_the_endpoint() -> None:
    from dynamoxy.telemetry import TelemetryRecorder

    recorder = TelemetryRecorder(None, region=None, endpoint="http://localhost:4566")
    assert recorder.base_attributes("GetItem", None) | {} == {
        "db.system.name": "aws.dynamodb",
        "db.operation.name": "GetItem",
        "rpc.system": "aws-api",
        "rpc.service": "DynamoDB",
        "rpc.method": "GetItem",
        "server.address": "localhost",
        "server.port": 4566,
    }
