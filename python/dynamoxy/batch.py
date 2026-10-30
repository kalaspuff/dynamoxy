"""Batch writes: many puts and deletes with ``BatchWriteItem`` (25 per request, parallel).

::

    async with client.batch_writer() as batch:
        for user in users:
            await batch.put(user)  # flushes every `flush_size` writes
        await batch.delete(old_user)
        await batch.put_item("events", {"pk": "e1", "sk": 1})

Batches are not atomic and have no conditions. Requests for the same key are de-duplicated
(the last one wins). Unprocessed items are resent with backoff; whatever remains after every
round is raised as :class:`dynamoxy.UnprocessedItemsError`, and only the writes that were made
update their instances and run ``after_*`` hooks. A flush that fails before anything is sent
(a ``before_*`` hook or a validation raised) or whose request fails keeps the writes queued:
nothing is dropped. Models with unique attributes cannot be written in batches.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, Self

from dynamoxy._utils import stored_identity
from dynamoxy.errors import UnprocessedItemsError
from dynamoxy.schema import bound_repository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from dynamoxy.client import Client
    from dynamoxy.repository import Repository
    from dynamoxy.schema import HookPoint

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
        self._pending: dict[tuple[str, Any], tuple[str, str, dict[str, Any], Any]] = {}
        self._anonymous = itertools.count()
        self._repositories: dict[type, Repository[Any]] = {}
        self.written = 0

    def __len__(self) -> int:
        return len(self._pending)

    def _repository(self, model: type, target: Any = None) -> Repository[Any]:
        """The repository that writes ``target``: the one it is bound to, if it uses this client.

        Raises:
            TypeError: The model has unique attributes.
        """
        repository = self._repositories.get(model)
        if repository is None:
            repository = self.client.model(model)
            if repository.schema.unique_fields:
                raise TypeError(f"{model.__name__} has unique attributes; batch writes cannot maintain them")
            self._repositories[model] = repository
        return bound_repository(target, model, self.client) or repository

    async def _queue(self, table: str, kind: str, item: dict[str, Any], identity: Any, instance: Any = None) -> None:
        self._pending.pop((table, identity), None)
        self._pending[(table, identity)] = (table, kind, item, instance)
        if len(self._pending) >= self.flush_size:
            await self.flush()

    @staticmethod
    def _identity(key: Mapping[str, Any]) -> Any:
        return stored_identity(dict(key))

    async def put(self, item: Any) -> None:
        """Queues a model instance (validated now; hooks run at flush)."""
        repository = self._repository(type(item), item)
        repository.schema.validate(item)
        await self._queue(repository.table_name, "put", {}, self._identity(repository.schema.key_of(item)), item)

    async def delete(self, target: Any, *, model: type | None = None) -> None:
        """Queues a delete by instance, or by stored key with ``model=``."""
        resolved = model or type(target)
        repository = self._repository(resolved, target)
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
            UnprocessedItemsError: Some writes stayed unprocessed after every retry round (the
                others were made).
        """
        pending, self._pending = dict(self._pending), {}
        if not pending:
            return
        requests: list[tuple[str, str, dict[str, Any]]] = []
        # (repository, instance, changes, stored item, hook, the request)
        finalizers: list[tuple[Repository[Any], Any, dict[str, Any], dict[str, Any] | None, HookPoint, Any]] = []
        hooks = self.client.hooks
        failed: tuple[str, Any] | None = None
        try:
            for slot, (table, kind, item, instance) in pending.items():
                failed = slot
                if instance is not None:
                    model = type(instance)
                    repository = self._repository(model, instance)
                    if kind == "put":
                        if hooks.has("before_save", model):
                            await hooks.run("before_save", instance, repository._context("batch_put"))
                        item, changes, _ = repository._prepare(instance, "batch")
                        finalizers.append((repository, instance, changes, item, "after_save", (table, kind, item)))
                    else:
                        if hooks.has("before_delete", model):
                            await hooks.run("before_delete", instance, repository._context("batch_delete"))
                        finalizers.append((repository, instance, {}, None, "after_delete", (table, kind, item)))
                requests.append((table, kind, item))
        except Exception:
            # Nothing was sent: the other writes stay queued (the exception reports this one).
            self._requeue(pending, without=failed)
            raise
        except BaseException:  # cancelled: nothing was sent
            self._requeue(pending)
            raise
        try:
            output = await self.client.call("batch_write", "BatchWriteItem", requests=requests)
        except BaseException:
            # Batch writes are unconditional: sending them again (the next flush) is safe.
            self._requeue(pending)
            raise
        unprocessed = output["unprocessed"]
        self.written += len(requests) - len(unprocessed)
        left = {(table, kind, stored_identity(item)) for table, kind, item in unprocessed}
        done = [entry for entry in finalizers if (entry[5][0], entry[5][1], stored_identity(entry[5][2])) not in left]
        for repository, instance, changes, item, _, _ in done:
            repository._apply(instance, changes, item)
        errors: list[BaseException] = []
        for repository, instance, _, _, hook, _ in done:
            if hooks.has(hook, type(instance)):  # every instance is final before the first hook runs
                operation = "batch_put" if hook == "after_save" else "batch_delete"
                try:
                    await hooks.run(hook, instance, repository._context(operation))
                except Exception as error:  # noqa: BLE001 - raised once every hook ran
                    errors.append(error)
        if unprocessed:
            raise UnprocessedItemsError(
                f"{len(unprocessed)} writes stayed unprocessed (throttling)",
                unprocessed=[(kind, item) for _, kind, item in unprocessed],
            ) from (errors[0] if errors else None)
        if errors:
            raise errors[0]

    def _requeue(self, pending: dict[tuple[str, Any], Any], without: tuple[str, Any] | None = None) -> None:
        """Puts writes that were not made back in front of the ones queued since."""
        restored = {slot: entry for slot, entry in pending.items() if slot != without}
        restored.update(self._pending)
        self._pending = restored

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is None:
            await self.flush()
