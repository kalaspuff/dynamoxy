"""Extension points around operations: middleware and lifecycle hooks.

Middleware wraps every DynamoDB call; lifecycle hooks run around model writes, deletes and loads.

**Middleware** wraps each call to DynamoDB. It sees the :class:`Request` (operation, table,
the parameters of the call, which it may change) and returns the :class:`Response`::

    async def tenant_guard(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
        if request.table and not request.table.startswith(current_tenant()):
            raise PermissionError(request.table)
        return await call_next(request)


    client.use(tenant_guard)

Typical uses: auditing, access control, caching ``GetItem``, custom retries, fault injection.

**Lifecycle hooks** run around model persistence: ``before_save``/``after_save`` (create, put,
save, batch puts), ``before_update``/``after_update`` (updates of instances, including
``add()``, ``increment()`` and ``decrement()``), ``before_delete``/``after_delete`` and
``after_load``. Declare them on the model (decorated methods, see :func:`dynamoxy.before_save`)
or on the client for any model::

    @client.hook("after_update", User)
    async def audit(user: User, context: dynamoxy.HookContext) -> None: ...
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dynamoxy._utils import maybe_await

if TYPE_CHECKING:
    from dynamoxy.client import Client
    from dynamoxy.results import CallStats
    from dynamoxy.schema import HookPoint

__all__ = ["CallNext", "HookContext", "HookRegistry", "Middleware", "Request", "Response"]


@dataclass(eq=False)
class Request:
    """One call to DynamoDB, as middleware sees it.

    Attributes:
        operation: The DynamoDB operation (``"PutItem"``).
        method: The native method that performs it (``"put_item"``).
        table: The physical table name, when the call targets one table.
        params: Keyword arguments of the call (DynamoDB's parameters in snake_case, plain Python
            values). Middleware may change them.
        model: The model class, when a repository made the call.
        context: Free-form data shared by the middleware of this call.
    """

    operation: str
    method: str
    table: str | None
    params: dict[str, Any]
    model: type | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(eq=False)
class Response:
    """The result of a call: DynamoDB's response (snake_case, plain Python values) and its cost.

    Attributes:
        output: The response.
        stats: Attempts, throttles, requests and duration.
    """

    output: Any
    stats: CallStats


type CallNext = Callable[[Request], Awaitable[Response]]
type Middleware = Callable[[Request, CallNext], Awaitable[Response]]


@dataclass(frozen=True, slots=True)
class HookContext:
    """What a client-level lifecycle hook is told about the operation.

    Attributes:
        operation: ``"create"``, ``"put"``, ``"save"``, ``"update"``, ``"delete"``,
            ``"batch_put"``, ``"batch_delete"`` or ``"load"`` (a write in a transaction reports
            its own operation).
        model: The model class.
        client: The client.
    """

    operation: str
    model: type
    client: Client


type ClientHook = Callable[[Any, HookContext], Any]


class HookRegistry:
    """Client-level lifecycle hooks."""

    def __init__(self) -> None:
        self._hooks: list[tuple[HookPoint, type | None, ClientHook]] = []
        # Matching hooks, and whether any hook runs, by (point, model class); rebuilt when hooks
        # are added or removed (a model's own hooks never change).
        self._matches: dict[tuple[HookPoint, type], tuple[ClientHook, ...]] = {}
        self._any: dict[tuple[HookPoint, type], bool] = {}

    def add(self, point: HookPoint, hook: ClientHook, model: type | None = None) -> Callable[[], None]:
        """Registers ``hook`` (sync or async) for ``point``, for one model or every model.

        Returns:
            A function that removes the hook.

        Raises:
            ValueError: ``point`` is not a hook point.
        """
        from dynamoxy.schema import HOOK_POINTS

        if point not in HOOK_POINTS:
            raise ValueError(f"unknown hook point {point!r}; use one of {', '.join(HOOK_POINTS)}")
        entry = (point, model, hook)
        self._hooks.append(entry)
        self._matches.clear()
        self._any.clear()

        def remove() -> None:
            if entry in self._hooks:
                self._hooks.remove(entry)
                self._matches.clear()
                self._any.clear()

        return remove

    def _matching(self, point: HookPoint, model: type) -> tuple[ClientHook, ...]:
        hooks = self._matches.get((point, model))
        if hooks is None:
            hooks = self._matches[(point, model)] = tuple(
                hook
                for entry_point, target, hook in self._hooks
                if entry_point == point and (target is None or issubclass(model, target))
            )
        return hooks

    def matching(self, point: HookPoint, model: type) -> list[ClientHook]:
        """The hooks for ``point`` that apply to ``model`` (subclasses included)."""
        return list(self._matching(point, model))

    async def run(self, point: HookPoint, instance: Any, context: HookContext) -> None:
        """Runs the model's own hooks, then the client's hooks."""
        from dynamoxy.schema import schema_of

        model = type(instance)
        for method in schema_of(model).hooks.get(point, ()):
            await maybe_await(method(instance))
        for hook in self._matching(point, model):
            await maybe_await(hook(instance, context))

    def run_sync(self, point: HookPoint, instance: Any, context: HookContext) -> None:
        """Runs ``after_load`` hooks, which must be synchronous.

        Raises:
            TypeError: A hook returned an awaitable.
        """
        from dynamoxy.schema import schema_of

        model = type(instance)
        hooks: list[Callable[[], Any]] = [
            lambda method=method: method(instance) for method in schema_of(model).hooks.get(point, ())
        ]
        hooks += [lambda hook=hook: hook(instance, context) for hook in self._matching(point, model)]
        for call in hooks:
            result = call()
            if isinstance(result, Awaitable):
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                raise TypeError(f"{point} hooks must be synchronous (they run while items are decoded)")

    def has(self, point: HookPoint, model: type) -> bool:
        """Whether any hook would run for ``point`` and ``model``."""
        found = self._any.get((point, model))
        if found is None:
            from dynamoxy.schema import schema_of

            found = self._any[(point, model)] = bool(schema_of(model).hooks.get(point)) or bool(
                self._matching(point, model)
            )
        return found
