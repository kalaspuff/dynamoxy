"""Queries and scans: immutable builders that read lazily, page by page.

Every builder method returns a new query, so queries can be shared and extended::

    recent = orders.query(customer_id="c1").descending().limit(20)
    async for order in recent.filter(Order.total > 100):
        ...
    page = await recent.page()  # one request
    next_page = await recent.after(page.cursor).page()

``limit`` caps the number of items returned overall; ``page_size`` sets DynamoDB's ``Limit``
(items *evaluated* per request, before filters). Iteration follows ``LastEvaluatedKey`` until
done. Cursors are opaque URL-safe strings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
from typing import TYPE_CHECKING, Any, ClassVar, Self

from dynamoxy import _core
from dynamoxy.errors import DynamoxyError
from dynamoxy.expressions import Condition, ExpressionBuilder, Path, as_condition, path_of
from dynamoxy.results import ConsumedCapacity, Page

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from dynamoxy.client import Client

__all__ = ["Query", "Scan", "decode_cursor", "encode_cursor"]


def encode_cursor(key: dict[str, Any] | None) -> str | None:
    """An opaque, URL-safe cursor for a ``LastEvaluatedKey`` (``None`` stays ``None``).

    Cursors are not signed: do not use them for authorization decisions.
    """
    if key is None:
        return None
    wire = _core.to_wire(key, base64_binary=True)
    text = json.dumps(wire, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """The ``ExclusiveStartKey`` encoded by :func:`encode_cursor`.

    Raises:
        ValueError: The cursor is not one dynamoxy produced.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        wire = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except ValueError as error:
        raise ValueError(f"invalid cursor: {error}") from None
    if not isinstance(wire, dict):
        raise ValueError("invalid cursor: not an object")
    try:
        return _core.from_wire(wire)
    except (ValueError, TypeError, DynamoxyError) as error:
        raise ValueError(f"invalid cursor: {error}") from None


