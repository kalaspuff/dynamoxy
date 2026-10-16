"""Retries, throttling, timeouts and unprocessed batch items (in-memory fault injection).

Faults can only be injected into the in-memory backend, so these tests do not run on the
emulator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, testing
from tests.models import Event, User

SAVED: list[str] = []


class Note(Model, table="notes"):
    note_id: Attr[str] = partition_key()
    text: Attr[str] = field(default="")
    version: Attr[int] = dynamoxy.version()

    @dynamoxy.before_save
    def refuse(self) -> None:
        if self.text == "refused":
            raise ValueError("refused by before_save")

    @dynamoxy.after_save
    def record(self) -> None:
        SAVED.append(self.note_id)


@pytest.fixture
async def client() -> AsyncIterator[dynamoxy.Client]:
    async with testing.memory_client(retry=dynamoxy.RetryPolicy(max_attempts=3, base_delay=0, max_delay=0)) as client:
        await client.create_tables(User, Event)
        yield client


def event(seq: int) -> Event:
    return Event(stream="s", seq=seq, kind="k")


async def test_throttling_is_retried(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, operation="PutItem", error="throttling", times=2)
    with testing.capture_events(client, "request.*") as events, dynamoxy.collect() as collected:
        await client.repository(Event).put(event(1))
    assert [record.attempts for record in collected.records] == [3]
    assert collected.records[0].throttles == 2
    assert [entry.kind for entry in events].count(dynamoxy.EventKind.REQUEST_RETRY) == 2
    assert dynamoxy.EventKind.REQUEST_THROTTLED in [entry.kind for entry in events]
    metrics = client.metrics().operations["PutItem"]
    assert (metrics.calls, metrics.attempts, metrics.throttles, metrics.errors) == (1, 3, 2, 0)


async def test_retries_give_up(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, operation="GetItem", error="throttling", times=5)
    with testing.capture_events(client, "request.gave_up") as events, pytest.raises(dynamoxy.ThrottlingError) as caught:
        await client.repository(Event).get("s", 1)
    error = caught.value
    assert (error.retryable, error.attempts, error.operation) == (True, 3, "GetItem")
    assert error.duration is not None
    assert error.duration >= 0
    assert len(events) == 1
    assert client.metrics().operations["GetItem"].errors == 1


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("internal", dynamoxy.InternalServerError),
        ("unavailable", dynamoxy.ServiceError),
        ("conflict", dynamoxy.TransactionConflictError),
        ("request_limit", dynamoxy.ThrottlingError),
    ],
)
async def test_retryable_errors(client: dynamoxy.Client, fault: str, expected: type[Exception]) -> None:
    testing.inject_fault(client, operation="PutItem", error=fault, times=2)
    await client.repository(Event).put(event(1))
    testing.inject_fault(client, operation="PutItem", error=fault, times=3)
    with pytest.raises(expected) as caught:
        await client.repository(Event).put(event(2))
    assert caught.value.retryable  # type: ignore[attr-defined]


async def test_errors_that_are_not_retried(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, operation="PutItem", error="ValidationException", message="nope")
    with pytest.raises(dynamoxy.ValidationError, match="nope") as caught:
        await client.repository(Event).put(event(1))
    assert (caught.value.retryable, caught.value.attempts) == (False, 1)
    testing.inject_fault(client, error="access_denied")
    with pytest.raises(dynamoxy.AccessDeniedError):
        await client.repository(Event).get("s", 1)


async def test_faults_target_tables_and_run_out(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, table=client.table_name("users"), error="ValidationException", times=1)
    await client.repository(Event).put(event(1)), "other tables are not affected"
    with pytest.raises(dynamoxy.ValidationError):
        await client.repository(User).get("u1")
    assert await client.repository(User).get("u1") is None, "the fault was used up"


async def test_slow_attempts_time_out_and_are_retried() -> None:
    async with testing.memory_client(
        timeout=0.05, retry=dynamoxy.RetryPolicy(max_attempts=2, base_delay=0, max_delay=0)
    ) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="GetItem", latency=0.5)
        assert await client.repository(Event).get("s", 1) is None
        assert client.metrics().operations["GetItem"].attempts == 2
        testing.inject_fault(client, operation="GetItem", latency=0.5, times=2)
        with pytest.raises(dynamoxy.TransportError, match="no response within") as caught:
            await client.repository(Event).get("s", 1)
        assert caught.value.retryable


async def test_timeouts_of_writes_can_be_left_to_the_caller() -> None:
    policy = dynamoxy.RetryPolicy(max_attempts=3, base_delay=0, max_delay=0, retry_timeouts=False)
    async with testing.memory_client(timeout=0.05, retry=policy) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="PutItem", latency=0.5)
        with pytest.raises(dynamoxy.TransportError):
            await client.repository(Event).put(event(1))
        assert client.metrics().operations["PutItem"].attempts == 1


async def test_slow_operations_emit_events() -> None:
    async with testing.memory_client(slow_operation_threshold=0.1) as client:  # roomy for slow CI machines
        await client.create_tables(Event)
        testing.inject_fault(client, operation="GetItem", latency=0.3)
        with testing.capture_events(client, "request.slow") as events:
            await client.repository(Event).get("s", 1)
            await client.repository(Event).get("s", 1)
        assert len(events) == 1
        assert events[0].fields["operation"] == "GetItem"


async def test_unprocessed_batch_items_are_sent_again(client: dynamoxy.Client) -> None:
    events = client.repository(Event)
    testing.inject_fault(client, operation="BatchWriteItem", unprocessed=True, times=2)
    with testing.capture_events(client, "batch.*") as emitted:
        await events.batch_put([event(seq) for seq in range(40)])
    assert await events.count() == 40
    assert {entry.kind for entry in emitted} == {dynamoxy.EventKind.BATCH_UNPROCESSED}
    testing.inject_fault(client, operation="BatchGetItem", unprocessed=True, times=1)
    found = await events.batch_get([Event.key("s", seq) for seq in range(40)])
    assert len(found) == 40


async def test_batches_report_what_stays_unprocessed() -> None:
    async with testing.memory_client(batch_max_rounds=2) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="BatchWriteItem", unprocessed=True, times=100)
        with (
            testing.capture_events(client, "batch.incomplete") as emitted,
            pytest.raises(dynamoxy.UnprocessedItemsError) as caught,
        ):
            await client.repository(Event).batch_put([event(seq) for seq in range(3)])
        assert sorted(item["seq"] for _, item in caught.value.unprocessed) == [0, 1, 2]
        assert {kind for kind, _ in caught.value.unprocessed} == {"put"}
        assert caught.value.retryable
        assert len(emitted) == 1


async def test_failed_operations_are_collected_and_counted(client: dynamoxy.Client) -> None:
    users = client.repository(User)
    await users.create(User(user_id="u1", email="a@example.com", name="Ada"))
    with (
        dynamoxy.collect() as collected,
        testing.capture_events(client, "operation.failed") as failed,
        pytest.raises(dynamoxy.ItemAlreadyExistsError),
    ):
        await users.create(User(user_id="u1", email="b@example.com", name="Bob"))
    assert [record.error for record in collected.records] == ["TransactionCanceledException"]
    assert collected.errors == 1
    assert failed[0].fields["reasons"] == "ConditionalCheckFailed,None"


async def test_batch_writers_finalize_only_what_was_written() -> None:
    SAVED.clear()
    async with testing.memory_client(batch_max_rounds=1) as client:
        await client.create_tables(Note)
        notes = [Note(note_id=f"n{index}") for index in range(3)]
        testing.inject_fault(client, operation="BatchWriteItem", unprocessed=True, times=10)
        batch = client.batch_writer()
        for note in notes:
            await batch.put(note)
        with pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await batch.flush()
        assert len(caught.value.unprocessed) == 3
        assert SAVED == [], "no after_save hook for writes that were not made"
        assert [(note.is_loaded, note.version) for note in notes] == [(False, None)] * 3
        client.store.clear_faults()  # type: ignore[union-attr]
        await client.repository(Note).batch_put(notes)
        assert SAVED == ["n0", "n1", "n2"]
        assert all(note.is_loaded for note in notes)


async def test_failed_flushes_keep_the_other_writes() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Note)
        batch = client.batch_writer(flush_size=3)
        await batch.put(Note(note_id="a"))
        await batch.put(Note(note_id="b", text="refused"))
        with pytest.raises(ValueError, match="refused by before_save"):
            await batch.put(Note(note_id="c"))  # the third write flushes
        assert len(batch) == 2, "a and c stay queued; b is reported by the exception"
        testing.inject_fault(client, operation="BatchWriteItem", error="internal", times=10)
        with pytest.raises(dynamoxy.InternalServerError):
            await batch.flush()
        assert len(batch) == 2, "a request that failed keeps its writes queued (they are unconditional)"
        client.store.clear_faults()  # type: ignore[union-attr]
        await batch.flush()
        assert sorted(item["note_id"] for item in await client.table("notes").scan().all()) == ["a", "c"]
        assert batch.written == 2
