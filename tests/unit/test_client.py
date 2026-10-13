"""Client options, middleware, events, metrics, collection and closing (in-memory backend)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest

import dynamoxy
from dynamoxy import testing
from tests.models import Event, User


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"endpoint_url": "localhost:8000"}, "must start with http"),
        ({"consumed_capacity": "ALL"}, "consumed_capacity"),
        ({"batch_concurrency": 0}, "batch_concurrency"),
        ({"batch_max_rounds": 0}, "batch_max_rounds"),
        ({"event_buffer": 0}, "event_buffer"),
        ({"timeout": -1}, "timeout"),
        ({"store": dynamoxy.MemoryStore(), "endpoint_url": "http://localhost:8000"}, "not both"),
    ],
)
def test_invalid_options_fail_early(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dynamoxy.Client(**options)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"base_delay": -1}, "base_delay"),
        ({"base_delay": 2, "max_delay": 1}, "max_delay"),
    ],
)
def test_invalid_retry_policies(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dynamoxy.RetryPolicy(**options)


def test_core_configuration_and_secrets() -> None:
    credentials = dynamoxy.Credentials("AKIDEXAMPLE", "very-secret", "token-secret")
    client = dynamoxy.Client(
        region="eu-west-1",
        endpoint_url="http://localhost:8000",
        credentials=credentials,
        timeout=timedelta(seconds=2),
        retry=dynamoxy.RetryPolicy.none(),
        table_prefix="dev-",
        table_names={"users": "legacy_users"},
    )
    core = client.to_core()
    assert core["region"] == "eu-west-1"
    assert core["timeout"] == 2.0
    assert core["retry"]["max_attempts"] == 1
    assert "very-secret" not in repr(credentials)
    assert "token-secret" not in repr(credentials)
    assert "very-secret" not in repr(client)
    assert client.table_name("events") == "dev-events"
    assert client.table_name("users") == "legacy_users"
    assert client.backend == "aws"
    assert testing.memory_client().backend == "memory"


async def test_middleware_wraps_calls_in_order() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Event)
        seen: list[str] = []

        async def outer(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
            seen.append(f"outer:{request.operation}")
            request.context["tag"] = "x"
            response = await call_next(request)
            seen.append(f"outer:done:{response.stats.attempts}")
            return response

        async def inner(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
            seen.append(f"inner:{request.context.get('tag')}:{request.model.__name__ if request.model else None}")
            if request.operation == "PutItem":
                request.params["item"]["kind"] = "rewritten"
            return await call_next(request)

        remove_outer = client.use(outer)
        client.use(inner)
        await client.repository(Event).put(Event(stream="s", seq=1, kind="k"))
        assert seen == ["outer:PutItem", "inner:x:Event", "outer:done:1"]
        assert (await client.repository(Event).get_or_raise("s", 1)).kind == "rewritten"
        remove_outer()
        remove_outer()  # removing twice is harmless
        seen.clear()
        await client.repository(Event).get("s", 1)
        assert seen == ["inner:None:Event"]


async def test_middleware_can_short_circuit() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Event)

        async def deny(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
            if request.operation == "DeleteItem":
                raise PermissionError("deletes are disabled")
            return await call_next(request)

        client.use(deny)
        with pytest.raises(PermissionError):
            await client.repository(Event).delete(Event.key("s", 1))


async def test_event_hooks() -> None:
    async with testing.memory_client() as client:
        received: list[dynamoxy.Event] = []

        @client.on("table.*")
        def on_table(event: dynamoxy.Event) -> None:
            received.append(event)

        async def slow_hook(event: dynamoxy.Event) -> None:
            await asyncio.sleep(0)
            received.append(event)

        unsubscribe = client.on(dynamoxy.EventKind.OPERATION_SUCCEEDED, slow_hook)
        await client.create_tables(Event)
        await client.events.drain()
        kinds = [event.kind for event in received]
        assert dynamoxy.EventKind.TABLE_CREATED in kinds
        assert dynamoxy.EventKind.OPERATION_SUCCEEDED in kinds
        created = next(event for event in received if event.kind is dynamoxy.EventKind.TABLE_CREATED)
        assert created.category == "table"
        assert created.fields["table"] == "events"
        unsubscribe()
        received.clear()
        await client.repository(Event).get("s", 1)
        await client.events.drain()
        assert received == []
        with pytest.raises(ValueError, match="matches no event kind"):
            client.on("tabel.*", on_table)


async def test_failing_hooks_never_break_operations(caplog: pytest.LogCaptureFixture) -> None:
    async with testing.memory_client() as client:
        failures: list[dynamoxy.Event] = []

        def broken(event: dynamoxy.Event) -> None:
            raise RuntimeError("boom")

        client.on("operation.succeeded", broken)
        client.on("hook.failed", failures.append)
        with caplog.at_level(logging.ERROR, logger="dynamoxy"):
            await client.create_tables(Event)
        assert failures
        assert failures[0].fields["error"] == "RuntimeError"
        assert "boom" in caplog.text


async def test_logging_configuration(caplog: pytest.LogCaptureFixture) -> None:
    config = dynamoxy.LoggingConfig(levels={"table.created": "warning"}, disabled=("client.*",), include_fields=False)
    async with testing.memory_client(logging=config) as client:
        with caplog.at_level(logging.DEBUG, logger="dynamoxy"):
            await client.create_tables(Event)
    records = {record.getMessage(): record for record in caplog.records}
    created = next(record for message, record in records.items() if "created" in message and "table" in record.name)
    assert created.levelno == logging.WARNING
    assert "table=" not in created.getMessage()
    assert not any(record.name == "dynamoxy.client" for record in caplog.records)
    assert isinstance(created.__dict__["dynamoxy"], dynamoxy.Event)


async def test_metrics_and_collection() -> None:
    client = testing.memory_client()
    assert client.metrics().operations == {}, "nothing before connecting"
    async with client:
        await client.create_tables(User, Event)
        events = client.repository(Event)
        with dynamoxy.collect() as outer:
            await events.put(Event(stream="s", seq=1, kind="k"))
            with dynamoxy.collect() as inner:
                await events.get("s", 1)
                await events.query(stream="s").all()
        assert outer.by_operation() == {"PutItem": 1, "GetItem": 1, "Query": 1}
        assert inner.calls == 2
        assert outer.write_capacity_units == 3, "the table and its two indexes"
        assert outer.read_capacity_units == 1, "two eventually consistent reads"
        summary = outer.summary()
        assert summary["calls"] == 3
        assert summary["errors"] == 0
        assert summary["retries"] == 0
        assert {record.model for record in outer.records} == {"Event"}
        snapshot = client.metrics()
        assert snapshot.operations["PutItem"].calls == 1
        assert snapshot.in_flight == 0
        assert snapshot.latency_buckets_ms[-1] is None
        client.reset_metrics()
        assert "PutItem" not in client.metrics().operations


async def test_collectors_follow_tasks() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Event)
        events = client.repository(Event)
        with dynamoxy.collect() as collected:
            await asyncio.gather(*(events.get("s", seq) for seq in range(5)))
        assert collected.calls == 5


async def test_closed_clients_refuse_calls() -> None:
    client = testing.memory_client()
    await client.create_tables(Event)
    closed: list[dynamoxy.Event] = []
    client.on("client.closed", closed.append)
    await client.close()
    await client.close()
    assert client.closed
    assert len(closed) == 1
    with pytest.raises(dynamoxy.ShutdownError, match="closed"):
        await client.repository(Event).get("s", 1)


async def test_clients_work_across_event_loops() -> None:
    client = testing.memory_client()

    async def work(seq: int) -> int:
        await client.repository(Event).put(Event(stream="s", seq=seq, kind="k"))
        return await client.repository(Event).count()

    await client.create_tables(Event)
    assert await work(1) == 1
    loop = asyncio.new_event_loop()
    try:
        assert await asyncio.to_thread(loop.run_until_complete, work(2)) == 2
    finally:
        loop.close()
    await client.close()


async def test_shared_stores_and_purging_expired_items() -> None:
    store = dynamoxy.MemoryStore()
    writer, reader = testing.memory_client(store=store), testing.memory_client(store=store)
    await writer.create_tables(Event)
    await writer.repository(Event).put(Event(stream="s", seq=1, kind="k", expires_at=dynamoxy_epoch(1)))
    await writer.repository(Event).put(Event(stream="s", seq=2, kind="k"))
    assert await reader.repository(Event).count() == 2
    assert testing.purge_expired(store) == 1
    assert [event.seq for event in await reader.repository(Event).scan().all()] == [2]
    with pytest.raises(TypeError, match="in-memory client"):
        testing.purge_expired(dynamoxy.Client())
    await writer.close()
    await reader.close()


def dynamoxy_epoch(seconds: int) -> Any:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC)
