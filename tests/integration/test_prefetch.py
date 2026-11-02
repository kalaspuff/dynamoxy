"""Queries and scans that request their next pages ahead (``prefetch()``).

The pages, cursors and limits are those of a read without it; the requests ahead are cancelled
when the read stops, and their errors are raised when the read reaches their page.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import dynamoxy
from dynamoxy import F, testing
from tests.models import Event


@pytest.fixture
async def events(client: dynamoxy.Client) -> dynamoxy.Repository[Event]:
    await client.create_tables(Event)
    repository = client.model(Event)
    await repository.batch_put([Event(stream="s", seq=seq, kind="even" if seq % 2 == 0 else "odd") for seq in range(9)])
    return repository


async def pages_of(reader: Any) -> list[tuple[list[Any], str | None]]:
    return [([event.seq for event in page.items], page.cursor) async for page in reader.pages()]


@pytest.mark.parametrize("pages", [1, 3])
async def test_reads_ahead_give_the_same_pages(events: dynamoxy.Repository[Event], pages: int) -> None:
    query = events.query(stream="s").page_size(2)
    expected = await pages_of(query)
    assert [seq for seqs, _ in expected for seq in seqs] == list(range(9))
    assert len(expected) >= 5
    assert await pages_of(query.prefetch(pages)) == expected
    assert [event.seq for event in await query.prefetch(pages).all()] == list(range(9))
    # Scans read partitions in no particular order: the same items.
    assert sorted([event.seq async for event in events.scan().page_size(4).prefetch(pages)]) == list(range(9))


async def test_reads_ahead_keep_limits_filters_and_cursors(events: dynamoxy.Repository[Event]) -> None:
    query = events.query(stream="s").page_size(2)
    for reader in (query.limit(5), query.filter(F("kind") == "even").limit(3), query.descending().limit(4)):
        assert await pages_of(reader.prefetch(2)) == await pages_of(reader)
    first = await query.prefetch().first()
    assert first is not None
    assert first.seq == 0
    page = await anext(aiter(query.limit(3).prefetch().pages()))
    rest = [event.seq for event in await query.after(page.cursor).prefetch().all()]
    assert [event.seq for event in page.items] + rest == list(range(9))


async def test_prefetch_needs_a_page() -> None:
    async with testing.memory_client() as client:
        with pytest.raises(ValueError, match="at least 1 page"):
            client.table("t").scan().prefetch(0)


@pytest.fixture
async def memory_events() -> AsyncIterator[dynamoxy.Repository[Event]]:
    async with testing.memory_client() as client:
        await client.create_tables(Event)
        repository = client.model(Event)
        await repository.batch_put([Event(stream="s", seq=seq, kind="k") for seq in range(9)])
        yield repository


async def test_the_next_page_is_requested_while_a_page_is_read(memory_events: dynamoxy.Repository[Event]) -> None:
    client = memory_events.client
    query = memory_events.query(stream="s").page_size(2)
    async with contextlib.aclosing(query.pages()) as pages:
        await anext(pages)
        assert client.metrics().in_flight == 0, "without prefetch(), nothing is requested ahead"
    with dynamoxy.collect() as usage:
        async with contextlib.aclosing(query.prefetch(2).pages()) as pages:
            first = await anext(pages)
            assert [event.seq for event in first.items] == [0, 1]
            testing.inject_fault(client, operation="Query", latency=60.0, times=10)
            await asyncio.sleep(0.05)  # the second page arrived meanwhile, the third waits
            assert client.metrics().in_flight == 1
            second = await anext(pages)
            assert [event.seq for event in second.items] == [2, 3]
        # Closing the read cancelled the request ahead.
        async with asyncio.timeout(5):
            while client.metrics().in_flight:  # noqa: ASYNC110 - polls client metrics (there is no event)
                await asyncio.sleep(0.001)
    assert usage.calls <= 4, "at most two pages ahead of the page being read"


async def test_errors_of_requests_ahead_are_raised_at_their_page(memory_events: dynamoxy.Repository[Event]) -> None:
    client = memory_events.client
    testing.inject_fault(client, operation="Query", latency=0.0)
    testing.inject_fault(client, operation="Query", error="ValidationException", message="refused")
    seen: list[list[int]] = []

    async def read() -> None:
        async for page in memory_events.query(stream="s").page_size(2).prefetch().pages():
            await asyncio.sleep(0.01)  # the request ahead fails meanwhile
            seen.append([event.seq for event in page.items])

    with pytest.raises(dynamoxy.ValidationError, match="refused"):
        await read()
    assert seen == [[0, 1]]
