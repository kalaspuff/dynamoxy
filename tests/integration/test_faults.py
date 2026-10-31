"""Retries, throttling, timeouts and unprocessed batch items (in-memory fault injection).

Faults can only be injected into the in-memory backend, so these tests do not run on the
emulator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

import dynamoxy
from dynamoxy import Attr, F, Model, field, partition_key, testing
from tests.models import Event, User

SAVED: list[str] = []


class Note(Model, table="notes"):
    note_id: Attr[str] = partition_key()
    text: Attr[str] = field(default="")
    version: Attr[str] = dynamoxy.version()

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
        await client.model(Event).put(event(1))
    assert [record.attempts for record in collected.records] == [3]
    assert collected.records[0].throttles == 2
    assert [entry.kind for entry in events].count(dynamoxy.EventKind.REQUEST_RETRY) == 2
    assert dynamoxy.EventKind.REQUEST_THROTTLED in [entry.kind for entry in events]
    metrics = client.metrics().operations["PutItem"]
    assert (metrics.calls, metrics.attempts, metrics.throttles, metrics.errors) == (1, 3, 2, 0)


async def test_retries_give_up(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, operation="GetItem", error="throttling", times=5)
    with testing.capture_events(client, "request.gave_up") as events, pytest.raises(dynamoxy.ThrottlingError) as caught:
        await client.model(Event).get("s", 1)
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
    await client.model(Event).put(event(1))
    testing.inject_fault(client, operation="GetItem", error=fault, times=3)
    with pytest.raises(expected) as caught:
        await client.model(Event).get("s", 1)
    assert caught.value.retryable  # type: ignore[attr-defined]
    # A write whose attempts all failed: a 5xx may have been applied ("the operation may have
    # succeeded or failed"), so the outcome is unknown; other errors applied nothing.
    testing.inject_fault(client, operation="PutItem", error=fault, times=3)
    unknown = fault in ("internal", "unavailable")
    with pytest.raises(dynamoxy.WriteOutcomeUnknownError if unknown else expected) as caught:
        await client.model(Event).put(event(2))
    assert caught.value.retryable is not unknown  # type: ignore[attr-defined]


async def test_errors_that_are_not_retried(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, operation="PutItem", error="ValidationException", message="nope")
    with pytest.raises(dynamoxy.ValidationError, match="nope") as caught:
        await client.model(Event).put(event(1))
    assert (caught.value.retryable, caught.value.attempts) == (False, 1)
    testing.inject_fault(client, error="access_denied")
    with pytest.raises(dynamoxy.AccessDeniedError):
        await client.model(Event).get("s", 1)


async def test_faults_target_tables_and_run_out(client: dynamoxy.Client) -> None:
    testing.inject_fault(client, table=client.table_name("users"), error="ValidationException", times=1)
    await client.model(Event).put(event(1)), "other tables are not affected"
    with pytest.raises(dynamoxy.ValidationError):
        await client.model(User).get("u1")
    assert await client.model(User).get("u1") is None, "the fault was used up"


async def test_slow_attempts_time_out_and_are_retried() -> None:
    async with testing.memory_client(
        timeout=0.05, retry=dynamoxy.RetryPolicy(max_attempts=2, base_delay=0, max_delay=0)
    ) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="GetItem", latency=0.5)
        assert await client.model(Event).get("s", 1) is None
        assert client.metrics().operations["GetItem"].attempts == 2
        testing.inject_fault(client, operation="GetItem", latency=0.5, times=2)
        with pytest.raises(dynamoxy.TransportError, match="no response within") as caught:
            await client.model(Event).get("s", 1)
        assert caught.value.retryable


async def test_timeouts_of_writes_can_be_left_to_the_caller() -> None:
    policy = dynamoxy.RetryPolicy(max_attempts=3, base_delay=0, max_delay=0, retry_timeouts=False)
    async with testing.memory_client(timeout=0.05, retry=policy) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="PutItem", latency=0.5)
        with pytest.raises(dynamoxy.WriteOutcomeUnknownError) as caught:
            await client.table("events").put(event(1).to_item())
        assert isinstance(caught.value, dynamoxy.TransportError)
        assert not caught.value.retryable
        assert client.metrics().operations["PutItem"].attempts == 1


async def test_without_retries_of_timeouts_lost_writes_are_not_retryable() -> None:
    policy = dynamoxy.RetryPolicy(max_attempts=3, base_delay=0, max_delay=0, retry_timeouts=False)
    async with testing.memory_client(retry=policy) as client:
        await client.create_tables(Event, Plain)
        table = client.table("plain")
        await table.put({"plain_id": "a", "n": 0})
        writes = {
            "an increment": lambda: table.update({"plain_id": "a"}, F("n").add(1)),
            "a SET": lambda: table.update({"plain_id": "a"}, F("text").set("x")),
            "a put": lambda: table.put({"plain_id": "b", "n": 0}),
        }
        for name, write in writes.items():
            testing.inject_fault(client, lose_response=True)
            with pytest.raises(dynamoxy.WriteOutcomeUnknownError) as caught:
                await write()
            assert isinstance(caught.value, dynamoxy.TransportError), name
            assert not caught.value.retryable, name
            assert caught.value.attempts == 1, name
        # Each lost write was applied once.
        assert await table.get({"plain_id": "a"}) == {"plain_id": "a", "n": 1, "text": "x"}
        assert await table.get({"plain_id": "b"}) == {"plain_id": "b", "n": 0}

        # Reads keep their retryable transport error.
        testing.inject_fault(client, operation="GetItem", lose_response=True)
        with pytest.raises(dynamoxy.TransportError) as read:
            await table.get({"plain_id": "a"})
        assert not isinstance(read.value, dynamoxy.WriteOutcomeUnknownError)
        assert read.value.retryable

        # Model writes settle the unknown outcome by their write id.
        testing.inject_fault(client, operation="PutItem", lose_response=True)
        with testing.capture_events(client, "request.outcome_*") as outcomes:
            await client.model(Event).create(event(3))
        assert [entry.kind for entry in outcomes] == [
            dynamoxy.EventKind.REQUEST_OUTCOME_UNKNOWN,
            dynamoxy.EventKind.REQUEST_OUTCOME_SETTLED,
        ]


async def test_conditions_that_fail_after_a_lost_response_have_an_unknown_outcome(client: dynamoxy.Client) -> None:
    table = client.model(Event).table
    item = {"stream": "s", "seq": 1, "kind": "k"}
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    with (
        testing.capture_events(client, "request.outcome_unknown") as events,
        pytest.raises(dynamoxy.WriteOutcomeUnknownError, match="condition may have failed because of it") as caught,
    ):
        await table.put(item, condition=F("stream").not_exists())
    # The first attempt wrote the item; the second one failed its condition because of it.
    error = caught.value
    assert isinstance(error, dynamoxy.TransportError)
    assert not isinstance(error, dynamoxy.ConditionalCheckFailedError)
    assert not error.retryable
    assert error.item == item
    assert error.attempts == 2
    assert [entry.kind for entry in events] == [dynamoxy.EventKind.REQUEST_OUTCOME_UNKNOWN]
    assert await table.get({"stream": "s", "seq": 1}) == item


class Plain(Model, table="plain", write_ids=False):
    """A model whose items store no write id."""

    plain_id: Attr[str] = partition_key()
    n: Attr[int] = field(default=0)


async def test_model_writes_recognise_their_own_earlier_attempt(client: dynamoxy.Client) -> None:
    await client.create_tables(Note)
    events, notes, users = client.model(Event), client.model(Note), client.model(User)
    # The stored item holds this write's id: the lost attempt was applied, and create() succeeds
    # (instead of a false ItemAlreadyExistsError).
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    with testing.capture_events(client, "request.outcome_*") as outcomes:
        created = await events.create(event(2))
    assert created.is_loaded
    kinds = [entry.kind for entry in outcomes]
    assert kinds == [dynamoxy.EventKind.REQUEST_OUTCOME_UNKNOWN, dynamoxy.EventKind.REQUEST_OUTCOME_SETTLED]

    # save() of a versioned model: applied once, not a VersionConflictError.
    note = await notes.create(Note(note_id="n1", text="a"))
    created_version = note.version
    note.text = "b"
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    await notes.save(note)
    assert note.version not in (None, created_version)
    assert (await notes.get_or_raise("n1")).version == note.version

    # An increment is not sent again: a consistent read finds its id.
    await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
    testing.inject_fault(client, operation="UpdateItem", lose_response=True)
    updated = await users.update({"user_id": "u1"}, User.visits.increment(1))
    assert updated.visits == 1
    assert (await users.get_or_raise("u1")).visits == 1, "applied once"
    assert client.metrics().operations["UpdateItem"].attempts == 1


async def test_writes_that_run_out_of_attempts_after_a_lost_response_are_settled(client: dynamoxy.Client) -> None:
    # The first attempt creates the item and its response is lost; the others are throttled. The
    # call does not report the throttling (as if nothing was written): the outcome is unknown,
    # and the write id settles it.
    events = client.model(Event)
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    testing.inject_fault(client, operation="PutItem", error="throttling", times=2)
    with testing.capture_events(client, "request.outcome_*") as outcomes:
        created = await events.create(event(3))
    assert created.is_loaded
    assert [entry.kind for entry in outcomes] == [
        dynamoxy.EventKind.REQUEST_OUTCOME_UNKNOWN,
        dynamoxy.EventKind.REQUEST_OUTCOME_SETTLED,
    ]
    # The dict-level API reports the unknown outcome.
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    testing.inject_fault(client, operation="PutItem", error="throttling", times=2)
    with pytest.raises(dynamoxy.WriteOutcomeUnknownError, match="no later attempt answered whether it was"):
        await events.table.put({"stream": "s", "seq": 4, "kind": "k"})


async def test_calls_in_progress_finish_their_retries_after_close() -> None:
    async with testing.memory_client(timeout=0.05, retry=dynamoxy.RetryPolicy(base_delay=0, max_delay=0)) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="PutItem", latency=0.5)  # the first attempt times out
        put = asyncio.create_task(client.model(Event).create(event(1)))
        await asyncio.sleep(0.01)
        await client.close()
        created = await put
        assert created.is_loaded


async def test_writes_overwritten_since_their_lost_attempt_stay_unknown(client: dynamoxy.Client) -> None:
    events = client.model(Event)
    await events.create(event(1))
    table = events.table
    # The first attempt is applied and lost; before the second one, another writer writes.
    testing.inject_fault(client, operation="UpdateItem", lose_response=True)
    testing.inject_fault(client, operation="UpdateItem", latency=0.2)

    async def other_writer() -> None:
        await asyncio.sleep(0.05)
        await table.put({"stream": "s", "seq": 1, "kind": "other", "_wid": "0000000000000000"})

    writer = asyncio.create_task(other_writer())
    with pytest.raises(dynamoxy.WriteOutcomeUnknownError) as caught:
        await events.update({"stream": "s", "seq": 1}, Event.kind.set("mine"), condition=Event.kind == "k")
    await writer
    assert caught.value.item is not None
    assert caught.value.item["kind"] == "other", "the item as stored now"


async def test_models_without_write_ids_report_the_unknown_outcome(client: dynamoxy.Client) -> None:
    await client.create_tables(Plain)
    plains = client.model(Plain)
    testing.inject_fault(client, operation="PutItem", lose_response=True)
    with pytest.raises(dynamoxy.WriteOutcomeUnknownError) as caught:
        await plains.create(Plain(plain_id="p1"))
    assert caught.value.item == {"plain_id": "p1", "n": 0}


async def test_updates_that_would_apply_twice_are_not_sent_again(client: dynamoxy.Client) -> None:
    table = client.model(Event).table
    await table.put({"stream": "s", "seq": 1, "kind": "k", "n": 0})
    testing.inject_fault(client, operation="UpdateItem", lose_response=True)
    with pytest.raises(dynamoxy.WriteOutcomeUnknownError, match="could apply it twice") as caught:
        await table.update({"stream": "s", "seq": 1}, F("n").add(1))
    assert caught.value.attempts == 1
    assert caught.value.item is None
    assert (await table.get({"stream": "s", "seq": 1}) or {})["n"] == 1, "applied once"

    testing.inject_fault(client, operation="UpdateItem", lose_response=True)
    updated = await table.update({"stream": "s", "seq": 1}, F("kind").set("x"))
    assert updated is not None
    assert updated["kind"] == "x", "an idempotent update is sent again"
    assert client.metrics().operations["UpdateItem"].attempts == 3


async def test_transactions_are_sent_again_after_a_lost_response(client: dynamoxy.Client) -> None:
    users = client.model(User)  # unique emails: create() is a transaction
    testing.inject_fault(client, operation="TransactWriteItems", lose_response=True)
    await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
    assert client.metrics().operations["TransactWriteItems"].attempts == 2
    assert await users.get("u1") is not None


async def test_slow_operations_emit_events() -> None:
    async with testing.memory_client(slow_operation_threshold=0.1) as client:  # roomy for slow CI machines
        await client.create_tables(Event)
        testing.inject_fault(client, operation="GetItem", latency=0.3)
        with testing.capture_events(client, "request.slow") as events:
            await client.model(Event).get("s", 1)
            await client.model(Event).get("s", 1)
        assert len(events) == 1
        assert events[0].fields["operation"] == "GetItem"


async def test_unprocessed_batch_items_are_sent_again(client: dynamoxy.Client) -> None:
    events = client.model(Event)
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
            await client.model(Event).batch_put([event(seq) for seq in range(3)])
        assert sorted(item["seq"] for _, item in caught.value.unprocessed) == [0, 1, 2]
        assert {kind for kind, _ in caught.value.unprocessed} == {"put"}
        assert caught.value.retryable
        assert len(emitted) == 1


async def test_a_batch_request_that_fails_after_its_attempts_is_sent_again(client: dynamoxy.Client) -> None:
    events = client.model(Event)
    # Every attempt (3) of the first chunk's request is throttled; the next round writes it.
    testing.inject_fault(client, operation="BatchWriteItem", error="throttling", times=3)
    with testing.capture_events(client, "batch.*") as emitted:
        await events.batch_put([event(seq) for seq in range(40)])
    assert await events.count() == 40
    assert [entry.kind for entry in emitted] == [dynamoxy.EventKind.BATCH_UNPROCESSED]
    testing.inject_fault(client, operation="BatchGetItem", error="internal", times=3)
    assert len(await events.batch_get([Event.key("s", seq) for seq in range(40)])) == 40


async def test_batches_report_the_requests_that_failed_in_every_round() -> None:
    async with testing.memory_client(batch_max_rounds=2) as client:
        await client.create_tables(Event)
        events = client.model(Event)
        testing.inject_fault(client, operation="BatchWriteItem", error="throttling", times=1000)
        with dynamoxy.collect() as collected, pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await events.batch_put([event(seq) for seq in range(3)])
        assert isinstance(caught.value.error, dynamoxy.ThrottlingError)
        assert caught.value.__cause__ is caught.value.error
        assert caught.value.retryable
        assert sorted(item["seq"] for _, item in caught.value.unprocessed) == [0, 1, 2]
        assert "ProvisionedThroughputExceededException" in str(caught.value)
        assert [record.error for record in collected.records] == ["UnprocessedItemsError"]
        client.store.clear_faults()  # type: ignore[union-attr]
        await events.batch_put([event(seq) for seq in range(3)])
        testing.inject_fault(client, operation="BatchGetItem", error="internal", times=1000)
        with pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await events.batch_get([Event.key("s", seq) for seq in range(3)])
        assert isinstance(caught.value.error, dynamoxy.InternalServerError)
        assert [kind for kind, _ in caught.value.unprocessed] == ["get"] * 3


async def test_batches_send_nothing_again_after_a_timeout_without_retry_timeouts() -> None:
    async with testing.memory_client(retry=dynamoxy.RetryPolicy(retry_timeouts=False)) as client:
        await client.create_tables(Event)
        testing.inject_fault(client, operation="BatchWriteItem", lose_response=True)
        with dynamoxy.collect() as collected, pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await client.model(Event).batch_put([event(seq) for seq in range(3)])
        assert isinstance(caught.value.error, dynamoxy.TransportError)
        assert caught.value.retryable
        assert len(caught.value.unprocessed) == 3
        assert collected.records[0].requests == 1, "the chunk is not sent again"


async def test_an_error_that_is_not_retried_stops_a_batch_and_reports_what_is_left() -> None:
    SAVED.clear()
    async with testing.memory_client() as client:
        await client.create_tables(Note)
        notes = [Note(note_id=f"n{index:02}") for index in range(60)]
        # The first chunk (25 writes) goes through, the second is refused, the third never sent.
        testing.inject_fault(client, operation="BatchWriteItem", latency=0.0)
        testing.inject_fault(client, operation="BatchWriteItem", error="access_denied")
        with pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await client.model(Note).batch_put(notes)
        assert isinstance(caught.value.error, dynamoxy.AccessDeniedError)
        assert not caught.value.retryable
        assert len(caught.value.unprocessed) == 35
        assert [note.note_id for note in notes[:25]] == SAVED, "the written instances are finalized"
        assert [note.is_loaded for note in notes] == [True] * 25 + [False] * 35
        assert len(await client.table("notes").scan().all()) == 25


async def test_failed_operations_are_collected_and_counted(client: dynamoxy.Client) -> None:
    users = client.model(User)
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
        await client.model(Note).batch_put(notes)
        assert SAVED == ["n0", "n1", "n2"]
        assert all(note.is_loaded for note in notes)


async def test_failed_flushes_keep_the_other_writes() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Note)
        batch = client.batch_writer(flush_size=3)
        a, c = Note(note_id="a"), Note(note_id="c")
        await batch.put(a)
        await batch.put(Note(note_id="b", text="refused"))
        with pytest.raises(ValueError, match="refused by before_save"):
            await batch.put(c)  # the third write flushes
        assert len(batch) == 2, "a and c stay queued; b is reported by the exception"
        # The request fails in every round: its writes are reported, as unprocessed ones are.
        testing.inject_fault(client, operation="BatchWriteItem", error="internal", times=1000)
        with pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await batch.flush()
        assert isinstance(caught.value.error, dynamoxy.InternalServerError)
        assert caught.value.__cause__ is caught.value.error
        assert caught.value.retryable
        assert sorted(item["note_id"] for _, item in caught.value.unprocessed) == ["a", "c"]
        assert len(batch) == 0, "the exception reports the writes of the request that failed"
        assert (a.is_loaded, c.is_loaded) == (False, False)
        client.store.clear_faults()  # type: ignore[union-attr]
        await batch.put(a)
        await batch.put(c)
        await batch.flush()
        assert sorted(item["note_id"] for item in await client.table("notes").scan().all()) == ["a", "c"]
        assert batch.written == 2


async def test_blocks_that_raise_keep_their_writes_queued() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(Note)
        # The block ends with the exception of a failed automatic flush: nothing more is written,
        # and the exception reports what that flush did not write.
        batch = client.batch_writer(flush_size=2)

        async def flushing_block() -> None:
            async with batch:
                await batch.put(Note(note_id="a"))
                await batch.put(Note(note_id="b"))  # flushes, and fails

        testing.inject_fault(client, operation="BatchWriteItem", error="access_denied")
        with pytest.raises(dynamoxy.UnprocessedItemsError) as caught:
            await flushing_block()
        assert isinstance(caught.value.error, dynamoxy.AccessDeniedError)
        assert not caught.value.retryable
        assert len(batch) == 0, "the exception reports what the flush did not write"
        other = client.batch_writer()

        async def raising_block() -> None:
            async with other:
                await other.put(Note(note_id="c"))
                raise RuntimeError("stop")

        with pytest.raises(RuntimeError, match="stop"):
            await raising_block()
        assert len(other) == 1
        assert await client.table("notes").scan().all() == [], "a block that raises writes nothing"
        for _, item in caught.value.unprocessed:  # written again on request
            await batch.put_item("notes", item, key={"note_id": item["note_id"]})
        await other.flush()
        assert sorted(item["note_id"] for item in await client.table("notes").scan().all()) == ["a", "b", "c"]
        assert (len(batch), len(other)) == (0, 0)