class _Reader[T]:
    """Shared behaviour of :class:`Query` and :class:`Scan`."""

    _operation: ClassVar[str] = ""
    _method: ClassVar[str] = ""

    def __init__(
        self,
        client: Client,
        table: str,
        *,
        decode: Callable[[dict[str, Any]], T],
        model: type | None = None,
        index: str | None = None,
        global_index: bool = False,
        key_condition: Condition | None = None,
        consistent: bool = False,
        hydrate: Callable[[list[dict[str, Any]], bool], Awaitable[list[dict[str, Any]]]] | None = None,
        decode_page: Callable[[list[dict[str, Any]]], list[T]] | None = None,
    ) -> None:
        self._client = client
        self._table = table
        self._decode = decode
        self._decode_page = decode_page
        self._model = model
        self._index = index
        self._global_index = global_index
        self._key_condition = key_condition
        self._filter: Condition | None = None
        self._projection: tuple[Path[Any], ...] = ()
        self._limit: int | None = None
        self._page_size: int | None = None
        self._forward = True
        self._check_consistent(consistent)
        self._consistent = consistent
        self._start: dict[str, Any] | None = None
        self._segment: tuple[int, int] | None = None
        self._segments: int | None = None
        self._hydrator = hydrate
        self._hydrate = False

    def _replace(self, **changes: Any) -> Self:
        clone = copy.copy(self)
        for name, value in changes.items():
            setattr(clone, f"_{name}", value)
        return clone

    def __repr__(self) -> str:
        target = f"{self._table}.{self._index}" if self._index else self._table
        return f"<dynamoxy.{type(self).__name__} {target}>"

    # ---- builders -------------------------------------------------------------------------

    def filter(self, *conditions: Condition | bool) -> Self:
        """Adds a filter (AND-ed with earlier ones).

        Filters run after items are read: they reduce what is returned, not what is consumed.
        """
        combined = self._filter
        for condition in conditions:
            checked = as_condition(condition)
            assert checked is not None
            combined = checked if combined is None else combined & checked
        return self._replace(filter=combined)

    def limit(self, count: int) -> Self:
        """Returns at most ``count`` items in total (iteration, ``all()``, ``count()``)."""
        if count < 1:
            raise ValueError(f"limit must be at least 1, got {count}")
        return self._replace(limit=count)

    def page_size(self, count: int) -> Self:
        """Evaluates at most ``count`` items per request (DynamoDB's ``Limit``)."""
        if count < 1:
            raise ValueError(f"page_size must be at least 1, got {count}")
        return self._replace(page_size=count)

    def consistent(self, consistent: bool = True) -> Self:
        """Strongly consistent reads (twice the read cost), like ``consistent=True``.

        Raises:
            ValueError: The query or scan reads a global secondary index (eventually consistent
                only).
        """
        self._check_consistent(consistent)
        return self._replace(consistent=consistent)

    def _check_consistent(self, consistent: bool) -> None:
        if consistent and self._global_index:
            owner = f"{self._model.__name__} index" if self._model is not None else "index"
            raise ValueError(
                f"{owner} {self._index!r} is a global secondary index, which does not support consistent reads: "
                "remove consistent=True, or read the table or a local index"
            )

    def after(self, cursor: str | dict[str, Any] | None) -> Self:
        """Continues after a cursor (``page.cursor``) or a raw ``LastEvaluatedKey``."""
        start = decode_cursor(cursor) if isinstance(cursor, str) else cursor
        return self._replace(start=start)

    def _with_projection(self, *paths: Path[Any] | str) -> _Reader[dict[str, Any]]:
        if not paths:
            raise ValueError("project() needs at least one attribute")
        projected = tuple(path_of(path) for path in paths)
        reader = self._replace(projection=projected, hydrate=False, decode_page=None)
        reader._decode = _projection_decoder(self._model)  # type: ignore[assignment]
        return reader  # type: ignore[return-value]

    def hydrate(self) -> Self:
        """Fetches full items for index entries that hold only some attributes.

        For ``KEYS_ONLY`` / ``INCLUDE`` projections; uses ``BatchGetItem``, strongly consistent
        when the query is. Model queries only.
        """
        if self._hydrator is None:
            raise TypeError("hydrate() needs a model query (repository.query / repository.scan)")
        return self._replace(hydrate=True)

    # ---- execution ------------------------------------------------------------------------

    def _params(self, builder: ExpressionBuilder, start: dict[str, Any] | None, limit: int | None) -> dict[str, Any]:
        params: dict[str, Any] = {"table_name": self._table, "consistent_read": self._consistent}
        if self._index is not None:
            params["index_name"] = self._index
        if self._key_condition is not None:
            params["key_condition_expression"] = builder.condition(self._key_condition)
        if self._filter is not None:
            params["filter_expression"] = builder.condition(self._filter)
        if self._projection:
            params["projection_expression"] = builder.projection(self._projection)
        if limit is not None:
            params["limit"] = limit
        if start is not None:
            params["exclusive_start_key"] = start
        return params

    def _request_limit(self, remaining: int | None) -> int | None:
        if remaining is not None and self._filter is None:
            return min(self._page_size or remaining, remaining)
        return self._page_size

    async def _fetch(
        self, start: dict[str, Any] | None, limit: int | None, *, segment: tuple[int, int] | None = None
    ) -> Page[T]:
        builder = ExpressionBuilder()
        params = self._params(builder, start, limit)
        segment = segment or self._segment
        if segment is not None:
            params["segment"], params["total_segments"] = segment
        params.update(builder.arguments())
        output = await self._client.call(self._method, self._operation, table=self._table, model=self._model, **params)
        raw_items = output["items"]
        if self._hydrate and self._hydrator is not None and raw_items:
            raw_items = await self._hydrator(raw_items, self._consistent)
        capacity = output["consumed_capacity"]
        last = output["last_evaluated_key"]
        decode_page = self._decode_page
        return Page(
            items=decode_page(raw_items) if decode_page is not None else [self._decode(item) for item in raw_items],
            cursor=encode_cursor(last),
            count=output["count"],
            scanned_count=output["scanned_count"],
            consumed_capacity=ConsumedCapacity.from_core(capacity) if capacity else None,
            last_evaluated_key=last,
        )

    async def page(self) -> Page[T]:
        """One request: up to ``page_size`` (or ``limit``) evaluated items.

        A page can be empty while ``cursor`` is set (a filter removed every item read).
        """
        if self._segments is not None:
            raise TypeError("page() reads one segment: use .segment(index, total) instead of .segments(n)")
        return await self._fetch(self._start, self._request_limit(self._limit))

    async def pages(self) -> AsyncGenerator[Page[T]]:
        """Every page, in order (stops early when ``limit`` items were returned)."""
        if self._segments is not None:
            raise TypeError("pages() reads one segment: use .segment(index, total) instead of .segments(n)")
        remaining = self._limit
        start = self._start
        while True:
            page = await self._fetch(start, self._request_limit(remaining))
            if remaining is not None and len(page.items) >= remaining:
                yield Page(
                    items=page.items[:remaining],
                    cursor=page.cursor,
                    count=page.count,
                    scanned_count=page.scanned_count,
                    consumed_capacity=page.consumed_capacity,
                    last_evaluated_key=page.last_evaluated_key,
                )
                return
            yield page
            if remaining is not None:
                remaining -= len(page.items)
            if page.last_evaluated_key is None:
                return
            start = page.last_evaluated_key

    async def __aiter__(self) -> AsyncIterator[T]:
        if self._segments is not None:
            async for item in self._parallel():
                yield item
            return
        async for page in self.pages():
            for item in page.items:
                yield item

    async def _parallel(self) -> AsyncIterator[T]:
        total = self._segments
        assert total is not None
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=max(total * 4, 16))

        async def worker(segment: int) -> None:
            start: dict[str, Any] | None = self._start
            try:
                while True:
                    page = await self._fetch(start, self._page_size, segment=(segment, total))
                    for item in page.items:
                        await queue.put(("item", item))
                    if page.last_evaluated_key is None:
                        break
                    start = page.last_evaluated_key
            except BaseException as error:  # noqa: BLE001 - forwarded to the consumer
                await queue.put(("error", error))
                return
            await queue.put(("done", None))

        tasks = [asyncio.get_running_loop().create_task(worker(segment)) for segment in range(total)]
        remaining = self._limit
        finished = 0
        try:
            while finished < total:
                kind, value = await queue.get()
                if kind == "done":
                    finished += 1
                elif kind == "error":
                    raise value
                else:
                    yield value
                    if remaining is not None:
                        remaining -= 1
                        if remaining == 0:
                            return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def first(self) -> T | None:
        """The first item, or ``None``."""
        if self._segments is not None:
            async for item in self.limit(1):
                return item
            return None
        async with contextlib.aclosing(self.limit(1).pages()) as pages:
            async for page in pages:
                if page.items:
                    return page.items[0]
        return None

    async def one(self) -> T:
        """The only item.

        Raises:
            ItemNotFoundError: Nothing matched.
            ValueError: More than one item matched.
        """
        items = [item async for item in self.limit(2)]
        if not items:
            from dynamoxy.errors import ItemNotFoundError

            raise ItemNotFoundError(f"no item matched {self!r}", model=self._model)
        if len(items) > 1:
            raise ValueError(f"more than one item matched {self!r}")
        return items[0]

    async def all(self) -> list[T]:
        """Every item (follows every page; mind the size of what you ask for)."""
        if self._segments is not None:
            return [item async for item in self]
        items: list[T] = []
        async for page in self.pages():
            items.extend(page.items)
        return items

    async def count(self) -> int:
        """The number of matching items (``Select=COUNT``; reads every page, returns no items)."""
        if self._segments is not None:
            totals = await asyncio.gather(
                *(
                    self._replace(segments=None, segment=(segment, self._segments)).count()
                    for segment in range(self._segments)
                )
            )
            total = sum(totals)
            return min(total, self._limit) if self._limit is not None else total
        total = 0
        start = self._start
        while True:
            builder = ExpressionBuilder()
            params = self._params(builder, start, self._page_size)
            params.pop("projection_expression", None)
            if self._segment is not None:
                params["segment"], params["total_segments"] = self._segment
            params["select"] = "COUNT"
            params.update(builder.arguments())
            output = await self._client.call(
                self._method, self._operation, table=self._table, model=self._model, **params
            )
            total += output["count"]
            if self._limit is not None and total >= self._limit:
                return self._limit
            start = output["last_evaluated_key"]
            if start is None:
                return total

    async def exists(self) -> bool:
        """Whether at least one item matches."""
        return await self.first() is not None


