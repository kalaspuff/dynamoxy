"""Events: what dynamoxy does, for logging and hooks.

The Rust core records its events (retries, throttling, unprocessed batch items, table changes) in
a bounded buffer; the client pulls them and dispatches them on the event loop, so Rust threads
never call into Python. The Python layer adds its own events (every operation's outcome, the
client's lifecycle, hook failures).

Subscribe with :meth:`dynamoxy.Client.on`::

    @client.on("request.*")
    def log_retries(event: dynamoxy.Event) -> None:
        print(event.kind, event.fields)

Events are logged through the standard :mod:`logging` module, one logger per category
(``dynamoxy.request``, ``dynamoxy.operation``, ...). Fields carry names, ids, sizes and
durations - never item contents.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["Event", "EventDispatcher", "EventHook", "EventKind", "LoggingConfig"]

_logger = logging.getLogger("dynamoxy")


class EventKind(StrEnum):
    """Every kind of event.

    The first members mirror ``EventKind`` in ``crates/dynamoxy-core/src/events.rs`` in the same
    order (``scripts/check_consistency.py`` checks it); the members after them are emitted by the
    Python layer.
    """

    REQUEST_RETRY = "request.retry"
    REQUEST_THROTTLED = "request.throttled"
    REQUEST_GAVE_UP = "request.gave_up"
    REQUEST_OUTCOME_UNKNOWN = "request.outcome_unknown"
    REQUEST_SLOW = "request.slow"
    BATCH_UNPROCESSED = "batch.unprocessed"
    BATCH_INCOMPLETE = "batch.incomplete"
    TABLE_CREATED = "table.created"
    TABLE_UPDATED = "table.updated"
    TABLE_DELETED = "table.deleted"
    # Emitted by the Python layer:
    CLIENT_CONNECTED = "client.connected"
    CLIENT_CLOSED = "client.closed"
    OPERATION_SUCCEEDED = "operation.succeeded"
    OPERATION_FAILED = "operation.failed"
    HOOK_FAILED = "hook.failed"
    EVENTS_DROPPED = "events.dropped"


#: Default level and description of the kinds the Python layer emits (the Rust kinds come from
#: ``dynamoxy._core.event_kinds()``).
PYTHON_EVENT_KINDS: dict[EventKind, tuple[str, str]] = {
    EventKind.CLIENT_CONNECTED: ("info", "The client connected to DynamoDB (or created its in-memory backend)."),
    EventKind.CLIENT_CLOSED: ("info", "The client was closed."),
    EventKind.OPERATION_SUCCEEDED: (
        "debug",
        "An operation succeeded (operation, table, duration, attempts, consumed capacity, item counts).",
    ),
    EventKind.OPERATION_FAILED: ("info", "An operation failed (operation, table, error code, attempts)."),
    EventKind.HOOK_FAILED: ("error", "An event hook raised an exception (the operation was not affected)."),
    EventKind.EVENTS_DROPPED: ("warning", "Core events were dropped because the event buffer was full."),
}

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened.

    Attributes:
        kind: What happened.
        message: Human readable summary.
        fields: Structured context (names, ids, sizes, durations; never item contents).
        level: Severity (``"debug"``, ``"info"``, ``"warning"``, ``"error"``).
        timestamp: When the Python layer received the event (seconds since the epoch).
    """

    kind: EventKind
    message: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    level: str = "info"
    timestamp: float = field(default_factory=time.time)

    @property
    def category(self) -> str:
        """The part before the dot (``"request"`` for ``request.retry``)."""
        return self.kind.value.split(".", 1)[0]


