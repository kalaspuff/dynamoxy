"""Queries and scans: immutable builders that read lazily, page by page.

Every builder method returns a new query, so queries can be shared and extended::

    recent = orders.query(customer_id="c1").descending().limit(20)
    async for order in recent.filter(Order.total > 100):
        ...
    page = await recent.page()  # one request
    next_page = await recent.after(page.cursor).page()

``limit`` caps the number of items returned overall; ``page_size`` sets DynamoDB's ``Limit``
(items *evaluated* per request, before filters). Iteration follows ``LastEvaluatedKey`` until
done. Cursors are opaque URL-safe strings; a page cut short by ``limit`` continues right after
its last item.
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
from dynamoxy.expressions import Condition, ExpressionBuilder, F, Path, and_, as_condition, attribute_roots, path_of
from dynamoxy.results import ConsumedCapacity, Page

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

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
    except RecursionError:  # JSON nested too deeply for the parser: no cursor dynamoxy encoded
        raise ValueError("invalid cursor: it does not hold a key that dynamoxy encoded") from None
    except ValueError as error:
        raise ValueError(f"invalid cursor: {error}") from None
    if not isinstance(wire, dict):
        raise ValueError("invalid cursor: not an object")
    try:
        return _core.from_wire(wire)
    except (ValueError, TypeError, DynamoxyError):
        # Not the detail: a cursor holds key values, which stay out of messages.
        raise ValueError("invalid cursor: it does not hold a key that dynamoxy encoded") from None


#: The key attribute names of what a read targets (the table's key, plus the index key for index
#: reads), or an async function that looks them up (dict-level reads of any table).
type KeyNames = tuple[str, ...] | Callable[[], Awaitable[tuple[str, ...]]]

#: A check of a stored item on the client, for conditions a request cannot express.
type ItemCheck = Callable[[dict[str, Any]], bool]

#: How much larger the next request gets when filters dynamoxy adds itself (or checks on the
#: client) removed items from a page: a few requests then find what a limit asks for.
_GROWTH = 4

#: The largest ``Limit`` a request asks for: more items than a 1 MB page holds, so the page size
#: bounds such requests, and the number still fits the request (an unsigned 32-bit integer), which
#: about 16 growing pages exceeded, and so could a large ``limit()`` or ``page_size()``.
_MAX_REQUEST_LIMIT = 1 << 20


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
        hydrate: Callable[[list[dict[str, Any]], bool], Awaitable[Sequence[dict[str, Any] | None]]] | None = None,
        decode_page: Callable[[list[dict[str, Any]]], list[T]] | None = None,
        key_names: KeyNames | None = None,
        auto_filter: Condition | None = None,
        auto_sort: Condition | None = None,
        explicit_sort: bool = False,
        checks: tuple[ItemCheck, ...] = (),
        where_checks: tuple[ItemCheck, ...] = (),
        projected: frozenset[str] | None = None,
        partial: bool = False,
        select_all: bool = False,
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
        self._key_names = key_names
        # Filters dynamoxy adds (the entity type, guard items of unique values): unlike the
        # caller's filters, they rarely remove items, so a limit still bounds each request.
        self._auto_filter = auto_filter
        # The sort key condition dynamoxy adds (a template's constant prefix); where() replaces it,
        # since DynamoDB takes one sort key condition per query.
        self._auto_sort = auto_sort
        self._explicit_sort = explicit_sort  # the key condition selects a sort key value already
        self._where: Condition | None = None
        self._checks = checks  # checks of every item on the client
        self._where_checks = where_checks  # checks once where() replaced the automatic condition
        # The attributes an index holds (None: every attribute): filters, and projections of a
        # global index, may name no others.
        self._projected = projected
        # A global index that does not hold every attribute of the model: its entries cannot be
        # decoded into complete instances (hydrate() or project() instead).
        self._partial = partial
        # A local index that does not hold every attribute: DynamoDB reads the rest from the table.
        self._select_all = select_all

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

        Raises:
            ValueError: The filter reads attributes that the index being read does not hold
                (``KEYS_ONLY`` / ``INCLUDE`` projections): DynamoDB refuses such a query filter, and
                such a scan filter of a global index never matches. A scan of a local index reads
                the items from the table, and its filter sees them.
        """
        combined = self._filter
        for condition in conditions:
            checked = as_condition(condition)
            assert checked is not None
            if self._operation == "Query" or self._global_index:
                self._check_projected(checked)
            combined = checked if combined is None else combined & checked
        return self._replace(filter=combined)

    def _check_projected(self, condition: Condition) -> None:
        projected = self._projected
        if projected is None:
            return
        roots = attribute_roots(condition)
        missing = sorted(roots - projected) if roots is not None else []
        if missing:
            owner = f"{self._model.__name__} index" if self._model is not None else "index"
            names = ", ".join(repr(name) for name in missing)
            raise ValueError(
                f"{owner} {self._index!r} does not hold {names} (see its projection), and DynamoDB filters the "
                "index entries before anything else: it refuses this filter in a query, and in a scan it would never "
                "match. Query the table, add the attribute to the index with Projection.include(...), or check the "
                "items after .hydrate() in Python"
            )

    def _check_projection(self) -> None:
        projected = self._projected
        if projected is None or not self._global_index:
            return  # a local index fetches what it does not hold from the table
        roots = {str(path.elements[0]) for path in self._projection}
        missing = sorted(roots - projected)
        if missing:
            owner = f"{self._model.__name__} index" if self._model is not None else "index"
            names = ", ".join(repr(name) for name in missing)
            raise ValueError(
                f"{owner} {self._index!r} does not hold {names} (see its projection), and DynamoDB refuses a "
                "projection of a global index that names them: add .hydrate() to read them from the full items, "
                "project what the index holds, or add the attribute to the index with Projection.include(...)"
            )

    def _check_filter_of_held_projection(self) -> None:
        """Refuses a scan filter that would never match: a projection of what the index holds.

        Such a scan of a local index reads no items from the table, so its filter sees the
        entries, where other attributes are missing.
        """
        projected = self._projected
        if projected is None or self._filter is None:
            return
        if {str(path.elements[0]) for path in self._projection} <= projected:
            self._check_projected(self._filter)

    def limit(self, count: int) -> Self:
        """Returns at most ``count`` items in total (iteration, ``all()``, ``count()``, ``page()``)."""
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
        # A hydrate() before or after projects the full items.
        reader = self._replace(projection=projected, decode_page=None)
        requested = frozenset(str(path.elements[0]) for path in projected)
        reader._decode = _projection_decoder(self._model, requested)  # type: ignore[assignment]
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

    def _params(
        self,
        builder: ExpressionBuilder,
        start: dict[str, Any] | None,
        limit: int | None,
        key_names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"table_name": self._table, "consistent_read": self._consistent}
        if self._index is not None:
            params["index_name"] = self._index
        key = self._key_condition
        sort = self._where if self._where is not None else self._auto_sort
        if sort is not None:
            key = sort if key is None else key & sort
        if key is not None:
            params["key_condition_expression"] = builder.condition(key)
        filters = [condition for condition in (self._auto_filter, self._filter) if condition is not None]
        if filters:
            params["filter_expression"] = builder.condition(filters[0] if len(filters) == 1 else and_(*filters))
        if self._projection and self._hydrate and key_names:
            # The attributes come from the full items: the index entries need their keys only
            # (a global index may hold none of the attributes, and DynamoDB refuses naming them).
            params["projection_expression"] = builder.projection(tuple(F(name) for name in key_names))
        elif self._projection:
            paths = self._projection
            if key_names:
                # Key attributes too, for cursors, client checks and hydration (see _fetch_page).
                roots = {path.elements[0] for path in paths}
                paths += tuple(F(name) for name in key_names if name not in roots)
            params["projection_expression"] = builder.projection(paths)
        elif self._select_all and not self._hydrate:
            params["select"] = "ALL_ATTRIBUTES"
        if limit is not None:
            # Also for a page_size() or limit() above it: no page holds that many items anyway.
            params["limit"] = min(limit, _MAX_REQUEST_LIMIT)
        if start is not None:
            params["exclusive_start_key"] = start
        return params

    def _request_limit(self, remaining: int | None, growth: int = 1) -> int | None:
        """DynamoDB's ``Limit`` for a request that should return ``remaining`` more items."""
        if remaining is None or self._filter is not None:
            # The caller's filters may remove most items: read DynamoDB's pages and stop at the limit.
            return self._page_size
        wanted = min(remaining * growth, _MAX_REQUEST_LIMIT)
        return wanted if self._page_size is None else min(self._page_size, wanted)

    async def _resolve_key_names(self) -> tuple[str, ...] | None:
        names = self._key_names
        if names is None or isinstance(names, tuple):
            return names
        resolved = await names()
        self._key_names = resolved
        return resolved

    async def _fetch(
        self, start: dict[str, Any] | None, limit: int | None, *, segment: tuple[int, int] | None = None
    ) -> Page[T]:
        page, _ = await self._fetch_page(start, limit, segment=segment)
        return page

    async def _fetch_page(
        self, start: dict[str, Any] | None, limit: int | None, *, segment: tuple[int, int] | None = None
    ) -> tuple[Page[T], list[dict[str, Any]]]:
        """One request: the page, and the entries read for its items (index entries for index reads)."""
        if self._projection and not self._hydrate:
            self._check_projection()
            if self._filter is not None and not self._global_index and self._operation == "Scan":
                self._check_filter_of_held_projection()
        if self._partial and not self._hydrate and not self._projection:
            owner = self._model.__name__ if self._model is not None else "the models"
            raise ValueError(
                f"index {self._index!r} does not hold every attribute of {owner} (see its projection), so its entries "
                "are not complete items: add .hydrate() to read the full items, or .project(...) to get dicts of the "
                "attributes it holds"
            )
        builder = ExpressionBuilder()
        checks = self._checks + self._where_checks if self._where is not None else self._checks
        # A projection reads the key attributes too when a limit may cut the page (its cursor),
        # when items are checked on the client (the sort key prefix that where() replaced) and
        # for hydration (the table key); the decoder leaves out what was not asked for.
        names = (
            await self._resolve_key_names()
            if self._projection and (self._limit is not None or checks or self._hydrate)
            else None
        )
        params = self._params(builder, start, limit, names)
        segment = segment or self._segment
        if segment is not None:
            params["segment"], params["total_segments"] = segment
        params.update(builder.arguments())
        output = await self._client.call(self._method, self._operation, table=self._table, model=self._model, **params)
        entries: list[dict[str, Any]] = output["items"]
        items = entries
        if self._hydrate and self._hydrator is not None and entries:
            full = await self._hydrator(entries, self._consistent)
            pairs = [(entry, item) for entry, item in zip(entries, full, strict=True) if item is not None]
            if checks:
                pairs = [(entry, item) for entry, item in pairs if all(check(item) for check in checks)]
            entries = [entry for entry, _ in pairs]
            items = [item for _, item in pairs]
        elif checks:
            entries = items = [item for item in entries if all(check(item) for check in checks)]
        capacity = output["consumed_capacity"]
        last = output["last_evaluated_key"]
        decode_page = self._decode_page
        page = Page(
            items=decode_page(items) if decode_page is not None else [self._decode(item) for item in items],
            cursor=encode_cursor(last),
            count=output["count"],
            scanned_count=output["scanned_count"],
            consumed_capacity=ConsumedCapacity.from_core(capacity) if capacity else None,
            last_evaluated_key=last,
        )
        return page, entries

    async def _truncate(self, page: Page[T], entries: list[dict[str, Any]], count: int) -> Page[T]:
        """The first ``count`` items of ``page``, with a cursor right after the last of them."""
        names = await self._resolve_key_names()
        if not names and page.last_evaluated_key is not None:
            names = tuple(page.last_evaluated_key)
        last = entries[count - 1]
        key = {name: last[name] for name in names or () if name in last}
        if not names or len(key) != len(names):
            raise TypeError(
                "cannot tell where a page cut short by limit() ends: the key attributes of the items are unknown"
            )
        return Page(
            items=page.items[:count],
            cursor=encode_cursor(key),
            count=page.count,
            scanned_count=page.scanned_count,
            consumed_capacity=page.consumed_capacity,
            last_evaluated_key=key,
        )

    async def page(self) -> Page[T]:
        """One request: up to ``page_size`` (or ``limit``) evaluated items.

        A page can be empty while ``cursor`` is set (a filter removed every item read).
        """
        if self._segments is not None:
            raise TypeError("page() reads one segment: use .segment(index, total) instead of .segments(n)")
        limit = self._limit
        request = self._page_size if limit is None else min(self._page_size or limit, limit)
        return await self._fetch(self._start, request)

    async def pages(self) -> AsyncGenerator[Page[T]]:
        """Every page, in order (stops early when ``limit`` items were returned).

        The last page is cut at the limit; its cursor continues right after its last item.
        """
        if self._segments is not None:
            raise TypeError("pages() reads one segment: use .segment(index, total) instead of .segments(n)")
        remaining = self._limit
        start = self._start
        growth = 1
        while True:
            request = self._request_limit(remaining, growth)
            page, entries = await self._fetch_page(start, request)
            if remaining is not None and len(page.items) >= remaining:
                yield await self._truncate(page, entries, remaining) if len(page.items) > remaining else page
                return
            yield page
            if remaining is not None:
                remaining -= len(page.items)
            if page.last_evaluated_key is None:
                return
            if len(page.items) < page.scanned_count:
                growth = min(growth * _GROWTH, _MAX_REQUEST_LIMIT)
            start = page.last_evaluated_key

    async def __aiter__(self) -> AsyncGenerator[T]:
        if self._segments is not None:
            async with contextlib.aclosing(self._parallel()) as parallel:
                async for item in parallel:
                    yield item
            return
        async with contextlib.aclosing(self.pages()) as pages:
            async for page in pages:
                for item in page.items:
                    yield item

    async def _parallel(self) -> AsyncGenerator[T]:
        total = self._segments
        assert total is not None
        if self._start is not None:
            raise TypeError(
                "a parallel scan reads every segment from its start: continue one segment with "
                ".segment(index, total).after(cursor) instead of .segments(n)"
            )
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=max(total * 4, 16))

        async def worker(segment: int) -> None:
            start: dict[str, Any] | None = None
            try:
                while True:
                    page = await self._fetch(start, self._page_size, segment=(segment, total))
                    for item in page.items:
                        await queue.put(("item", item))
                    if page.last_evaluated_key is None:
                        break
                    start = page.last_evaluated_key
            except asyncio.CancelledError:
                raise  # the consumer stopped: nobody reads the queue any more
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
        reader = self.limit(1)
        if self._segments is not None:
            async with contextlib.aclosing(reader.__aiter__()) as items:
                async for item in items:
                    return item
            return None
        async with contextlib.aclosing(reader.pages()) as pages:
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
        """The number of matching items (``Select=COUNT``; reads every page, returns no items).

        DynamoDB counts what matches the key condition and the filters; checks dynamoxy makes on
        the client (see ``where()``) do not apply.
        """
        if self._projection:
            # A count returns no attributes: a projection's names would be unused in the request.
            return await self._replace(projection=()).count()
        if self._segments is not None:
            if self._start is not None:
                raise TypeError(
                    "a parallel scan counts every segment from its start: count one segment with "
                    ".segment(index, total).after(cursor) instead of .segments(n)"
                )
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
        """Whether at least one item matches (model reads read the keys only: nothing is decoded)."""
        names = self._key_names
        if isinstance(names, tuple) and names:
            # The keys of the entries: no hydration (project() keeps a hydrate() before it).
            return await self.limit(1)._replace(hydrate=False)._with_projection(*names).first() is not None
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
        """Adds the sort key condition to the key condition.

        Allowed: ``=``, ``<``, ``<=``, ``>``, ``>=``, ``between`` or ``begins_with`` on the sort key,
        one per query (DynamoDB takes one sort key condition). It replaces the ``begins_with`` that
        model queries add for templated sort keys with a constant prefix
        (``orders.query(customer_id="c1").where(Order.sk.begins_with("ORDER#2026"))``).

        Raises:
            ValueError: The query has a sort key condition already (a ``where()`` before, or a sort
                key value given to ``query()``), or several conditions are given: combine bounds
                with ``between()``.
        """
        checked = [as_condition(condition) for condition in conditions]
        if not checked:
            return self
        if len(checked) > 1 or self._where is not None or self._explicit_sort:
            raise ValueError(
                "a query takes one sort key condition (DynamoDB allows one): combine bounds with between(), "
                "and give either a sort key value to query() or a where() condition"
            )
        return self._replace(where=checked[0])

    def project(self, *paths: Path[Any] | str) -> Query[dict[str, Any]]:
        """Returns only these attributes, as dicts (by field name for model queries)."""
        return self._with_projection(*paths)  # type: ignore[return-value]

    def _params(
        self,
        builder: ExpressionBuilder,
        start: dict[str, Any] | None,
        limit: int | None,
        key_names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        params = super()._params(builder, start, limit, key_names)
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


def _projection_decoder(model: type | None, requested: frozenset[str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Decodes projected items, leaving out the key attributes dynamoxy added for cursors."""
    if model is None:
        return lambda item: {attribute: value for attribute, value in item.items() if attribute in requested}

    from dynamoxy.schema import schema_of

    schema = schema_of(model)

    def decode(item: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attribute, raw in item.items():
            if attribute not in requested:
                continue
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
