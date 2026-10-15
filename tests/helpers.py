"""Helpers shared by integration and e2e tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

import dynamoxy

READS = frozenset({"GetItem", "BatchGetItem", "Query", "Scan"})


async def wait_for(condition: Callable[[], bool], timeout: float = 15.0, interval: float = 0.02) -> None:
    """Waits until ``condition()`` is true (never sleep for a fixed time to "let things happen")."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(interval)


def at(day: int, hour: int = 0) -> datetime:
    """A fixed UTC datetime in January 2026 (deterministic sort keys)."""
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def record_consistent_reads(client: dynamoxy.Client) -> list[tuple[str, bool]]:
    """``(operation, ConsistentRead)`` of every read ``client`` sends from now on (batch gets of one table)."""
    sent: list[tuple[str, bool]] = []

    async def record(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        if request.operation in READS:
            params = request.params
            if request.operation == "BatchGetItem":
                (params,) = params["request_items"].values()
            sent.append((request.operation, params["consistent_read"]))
        return await call_next(request)

    client.use(record)
    return sent
