"""Batch writes: many puts and deletes with ``BatchWriteItem`` (25 per request, parallel).

::

    async with client.batch_writer() as batch:
        for user in users:
            await batch.put(user)  # flushes every `flush_size` writes
        await batch.delete(old_user)
        await batch.put_item("events", {"pk": "e1", "sk": 1})

Batches are not atomic and have no conditions. Requests for the same key are de-duplicated
(the last one wins). Unprocessed items are resent with backoff; whatever remains after every
round is raised as :class:`dynamoxy.UnprocessedItemsError`. Models with unique attributes
cannot be written in batches.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, Self

from dynamoxy.errors import UnprocessedItemsError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from dynamoxy.client import Client
    from dynamoxy.repository import Repository

__all__ = ["BatchWriter"]


class BatchWriter:
    """Buffers writes and sends them with managed batches.

    Flushes when ``flush_size`` writes are buffered and when the ``async with`` block ends.
    """

    def __init__(self, client: Client, *, flush_size: int = 100) -> None:
        if flush_size < 1:
            raise ValueError(f"flush_size must be at least 1, got {flush_size}")
        self.client = client
        self.flush_size = flush_size
        self._pending: dict[tuple[str, str], tuple[str, str, dict[str, Any], Any]] = {}
        self._anonymous = itertools.count()
        self._repositories: dict[type, Repository[Any]] = {}
        self.written = 0

    def __len__(self) -> int:
        return len(self._pending)

    def _repository(self, model: type) -> Repository[Any]:
        repository = self._repositories.get(model)
        if repository is None:
            repository = self.client.repository(model)
            if repository.schema.unique_fields:
                raise TypeError(f"{model.__name__} has unique attributes; batch writes cannot maintain them")
            self._repositories[model] = repository
        return repository

    async def _queue(self, table: str, kind: str, item: dict[str, Any], identity: str, instance: Any = None) -> None:
        self._pending.pop((table, identity), None)
        self._pending[(table, identity)] = (table, kind, item, instance)
        if len(self._pending) >= self.flush_size:
            await self.flush()

    @staticmethod
    def _identity(key: Mapping[str, Any]) -> str:
        return repr(sorted(key.items(), key=lambda pair: pair[0]))

    async def put(self, item: Any) -> None:
        """Queues a model instance (validated now; hooks run at flush)."""
        repository = self._repository(type(item))
        repository.schema.validate(item)
        await self._queue(repository.table_name, "put", {}, self._identity(repository.schema.key_of(item)), item)

    async def delete(self, target: Any, *, model: type | None = None) -> None:
        """Queues a delete by instance, or by stored key with ``model=``."""
        resolved = model or type(target)
        repository = self._repository(resolved)
        key = repository._target_key(target)
        instance = target if isinstance(target, resolved) else None
        await self._queue(repository.table_name, "delete", key, self._identity(key), instance)

    async def put_item(self, table: str, item: Mapping[str, Any], *, key: Mapping[str, Any] | None = None) -> None:
        """Queues a raw item for a table (logical name). Give its ``key`` to de-duplicate."""
        physical = self.client.table_name(table)
        identity = self._identity(key) if key is not None else f"#{next(self._anonymous)}"
        await self._queue(physical, "put", dict(item), identity)

    async def delete_item(self, table: str, key: Mapping[str, Any]) -> None:
        """Queues a raw delete."""
        physical = self.client.table_name(table)
        await self._queue(physical, "delete", dict(key), self._identity(key))

    async def flush(self) -> None:
        """Writes everything queued.

        Raises:
            UnprocessedItemsError: Some writes stayed unprocessed after every retry round.
        """
        pending, self._pending = list(self._pending.values()), {}
        if not pending:
            return
        requests: list[tuple[str, str, dict[str, Any]]] = []
        finalizers: list[Any] = []
        hooks = self.client.hooks
        for table, kind, item, instance in pending:
            if instance is not None:
                model = type(instance)
                repository = self._repositories.get(model) or self.client.repository(model)
                if kind == "put":
                    if hooks.has("before_save", model):
                        await hooks.run("before_save", instance, repository._context("batch_put"))
                    item, changes, _ = repository._prepare(instance, "batch")
                    finalizers.append((repository, instance, changes, item, "after_save"))
                else:
                    if hooks.has("before_delete", model):
                        await hooks.run("before_delete", instance, repository._context("batch_delete"))
                    finalizers.append((repository, instance, {}, None, "after_delete"))
            requests.append((table, kind, item))
        output = await self.client.call("batch_write", "BatchWriteItem", requests=requests)
        self.written += len(requests) - len(output["unprocessed"])
        for repository, instance, changes, item, hook in finalizers:
            repository._apply(instance, changes, item)
            if hooks.has(hook, type(instance)):
                await hooks.run(
                    hook, instance, repository._context("batch_put" if hook == "after_save" else "batch_delete")
                )
        if output["unprocessed"]:
            raise UnprocessedItemsError(
                f"{len(output['unprocessed'])} writes stayed unprocessed (throttling)",
                unprocessed=[(kind, item) for _, kind, item in output["unprocessed"]],
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is None:
            await self.flush()
