"""Internal helpers (not part of the public API)."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["MISSING", "LoopLocal", "Missing", "maybe_await", "number_text", "seconds", "stored_identity"]


class Missing:
    """Sentinel type for "no value given" (``None`` is a valid value)."""

    _instance: Any = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = Missing()


class LoopLocal[T]:
    """Holds one value per running event loop (for loop-bound asyncio primitives).

    asyncio locks, events and queues bind to the loop that first uses them. Clients may be
    used from several loops (e.g. one per test), so loop-bound helpers are created per loop.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._values: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, T] = weakref.WeakKeyDictionary()

    def get(self) -> T:
        """Returns the value for the running loop, creating it on first use."""
        loop = asyncio.get_running_loop()
        try:
            return self._values[loop]
        except KeyError:
            value = self._factory()
            self._values[loop] = value
            return value


def seconds(value: float | timedelta | None, name: str, *, allow_zero: bool = False) -> float | None:
    """Validates a duration given as seconds or ``timedelta`` and returns float seconds.

    Raises:
        ValueError: The duration is negative (or zero when not allowed), or not finite.
    """
    if value is None:
        return None
    result = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if result != result or result in (float("inf"), float("-inf")):  # NaN or infinite
        raise ValueError(f"{name} must be a finite number of seconds, got {value!r}")
    if result < 0 or (result == 0 and not allow_zero):
        bound = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {bound} number of seconds, got {value!r}")
    return result


async def maybe_await[T](value: T | Awaitable[T]) -> T:
    """Awaits ``value`` when it is awaitable (sync or async callbacks)."""
    if inspect.isawaitable(value):
        return await value
    return value


def number_text(value: float | Decimal) -> str:
    """The text DynamoDB stores a number as: no exponent, no trailing fractional zeros, ``0`` for zero.

    ``Decimal("1.50")``, ``1.5`` and ``Decimal("15E-1")`` are all ``"1.5"``; ``1e16`` is
    ``"10000000000000000"``. Floats are converted by the core, exactly as when they are stored
    (its shortest representation breaks ties differently from ``repr()``).
    """
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        from dynamoxy import _core

        return _core.value_to_wire(value)["N"]
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def stored_identity(value: Any) -> Any:
    """A hashable form of a stored value that compares like DynamoDB compares values.

    Numbers are equal when DynamoDB's canonical numbers are (``5``, ``5.0`` and
    ``Decimal("5.00")``), sets are unordered and maps compare by their members.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return ("N", number_text(value))
    if isinstance(value, (set, frozenset)):
        return frozenset(stored_identity(element) for element in value)
    if isinstance(value, (list, tuple)):
        return tuple(stored_identity(element) for element in value)
    if isinstance(value, dict):
        return ("M", frozenset((name, stored_identity(element)) for name, element in value.items()))
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value
