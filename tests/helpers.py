"""Helpers shared by integration and e2e tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime


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