class Query[T](_Reader[T]):
    """A query of one partition (of the table or an index)."""

    _operation = "Query"
    _method = "query"

    def descending(self) -> Self:
        """Highest sort keys first (``ScanIndexForward=false``)."""
        return self._replace(forward=False)

    def ascending(self) -> Self:
        """Lowest sort keys first (the default)."""
        return self._replace(forward=True)

    def reverse(self) -> Self:
        """Reverses the sort order."""
        return self._replace(forward=not self._forward)

    def where(self, *conditions: Condition | bool) -> Self:
        """Adds sort key conditions to the key condition.

        Allowed: ``=``, ``<``, ``<=``, ``>``, ``>=``, ``between`` or ``begins_with`` on the sort key.
        """
        combined = self._key_condition
        for condition in conditions:
            checked = as_condition(condition)
            assert checked is not None
            combined = checked if combined is None else combined & checked
        return self._replace(key_condition=combined)

    def project(self, *paths: Path[Any] | str) -> Query[dict[str, Any]]:
        """Returns only these attributes, as dicts (by field name for model queries)."""
        return self._with_projection(*paths)  # type: ignore[return-value]

    def _params(self, builder: ExpressionBuilder, start: dict[str, Any] | None, limit: int | None) -> dict[str, Any]:
        params = super()._params(builder, start, limit)
        params["scan_index_forward"] = self._forward
        return params