type EventHook = Callable[[Event], None] | Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """How events are logged.

    Each event is logged to the logger ``dynamoxy.<category>`` at its level, with the event in
    ``record.dynamoxy`` and (by default) its fields in the message.

    Attributes:
        enabled: Log events at all.
        levels: Level overrides by kind or glob (``{"request.retry": "debug", "operation.*": "info"}``).
        disabled: Kinds or globs never logged.
        include_fields: Append ``key=value`` fields to the message.
    """

    enabled: bool = True
    levels: Mapping[str, str] = field(default_factory=dict)
    disabled: tuple[str, ...] = ()
    include_fields: bool = True

    def __post_init__(self) -> None:
        for pattern, level in self.levels.items():
            if level not in _LEVELS:
                raise ValueError(f"LoggingConfig.levels[{pattern!r}] must be one of {sorted(_LEVELS)}, got {level!r}")

    def level_for(self, kind: EventKind, default: str) -> str | None:
        """The level to log ``kind`` at, or ``None`` when it is not logged."""
        if not self.enabled or any(fnmatch.fnmatchcase(kind.value, pattern) for pattern in self.disabled):
            return None
        for pattern, level in self.levels.items():
            if fnmatch.fnmatchcase(kind.value, pattern):
                return level
        return default


def _default_levels() -> dict[EventKind, str]:
    from dynamoxy._core import event_kinds

    levels = {EventKind(name): level for name, level, _ in event_kinds()}
    levels.update({kind: level for kind, (level, _) in PYTHON_EVENT_KINDS.items()})
    return levels


class _Route:
    """Where events of one kind go: the matching hooks and the logger (cached per kind)."""

    __slots__ = ("hooks", "level", "logger")

    def __init__(self, hooks: tuple[EventHook, ...], logger: logging.Logger | None, level: int) -> None:
        self.hooks = hooks
        self.logger = logger
        self.level = level


