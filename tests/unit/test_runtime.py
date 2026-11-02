"""The asyncio bridge (``crates/dynamoxy-py/src/runtime.rs``): how native calls resolve on event loops.

In-memory calls finish when they are made and yield to the event loop once; calls that wait
(here: injected latency) finish on the runtime's threads and are delivered through the loop's
port (a socket the loop watches, or ``call_soon_threadsafe`` for loops without ``add_reader``).
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import types
import weakref
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest

import dynamoxy
from dynamoxy import testing


def uvloop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    uvloop = pytest.importorskip("uvloop")
    return uvloop.new_event_loop


@pytest.fixture(params=["asyncio", "uvloop"])
def new_loop(request: pytest.FixtureRequest) -> Callable[[], asyncio.AbstractEventLoop]:
    """Creates event loops of each kind the bridge is tested on."""
    return asyncio.new_event_loop if request.param == "asyncio" else uvloop_factory()


async def items_table(client: dynamoxy.Client) -> dynamoxy.Table:
    table = client.table("items")
    await table.create(partition_key=("pk", "S"))
    await table.put({"pk": "a", "n": 1})
    return table


async def wait_until(condition: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 - polls client metrics (there is no event)
            await asyncio.sleep(0.001)


async def test_async_tests_run_on_the_selected_event_loop() -> None:
    loop = asyncio.get_running_loop()
    if os.environ.get("DYNAMOXY_TEST_EVENT_LOOP") == "uvloop":
        import uvloop

        assert isinstance(loop, uvloop.Loop)
    else:
        assert type(loop).__module__.startswith("asyncio")


async def test_in_memory_calls_yield_to_the_event_loop() -> None:
    async with testing.memory_client() as client:
        table = await items_table(client)
        ran: list[str] = []

        async def other() -> None:
            ran.append("other")

        task = asyncio.get_running_loop().create_task(other())
        await table.get({"pk": "a"})
        assert ran == ["other"], "like real I/O, a call lets other tasks run"
        await task


async def test_in_memory_calls_work_in_generator_coroutines() -> None:
    async with testing.memory_client() as client:
        await items_table(client)
        core = await client._core()

        @types.coroutine
        def legacy() -> Any:
            output, _ = yield from core.get_item("items", {"pk": "a"}).__await__()
            return output["item"]

        assert await legacy() == {"pk": "a", "n": 1}


async def test_calls_take_any_mapping_and_refuse_text_as_parameters() -> None:
    async with testing.memory_client() as client:
        table = await items_table(client)
        core = await client._core()
        await core.put_item(
            "items",
            {"pk": "b", "n": 2},
            condition_expression="attribute_not_exists(#p) AND :v > :w",
            expression_attribute_names=MappingProxyType({"#p": "pk"}),
            expression_attribute_values=MappingProxyType({":v": 2, ":w": 1}),
        )
        output, _ = await core.batch_get_item(MappingProxyType({"items": {"keys": [{"pk": "b"}]}}))
        assert output["responses"]["items"] == [{"pk": "b", "n": 2}]
        await core.batch_write_item(MappingProxyType({"items": [("delete", {"pk": "b"})]}))
        arn = (await table.describe()).arn
        assert arn is not None
        await core.tag_resource(arn, MappingProxyType({"team": "a"}))
        assert await table.tags() == {"team": "a"}
        with pytest.raises(TypeError, match="parameters must be a list of values, got str"):
            core.execute_statement('SELECT * FROM "items" WHERE pk = ?', parameters="a")  # type: ignore[arg-type]


async def test_waiting_calls_resolve_on_their_loop() -> None:
    async with testing.memory_client() as client:
        table = await items_table(client)
        testing.inject_fault(client, operation="GetItem", latency=0.01, times=3)
        items = await asyncio.gather(*(table.get({"pk": "a"}) for _ in range(3)))
        assert [item["n"] for item in items if item is not None] == [1, 1, 1]
        testing.inject_fault(client, operation="GetItem", latency=0.01)
        with pytest.raises(dynamoxy.ResourceNotFoundError):
            await client.table("missing").get({"pk": "a"})


async def test_cancelling_a_call_drops_its_work() -> None:
    async with testing.memory_client() as client:
        table = await items_table(client)
        testing.inject_fault(client, operation="GetItem", latency=60.0)
        task = asyncio.get_running_loop().create_task(table.get({"pk": "a"}))
        await wait_until(lambda: client.metrics().in_flight == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: client.metrics().in_flight == 0)
        assert await table.get({"pk": "a"}) == {"pk": "a", "n": 1}


async def large_page(client: dynamoxy.Client) -> Callable[[], Any]:
    """A table with one partition of 20,000 items (a page of about 600 KB), and a call that reads it."""
    table = client.table("large")
    await table.create(partition_key=("pk", "S"), sort_key=("n", "N"))
    await table.batch_write(puts=[{"pk": "p", "n": n, "text": f"item {n}", "flag": n % 2 == 0} for n in range(20_000)])
    core = await client._core()

    def read() -> Any:
        return core.query(
            table.physical_name, key_condition_expression="pk = :p", expression_attribute_values={":p": "p"}
        )

    return read


async def ticks_while(awaitable: Any) -> tuple[Any, int]:
    """The result of ``awaitable``, and how often another task ran while it was awaited."""
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.get_running_loop().create_task(heartbeat())
    await asyncio.sleep(0)
    before = ticks
    try:
        result = await awaitable
    finally:
        running = False
    during = ticks - before
    await task
    return result, during


@pytest.mark.parametrize("path", ["inline", "port"])
async def test_large_results_are_converted_in_steps_between_which_other_tasks_run(path: str) -> None:
    async with testing.memory_client() as client:
        read = await large_page(client)
        if path == "port":
            testing.inject_fault(client, operation="Query", latency=0.001)
        (output, _), ticks = await ticks_while(read())
        assert [item["n"] for item in output["items"]] == list(range(20_000))
        assert output["items"][3] == {"pk": "p", "n": 3, "text": "item 3", "flag": False}
        assert output["last_evaluated_key"] is None
        # Converting 20,000 items takes several steps of about a millisecond each.
        assert ticks >= 3, f"other tasks ran {ticks} time(s) while the page was converted"


async def test_a_call_cancelled_while_its_result_is_converted_drops_it() -> None:
    async with testing.memory_client() as client:
        read = await large_page(client)
        testing.inject_fault(client, operation="Query", latency=0.001)

        async def query() -> Any:
            return await read()

        call = asyncio.get_running_loop().create_task(query())
        await wait_until(lambda: client.metrics().in_flight == 1)
        await wait_until(lambda: client.metrics().in_flight == 0)  # finished: converting
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        (output, _), _ = await ticks_while(read())
        assert len(output["items"]) == 20_000


class ContinuationsLoop(asyncio.SelectorEventLoop):
    """Counts the drains of the port scheduled with ``call_soon`` to go on converting results."""

    def __init__(self) -> None:
        super().__init__()
        self.pending = self.most = 0

    def call_soon(self, callback: Callable[..., object], *args: Any, context: Any = None) -> asyncio.Handle:
        if getattr(callback, "__name__", "") not in ("drain", "resume"):
            return super().call_soon(callback, *args, context=context)
        self.pending += 1
        self.most = max(self.most, self.pending)

        def run(*arguments: Any) -> None:
            self.pending -= 1
            callback(*arguments)

        return super().call_soon(run, *args, context=context)


def test_results_arriving_while_others_convert_add_no_second_chain_of_drains() -> None:
    async def read_while_others_arrive() -> list[Any]:
        async with testing.memory_client() as client:
            read = await large_page(client)
            core = await client._core()
            testing.inject_fault(client, operation="Query", latency=0.001)
            page = read()
            # Small results that arrive (and wake the loop) while the page is converted.
            gets = []
            for delay in range(2, 40, 2):
                testing.inject_fault(client, operation="GetItem", latency=delay / 1000)
                gets.append(core.get_item(client.table("large").physical_name, key={"pk": "p", "n": 1}))
            return await asyncio.gather(page, *gets)

    loop = ContinuationsLoop()
    try:
        (page, _), *gets = loop.run_until_complete(read_while_others_arrive())
    finally:
        loop.close()
    assert len(page["items"]) == 20_000
    assert all(output["item"] == {"pk": "p", "n": 1, "text": "item 1", "flag": False} for output, _ in gets)
    assert loop.most == 1, f"{loop.most} chains of drains converted results at once"


async def scenario() -> list[Any]:
    """Inline and waiting calls, and a cancelled one, on the running loop."""
    async with testing.memory_client() as client:
        table = await items_table(client)
        testing.inject_fault(client, operation="GetItem", latency=0.005, times=2)
        waited = await asyncio.gather(table.get({"pk": "a"}), table.get({"pk": "a"}))
        testing.inject_fault(client, operation="PutItem", latency=60.0)
        task = asyncio.get_running_loop().create_task(table.put({"pk": "b"}))
        await wait_until(lambda: client.metrics().in_flight == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: client.metrics().in_flight == 0)
        return [*waited, await table.get({"pk": "a"}), await table.get({"pk": "b"})]


EXPECTED = [{"pk": "a", "n": 1}, {"pk": "a", "n": 1}, {"pk": "a", "n": 1}, None]


def test_loops_without_add_reader_are_woken_threadsafe() -> None:
    class NoReaders(asyncio.SelectorEventLoop):  # like the Windows proactor loop
        def add_reader(self, *args: Any, **kwargs: Any) -> None:
            raise NotImplementedError

    loop = NoReaders()
    try:
        assert loop.run_until_complete(scenario()) == EXPECTED
    finally:
        loop.close()


def test_each_thread_and_loop_gets_its_own_results(new_loop: Callable[[], asyncio.AbstractEventLoop]) -> None:
    results: dict[int, Any] = {}

    def run(number: int) -> None:
        results[number] = asyncio.run(scenario(), loop_factory=new_loop)

    threads = [threading.Thread(target=run, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert results == dict.fromkeys(range(4), EXPECTED)


def test_a_closed_loop_does_not_keep_its_pending_calls(new_loop: Callable[[], asyncio.AbstractEventLoop]) -> None:
    async def start() -> asyncio.Task[Any]:
        client = testing.memory_client()
        table = await items_table(client)
        testing.inject_fault(client, operation="GetItem", latency=60.0)
        task = asyncio.get_running_loop().create_task(table.get({"pk": "a"}))
        await wait_until(lambda: client.metrics().in_flight == 1)
        return task

    loop = new_loop()
    loop.set_exception_handler(lambda _loop, _context: None)  # "Task was destroyed but it is pending!"
    task = loop.run_until_complete(start())
    loop.close()  # the call is still pending: nobody cancelled its task
    closed = weakref.ref(loop)
    del loop, task
    assert (
        asyncio.run(scenario(), loop_factory=new_loop) == EXPECTED
    )  # a new loop: its port releases the closed loop's calls
    gc.collect()
    assert closed() is None


def test_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")
    assert uvloop.run(scenario()) == EXPECTED