class Scan[T](_Reader[T]):
    """A scan of a whole table or index."""

    _operation = "Scan"
    _method = "scan"

    def segments(self, total: int) -> Self:
        """Reads ``total`` segments in parallel (iteration, ``all()`` and ``count()``)."""
        if not 1 <= total <= 1_000_000:
            raise ValueError(f"segments must be between 1 and 1000000, got {total}")
        return self._replace(segments=total, segment=None)

    def segment(self, index: int, total: int) -> Self:
        """Reads one segment of a parallel scan (to spread a scan over workers)."""
        if not 0 <= index < total:
            raise ValueError(f"segment index must be in [0, {total}), got {index}")
        return self._replace(segment=(index, total), segments=None)

    def project(self, *paths: Path[Any] | str) -> Scan[dict[str, Any]]:
        """Returns only these attributes, as dicts (by field name for model scans)."""
        return self._with_projection(*paths)  # type: ignore[return-value]


def _projection_decoder(model: type | None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if model is None:
        return dict

    from dynamoxy.schema import schema_of

    schema = schema_of(model)

    def decode(item: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attribute, raw in item.items():
            spec = schema.attributes.get(attribute)
            if spec is None:
                result[attribute] = raw
                continue
            try:
                result[spec.name] = spec.codec.decode(raw)
            except Exception:  # noqa: BLE001 - partial documents keep their raw form
                result[spec.name] = raw
        return result

    return decode