class EventDispatcher:
    """Routes events to logging and hooks. A failing hook never breaks the library.

    Sync hooks run inline; async hooks run as tasks on the event loop (``drain()`` waits for them).
    """

    def __init__(self, logging_config: LoggingConfig | None = None) -> None:
        self.logging = logging_config if logging_config is not None else LoggingConfig()
        self._hooks: list[tuple[str, EventHook]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._levels = _default_levels()
        self._on_change: Callable[[], None] | None = None
        # Routes by kind for the logging configuration in `_routes_logging` and the current hooks
        # (pattern matching is the costly part; logger levels are still checked on every event).
        self._routes: dict[EventKind, _Route] = {}
        self._routes_logging: LoggingConfig | None = None
        self._routes_levels: dict[str, str] = {}

    def on_change(self, callback: Callable[[], None]) -> None:
        """Calls ``callback`` whenever the set of wanted kinds may have changed."""
        self._on_change = callback

    def subscribe(self, pattern: str | EventKind, hook: EventHook) -> Callable[[], None]:
        """Calls ``hook`` for every event whose kind matches ``pattern`` (a kind or a glob).

        Returns:
            A function that removes the subscription.

        Raises:
            ValueError: ``pattern`` matches no event kind (probably a typo).
        """
        text = pattern.value if isinstance(pattern, EventKind) else pattern
        if not any(fnmatch.fnmatchcase(kind.value, text) for kind in EventKind):
            known = ", ".join(kind.value for kind in EventKind)
            raise ValueError(f"event pattern {text!r} matches no event kind; kinds: {known}")
        entry = (text, hook)
        self._hooks.append(entry)
        self._changed()

        def unsubscribe() -> None:
            if entry in self._hooks:
                self._hooks.remove(entry)
                self._changed()

        return unsubscribe

    def _changed(self) -> None:
        self._routes.clear()
        if self._on_change is not None:
            self._on_change()

    def _route(self, kind: EventKind) -> _Route:
        config = self.logging
        # The configuration is frozen, but its `levels` mapping could still be changed in place.
        if self._routes_logging is not config or self._routes_levels != config.levels:
            self._routes.clear()
            self._routes_logging = config
            self._routes_levels = dict(config.levels)
        route = self._routes.get(kind)
        if route is None:
            hooks = tuple(hook for pattern, hook in self._hooks if fnmatch.fnmatchcase(kind.value, pattern))
            level = self.logging.level_for(kind, self.level(kind))
            logger = None if level is None else logging.getLogger(f"dynamoxy.{kind.value.split('.', 1)[0]}")
            route = self._routes[kind] = _Route(hooks, logger, _LEVELS[level] if level is not None else 0)
        return route

    def level(self, kind: EventKind) -> str:
        """The default level of ``kind``."""
        return self._levels.get(kind, "info")

    def wants(self, kind: EventKind) -> bool:
        """Whether an event of ``kind`` would be logged or reach a hook (cheap to build otherwise)."""
        route = self._route(kind)
        if route.hooks:
            return True
        logger = route.logger
        return logger is not None and logger.isEnabledFor(route.level)

    def wanted(self) -> list[EventKind]:
        """The core kinds somebody listens to (only these are recorded by the core).

        Logging is cheap to filter in Python, so every core kind that logging may want is enabled.
        """
        from dynamoxy._core import event_kinds

        core = [EventKind(name) for name, _, _ in event_kinds()]
        return [
            kind
            for kind in core
            if self.logging.level_for(kind, self.level(kind)) is not None
            or any(fnmatch.fnmatchcase(kind.value, pattern) for pattern, _ in self._hooks)
        ]

    def emit(self, kind: EventKind, message: str, fields: Mapping[str, Any] | None = None) -> None:
        """Builds and dispatches an event (Python-side kinds)."""
        if not self.wants(kind):
            return
        self.dispatch_event(Event(kind=kind, message=message, fields=dict(fields or {}), level=self.level(kind)))

    def dispatch(self, raw: Iterable[tuple[str, str, Mapping[str, str]]]) -> None:
        """Delivers raw events from the core."""
        for name, message, fields in raw:
            try:
                kind = EventKind(name)
            except ValueError:  # a newer core with kinds this Python layer does not know
                continue
            self.dispatch_event(Event(kind=kind, message=message, fields=dict(fields), level=self.level(kind)))

    def dispatch_event(self, event: Event) -> None:
        """Logs ``event`` and calls the hooks that match it."""
        self._log(event)
        for hook in self._route(event.kind).hooks:
            try:
                result = hook(event)
            except Exception as error:  # noqa: BLE001 - a failing hook must never break the library
                self._hook_failed(event, hook, error)
                continue
            if isinstance(result, Awaitable):
                self._schedule(event, hook, result)

    def _schedule(self, event: Event, hook: EventHook, result: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await result
            except Exception as error:  # noqa: BLE001 - a failing hook must never break the library
                self._hook_failed(event, hook, error)

        try:
            task = asyncio.get_running_loop().create_task(run(), name=f"dynamoxy-hook-{event.kind.value}")
        except RuntimeError:  # no running loop: run to completion now
            asyncio.run(run())
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _hook_failed(self, event: Event, hook: EventHook, error: Exception) -> None:
        name = getattr(hook, "__qualname__", repr(hook))
        if event.kind is EventKind.HOOK_FAILED:  # never recurse
            _logger.error("event hook %s failed while handling hook.failed: %r", name, error)
            return
        failure = Event(
            kind=EventKind.HOOK_FAILED,
            message=f"event hook {name} failed for {event.kind.value}: {error!r}",
            fields={"hook": name, "event": event.kind.value, "error": type(error).__name__},
            level=self.level(EventKind.HOOK_FAILED),
        )
        self._log(failure, exc_info=error)
        for pattern, other in list(self._hooks):
            if other is not hook and fnmatch.fnmatchcase(failure.kind.value, pattern):
                try:
                    result = other(failure)
                    if isinstance(result, Awaitable):
                        self._schedule(failure, other, result)
                except Exception:
                    _logger.exception("event hook %r failed for hook.failed", other)

    def _log(self, event: Event, exc_info: BaseException | None = None) -> None:
        if event.level == self.level(event.kind):
            route = self._route(event.kind)
            logger, level = route.logger, route.level
            if logger is None:
                return
        else:
            level_name = self.logging.level_for(event.kind, event.level)
            if level_name is None:
                return
            logger = logging.getLogger(f"dynamoxy.{event.category}")
            level = _LEVELS[level_name]
        if not logger.isEnabledFor(level):
            return
        message = event.message
        if self.logging.include_fields and event.fields:
            message = f"{message} " + " ".join(f"{key}={value}" for key, value in event.fields.items())
        logger.log(level, "%s", message, extra={"dynamoxy": event}, exc_info=exc_info)

    async def drain(self) -> None:
        """Waits for async hooks that are still running."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
