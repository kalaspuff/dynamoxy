# Testing

## The in-memory DynamoDB

`dynamoxy.testing.memory_client()` returns a `Client` backed by an in-memory DynamoDB written in
Rust. It implements the DynamoDB API with its semantics: key and item validation, the full
expression language, conditional writes and `ReturnValues`, global and local indexes with
projections, 1 MB pages and `Limit`, batches, transactions with cancellation reasons and
idempotency tokens, TTL, PartiQL (a subset, with `ORDER BY`), streams settings, backups,
consumed capacity and DynamoDB's error codes and messages. Tests need no network, credentials,
Docker or emulator.

```python
import pytest

import dynamoxy
from dynamoxy import testing


@pytest.fixture
async def client():
    async with testing.memory_client() as client:
        await client.create_tables(User, Order)
        yield client


async def test_signup_rejects_duplicate_emails(client: dynamoxy.Client) -> None:
    service = SignupService(UserRepository(client))
    await service.sign_up("u1", "ada@example.com", "Ada")
    with pytest.raises(EmailTakenError):
        await service.sign_up("u2", "ada@example.com", "Bob")
```

Every memory client has its own store unless you share one (`store=dynamoxy.MemoryStore()`),
for example between an application under test and the test's assertions. Configure pytest with
`asyncio_mode = "auto"` (pytest-asyncio) for async tests.

## Simulating failures

```python
testing.inject_fault(client, operation="PutItem", error="throttling", times=3)
testing.inject_fault(client, table=client.table_name("users"), error="internal")
testing.inject_fault(client, operation="BatchWriteItem", unprocessed=True, times=2)
testing.inject_fault(client, operation="GetItem", latency=0.5)        # with Client(timeout=0.1)
testing.inject_fault(client, error="ConditionalCheckFailedException")  # any DynamoDB error code
testing.inject_fault(client, operation="UpdateItem", lose_response=True)  # applied, then lost
```

Faults apply to the next `times` matching calls (attempts count, so throttling twice with four
attempts still succeeds). A fault for a `table=` matches every call that uses the table, also
among others: a batch or transaction of several tables, or a PartiQL statement on it. With
`unprocessed=True` only that table's requests of a batch come back unprocessed. `lose_response=True` applies the write and then loses its response,
as a dropped connection does: use it to test how your code handles
[writes with unknown outcome](errors-and-retries.md#writes-with-unknown-outcome).

## Time to Live

`testing.purge_expired(client, at=datetime)` deletes items whose TTL is at or before `at` (now by
default), as DynamoDB's background process would. Like DynamoDB, it leaves items whose TTL is
more than five years in the past, such as `0` or a date decades ago: they are never considered
for expiry.

## Events

```python
with testing.capture_events(client, "request.*") as events:
    await users.put(user)
assert [event.kind for event in events] == [dynamoxy.EventKind.REQUEST_RETRY, ...]
```

## Cost assertions

`dynamoxy.collect()` makes performance regressions testable:

```python
with dynamoxy.collect() as usage:
    await service.dashboard("u1")
assert usage.calls <= 3
assert usage.by_operation().get("Scan", 0) == 0
```

## Emulators

For end-to-end tests against a real protocol implementation, point the client at an emulator:

```python
dynamoxy.Client(endpoint_url="http://127.0.0.1:4566", region="us-east-1",
                credentials=dynamoxy.Credentials("test", "test"), table_prefix=f"test-{uuid4().hex[:8]}-")
```

[MiniStack](https://pypi.org/project/ministack/) is a pip-installable AWS emulator (no Docker)
that dynamoxy's own test suite uses; DynamoDB Local works too. Use a unique `table_prefix` per
test run and delete the tables afterwards.

## Testing hooks and validators

Model validation and hooks run without I/O when you construct instances or call
`instance.validate()`; test validators with plain unit tests. Client hooks can be tested with
the memory client and `capture_events` or by asserting their side effects.
