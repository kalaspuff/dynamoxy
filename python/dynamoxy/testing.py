"""Testing helpers for applications that use dynamoxy.

:func:`memory_client` returns a :class:`dynamoxy.Client` backed by an in-memory DynamoDB written
in Rust: the same semantics, limits, error codes and messages as DynamoDB (conditions,
transactions, indexes, paging, capacity), no network, deterministic and fast.

::

    async def test_signup() -> None:
        async with dynamoxy.testing.memory_client() as client:
            await client.create_tables(User)
            users = client.model(User)
            await users.create(User(user_id="u1", email="a@example.com", name="Ada"))
            with pytest.raises(dynamoxy.UniqueConstraintError):
                await users.create(User(user_id="u2", email="a@example.com", name="Bob"))

Simulate failures with :func:`inject_fault` (throttling, unprocessed batch items, latency) and
Time to Live deletion with :func:`purge_expired`. Several clients can share one
:class:`MemoryStore`.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dynamoxy._core import MemoryStore
from dynamoxy.client import Client, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dynamoxy.events import Event

__all__ = ["MemoryStore", "capture_events", "inject_fault", "memory_client", "purge_expired"]

_ERROR_CODES = {
    "throttling": "ProvisionedThroughputExceededException",
    "request_limit": "RequestLimitExceeded",
    "internal": "InternalServerError",
    "unavailable": "ServiceUnavailable",
    "conflict": "TransactionConflictException",
    "access_denied": "AccessDeniedException",
}


def memory_client(*, store: MemoryStore | None = None, **options: Any) -> Client:
    """A client backed by an in-memory DynamoDB (a new :class:`MemoryStore` unless given).

    Retries keep their default policy, with shorter delays so tests stay fast; pass ``retry=``
    to change it. Every other ``Client`` option is accepted.
    """
    options.setdefault("retry", RetryPolicy(base_delay=0.001, max_delay=0.01))
    return Client(store=store if store is not None else MemoryStore(), **options)


def _store(target: Client | MemoryStore) -> MemoryStore:
    if isinstance(target, MemoryStore):
        return target
    if target.store is None:
        raise TypeError("this helper needs an in-memory client (dynamoxy.testing.memory_client())")
    return target.store


def inject_fault(
    target: Client | MemoryStore,
    *,
    operation: str | None = None,
    table: str | None = None,
    error: str | None = None,
    message: str | None = None,
    unprocessed: bool = False,
    latency: float | None = None,
    lose_response: bool = False,
    times: int = 1,
) -> None:
    """Makes the next ``times`` matching calls fail, wait, lose their response or skip batch items.

    Args:
        target: An in-memory client or store.
        operation: DynamoDB operation (``"PutItem"``); every operation when omitted.
        table: Physical table name; every table when omitted. A call matches when it uses the
            table among others (a batch or transaction of several tables, a PartiQL statement
            on it); ``unprocessed=True`` then returns only that table's requests unprocessed.
        error: A shortcut (``"throttling"``, ``"internal"``, ``"conflict"``, ...) or any
            DynamoDB error code.
        message: Error message.
        unprocessed: Batch operations return every request unprocessed.
        latency: Seconds to wait before the call runs.
        lose_response: The call runs, then its response is lost, as when a connection drops: the
            client sees a ``TransportError`` (and retries) although the write was applied.
        times: Number of calls affected.
    """
    code = _ERROR_CODES.get(error, error) if error is not None else None
    _store(target).inject_fault(
        operation=operation,
        table=table,
        error_code=code,
        message=message,
        unprocessed=unprocessed,
        latency=latency,
        lose_response=lose_response,
        times=times,
    )


def purge_expired(target: Client | MemoryStore, at: datetime | float | None = None) -> int:
    """Deletes expired items, as DynamoDB eventually does.

    Items whose TTL attribute is at or before ``at`` (default: now), in tables with TTL enabled.
    Like DynamoDB, it leaves items whose TTL is more than five years before ``at`` (``0``, or a
    time in milliseconds): those are never considered for expiry. Returns how many were deleted.
    """
    moment = at.timestamp() if isinstance(at, datetime) else at
    return _store(target).purge_expired(moment)


@contextlib.contextmanager
def capture_events(client: Client, pattern: str = "*") -> Iterator[list[Event]]:
    """Collects the events matching ``pattern`` emitted inside the block.

    Example::

        with capture_events(client, "request.*") as events:
            await users.put(user)
        assert [event.kind for event in events] == [...]
    """
    events: list[Event] = []
    unsubscribe = client.on(pattern, events.append)
    try:
        yield events
    finally:
        unsubscribe()
