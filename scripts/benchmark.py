"""Reproduces the performance numbers in the README.

    uv run --with aiobotocore python scripts/benchmark.py   # everything
    uv run python scripts/benchmark.py memory                # one part: memory, emulator or cold-start

Three measurements:

- ``memory``: wall-clock time per call on the in-memory backend, one call at a time: what
  dynamoxy itself costs, without a network.
- ``emulator``: CPU time of this process per call against a DynamoDB emulator, for dynamoxy and,
  when they are installed, boto3's low-level client and aiobotocore. Every thread counts,
  dynamoxy's Tokio threads too; the emulator runs in another process and does not. MiniStack is
  started on a free port unless ``--endpoint`` names a running emulator.
- ``cold-start``: a fresh interpreter per run, from the first import to the first item read
  (interpreter startup excluded).

Figures are medians: of the mean time per call over the rounds, and of the runs for cold starts.
The summary at the end has the README's format. The README's numbers come from the extension
built as it is published (``uv run`` uses a quicker, slightly slower build) on an otherwise idle
machine; the script warns when the load is high:

    maturin build --locked --profile dist --out target/benchmark
    uv venv -p 3.14 target/benchmark/venv
    uv pip install --python target/benchmark/venv target/benchmark/dynamoxy-*.whl boto3 aiobotocore ministack
    target/benchmark/venv/bin/python scripts/benchmark.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import gc
import importlib
import importlib.metadata
import importlib.util
import itertools
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dynamoxy
from dynamoxy import Attr, F, field, partition_key, sort_key, testing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

LIBRARIES = ("dynamoxy", "boto3", "aiobotocore")
REGION = "us-east-1"
# Row names of the summary, as in the README.
GET, PUT, QUERY = "`GetItem`, one item", "`PutItem`, one item", "`Query`, 100 items"
MEMORY_GET, MEMORY_CREATE, MEMORY_QUERY = "get() -> model", "create()", "query, 100 items -> models"


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str]
    name: Attr[str] = field(min_length=1)
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()
    version: Attr[str] = dynamoxy.version()


class Entry(dynamoxy.Model, table="entries"):
    account_id: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    amount: Attr[Decimal]
    note: Attr[str]
    at: Attr[datetime]


def ada() -> User:
    return User(user_id="u0", email="ada@example.com", name="Ada", tags={"a", "b"})


def ledger(account_id: str, count: int) -> list[Entry]:
    now = datetime.now(UTC)
    return [
        Entry(account_id=account_id, seq=seq, amount=Decimal("12.50") + seq, note=f"entry {seq}", at=now)
        for seq in range(count)
    ]


@dataclasses.dataclass
class Results:
    memory: dict[str, float] = dataclasses.field(default_factory=dict)  # seconds per call, by operation
    calls: dict[str, dict[str, float]] = dataclasses.field(default_factory=dict)  # CPU seconds per call
    cold_starts: dict[str, float] = dataclasses.field(default_factory=dict)  # seconds, by library


# Measuring


async def per_call(call: Callable[[], Awaitable[object]], calls: int, rounds: int, clock: Callable[[], float]) -> float:
    """The median, over ``rounds`` rounds of ``calls`` calls, of the ``clock`` time per call."""
    for _ in range(min(calls, 200)):  # warm up connections and caches
        await call()
    return statistics.median([await one_round(call, calls, clock) for _ in range(rounds)])


async def one_round(call: Callable[[], Awaitable[object]], calls: int, clock: Callable[[], float]) -> float:
    """The ``clock`` time per call of ``calls`` calls."""
    gc.collect()
    start = clock()
    for _ in range(calls):
        await call()
    return (clock() - start) / calls


def per_call_sync(call: Callable[[], object], calls: int, rounds: int, clock: Callable[[], float]) -> float:
    """``per_call`` for a synchronous client."""
    for _ in range(min(calls, 200)):
        call()
    means = []
    for _ in range(rounds):
        gc.collect()
        start = clock()
        for _ in range(calls):
            call()
        means.append((clock() - start) / calls)
    return statistics.median(means)


async def calls_in(seconds: float, call: Callable[[], Awaitable[object]]) -> int:
    """About how many calls take ``seconds`` (at least 10)."""
    probe = min(seconds, 0.1)
    count, start = 0, time.perf_counter()
    while time.perf_counter() - start < probe:
        await call()
        count += 1
    return max(10, round(count * seconds / probe))


def installed(library: str) -> bool:
    return importlib.util.find_spec(library) is not None


def skipped(library: str) -> None:
    print(f"{library} is not installed: skipped (uv run --with {library} ...)")


# The in-memory backend


async def memory(rounds: int, seconds: float) -> dict[str, float]:
    results: dict[str, float] = {}
    async with testing.memory_client() as client:
        await client.create_tables(User, Entry)
        users, entries = client.model(User), client.model(Entry)
        loaded = await users.create(ada())
        await users.create(User(user_id="u1", email="bob@example.com", name="Bob"))
        await entries.batch_put(ledger("a1", 100))
        keys = itertools.count()
        many_keys = [{"user_id": f"missing{index}"} for index in range(10_000)]

        async def save() -> object:
            loaded.visits += 1
            return await loaded.save()

        async def transaction() -> None:
            async with client.transaction() as tx:
                tx.create(User(user_id=f"t{next(keys)}", email="t@example.com", name="T"))
                tx.update(User.key("u1"), User.visits.add(1))

        cases: dict[str, Callable[[], Awaitable[object]]] = {
            MEMORY_GET: lambda: users.get("u0"),
            "table.get() -> dict": lambda: client.table("users").get({"user_id": "u0"}),
            MEMORY_CREATE: lambda: users.create(User(user_id=f"c{next(keys)}", email="x@example.com", name="X")),
            "save() of a loaded instance": save,
            "update() with an atomic action": lambda: users.update(User.key("u1"), User.visits.add(1)),
            MEMORY_QUERY: lambda: entries.query(account_id="a1").all(),
            "table.query, 100 items -> dicts": lambda: client.table("entries").query(F("account_id") == "a1").all(),
            "transaction of two writes": transaction,
            "batch_get(), 10,000 missing keys": lambda: client.table("users").batch_get(many_keys),
        }
        counts = {name: await calls_in(seconds, call) for name, call in cases.items()}
        means: dict[str, list[float]] = {name: [] for name in cases}
        for _ in range(rounds):  # interleaved: a burst of other work slows one round of each case, not a whole case
            for name, call in cases.items():
                means[name].append(await one_round(call, counts[name], time.perf_counter))
        for name, seconds_per_call in means.items():
            results[name] = statistics.median(seconds_per_call)
            print(f"{'memory':<12} {name:<34} {duration(results[name])} per call")
    return results


# Against an emulator


@contextlib.contextmanager
def ministack() -> Iterator[str]:
    """Runs MiniStack on a free port for the duration of the block and yields its URL."""
    command = shutil.which("ministack", path=str(Path(sys.executable).parent)) or shutil.which("ministack")
    if command is None:
        raise SystemExit("MiniStack is not installed (it is in the test dependency group); or pass --endpoint")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    environment = {**os.environ, "GATEWAY_PORT": str(port), "LOG_LEVEL": "WARNING"}
    process = subprocess.Popen([command], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while not healthy(url):
            if process.poll() is not None or time.monotonic() > deadline:
                raise SystemExit(f"MiniStack did not start (exit code {process.poll()})")
            time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/_ministack/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


@dataclasses.dataclass
class Tables:
    client: dynamoxy.Client
    users: str  # physical table names
    entries: str


@contextlib.asynccontextmanager
async def tables(endpoint: str) -> AsyncIterator[Tables]:
    """The benchmark's tables with their items, under a prefix of their own; deleted afterwards."""
    client = dynamoxy.Client(
        endpoint_url=endpoint,
        region=REGION,
        credentials=dynamoxy.Credentials("test", "test"),
        table_prefix=f"benchmark-{uuid.uuid4().hex[:8]}-",
    )
    async with client:
        await client.create_tables(User, Entry)
        try:
            await client.model(User).put(ada())
            await client.model(Entry).batch_put(ledger("a1", 100))
            yield Tables(client, client.table_name("users"), client.table_name("entries"))
        finally:
            await client.delete_tables(User, Entry)


def botocore_options(endpoint: str) -> dict[str, str]:
    return {
        "endpoint_url": endpoint,
        "region_name": REGION,
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }


async def emulator(endpoint: str, rounds: int, calls: int, query_calls: int) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {GET: {}, PUT: {}, QUERY: {}}
    clock = time.process_time  # this process, every thread

    def record(library: str, operation: str, seconds: float) -> None:
        results[operation][library] = seconds
        print(f"{library:<12} {operation:<34} {duration(seconds)} CPU per call")

    async with tables(endpoint) as names:
        users, entries = names.client.model(User), names.client.model(Entry)
        keys = itertools.count()

        def user() -> User:  # one of 500 keys
            return User(user_id=f"p{next(keys) % 500}", email="x@example.com", name="X", tags={"a"})

        def ledger_page() -> Awaitable[list[Entry]]:
            return entries.query(account_id="a1").all()

        record("dynamoxy", GET, await per_call(lambda: users.get("u0"), calls, rounds, clock))
        record("dynamoxy", PUT, await per_call(lambda: users.put(user()), calls, rounds, clock))
        record("dynamoxy", QUERY, await per_call(ledger_page, query_calls, rounds, clock))

        # The same requests in DynamoDB JSON, for the botocore clients.
        get: dict[str, Any] = {"TableName": names.users, "Key": {"user_id": {"S": "u0"}}}
        query: dict[str, Any] = {
            "TableName": names.entries,
            "KeyConditionExpression": "account_id = :a",
            "ExpressionAttributeValues": {":a": {"S": "a1"}},
        }

        def put(item: dict[str, Any]) -> dict[str, Any]:  # one of 500 keys
            return {"TableName": names.users, "Item": {**item, "user_id": {"S": f"p{next(keys) % 500}"}}}

        if installed("boto3"):
            client = importlib.import_module("boto3").client("dynamodb", **botocore_options(endpoint))
            try:
                item = client.get_item(**get)["Item"]
                record("boto3", GET, per_call_sync(lambda: client.get_item(**get), calls, rounds, clock))
                record("boto3", PUT, per_call_sync(lambda: client.put_item(**put(item)), calls, rounds, clock))
                record("boto3", QUERY, per_call_sync(lambda: client.query(**query), query_calls, rounds, clock))
            finally:
                client.close()
        else:
            skipped("boto3")
        if installed("aiobotocore"):
            session = importlib.import_module("aiobotocore.session").get_session()
            async with session.create_client("dynamodb", **botocore_options(endpoint)) as client:
                item = (await client.get_item(**get))["Item"]
                record("aiobotocore", GET, await per_call(lambda: client.get_item(**get), calls, rounds, clock))
                record("aiobotocore", PUT, await per_call(lambda: client.put_item(**put(item)), calls, rounds, clock))
                record("aiobotocore", QUERY, await per_call(lambda: client.query(**query), query_calls, rounds, clock))
        else:
            skipped("aiobotocore")
    return results


# Cold starts: each program runs in a fresh interpreter, which then prints the seconds from its
# first import to its first item.

COLD_START = {
    "dynamoxy": """
start = time.perf_counter()
import asyncio
from datetime import datetime

import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str]
    name: Attr[str] = field(min_length=1)
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()
    version: Attr[str] = dynamoxy.version()


async def main() -> None:
    credentials = dynamoxy.Credentials("test", "test")
    options = {"endpoint_url": ENDPOINT, "region": "us-east-1", "table_names": {"users": TABLE}}
    async with dynamoxy.Client(credentials=credentials, **options) as client:
        assert await client.model(User).get("u0") is not None


asyncio.run(main())
""",
    "boto3": """
start = time.perf_counter()
import boto3

client = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
assert client.get_item(TableName=TABLE, Key={"user_id": {"S": "u0"}})["Item"]
""",
    "aiobotocore": """
start = time.perf_counter()
import asyncio

import aiobotocore.session


async def main() -> None:
    session = aiobotocore.session.get_session()
    async with session.create_client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1",
                                     aws_access_key_id="test", aws_secret_access_key="test") as client:
        assert (await client.get_item(TableName=TABLE, Key={"user_id": {"S": "u0"}}))["Item"]


asyncio.run(main())
""",
}


def cold_starts(endpoint: str, table: str, runs: int) -> dict[str, float]:
    results: dict[str, float] = {}
    for library, program in COLD_START.items():
        if not installed(library):
            skipped(library)
            continue
        header = f"import time\nENDPOINT = {endpoint!r}\nTABLE = {table!r}\n"
        source = f"{header}{program}\nprint(time.perf_counter() - start)\n"
        seconds = []
        for _ in range(runs):
            done = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, check=False)
            if done.returncode != 0:
                raise SystemExit(f"the cold start of {library} failed:\n{done.stderr}")
            seconds.append(float(done.stdout))
        results[library] = statistics.median(seconds)
        print(f"{library:<12} {'cold start, import to first item':<34} {duration(results[library])}")
    return results


async def cold_start(endpoint: str, runs: int) -> dict[str, float]:
    async with tables(endpoint) as names:
        return await asyncio.to_thread(cold_starts, endpoint, names.users, runs)


# Output


def duration(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1e6:7.1f} \N{MICRO SIGN}s"
    return f"{seconds * 1e3:7.2f} ms"


def environment() -> str:
    versions = [f"dynamoxy {dynamoxy.__version__}"]
    versions += [f"{name} {importlib.metadata.version(name)}" for name in ("boto3", "aiobotocore") if installed(name)]
    lines = [f"{', '.join(versions)}; Python {platform.python_version()} on {platform.system()} {platform.machine()}"]
    cpus = os.cpu_count() or 1
    if hasattr(os, "getloadavg"):
        load = os.getloadavg()[0]
        lines.append(f"{cpus} CPUs, load average {load:.2f}")
        if load > max(1.0, cpus / 4):
            lines.append("warning: the machine is busy, so the numbers will be noisy and too high")
    return "\n".join(lines)


def summary(results: Results) -> str:
    lines = ["", "Summary, in the README's format:", ""]
    if results.calls:
        libraries = [library for library in LIBRARIES if any(library in row for row in results.calls.values())]
        lines.append("| | " + " | ".join(libraries) + " |")
        lines.append("| --- |" + " ---: |" * len(libraries))
        for operation, row in results.calls.items():
            cells = [f"{row[library] * 1e3:.2f} ms" if library in row else "" for library in libraries]
            lines.append(f"| {operation} | " + " | ".join(cells) + " |")
        lines.append("")
    if results.cold_starts:
        starts = ", ".join(f"{library} {seconds * 1e3:.0f} ms" for library, seconds in results.cold_starts.items())
        lines.append(f"Cold start, from the first import to the first item: {starts}.")
    if results.memory:
        parts = [(MEMORY_GET, "a get()"), (MEMORY_CREATE, "a create()"), (MEMORY_QUERY, "a query of 100 items")]
        timings = ", ".join(f"{label} {rounded(results.memory[name])}" for name, label in parts)
        lines.append(f"In memory: {timings}.")
    return "\n".join(lines)


def rounded(seconds: float) -> str:
    """A duration as the README gives it: whole microseconds, or milliseconds from 0.1 ms."""
    return f"{seconds * 1e6:.0f} \N{MICRO SIGN}s" if seconds < 0.0001 else f"{seconds * 1e3:.2f} ms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduces the performance numbers in the README.")
    parser.add_argument("part", nargs="?", default="all", choices=("all", "memory", "emulator", "cold-start"))
    parser.add_argument("--endpoint", help="URL of a running DynamoDB emulator (default: start MiniStack)")
    parser.add_argument("--rounds", type=int, default=7, help="rounds per measurement (default: 7)")
    parser.add_argument("--seconds", type=float, default=0.5, help="seconds per in-memory round (default: 0.5)")
    parser.add_argument("--calls", type=int, default=3000, help="calls per round against the emulator (default: 3000)")
    parser.add_argument("--query-calls", type=int, default=400, help="queries per round (default: 400)")
    parser.add_argument("--runs", type=int, default=31, help="fresh processes per cold start (default: 31)")
    arguments = parser.parse_args(argv)
    part = arguments.part
    print(environment())
    results = Results()
    if part in ("all", "memory"):
        results.memory = asyncio.run(memory(arguments.rounds, arguments.seconds))
    if part != "memory":
        with contextlib.ExitStack() as stack:
            endpoint = arguments.endpoint.rstrip("/") if arguments.endpoint else stack.enter_context(ministack())
            if part in ("all", "emulator"):
                results.calls = asyncio.run(
                    emulator(endpoint, arguments.rounds, arguments.calls, arguments.query_calls)
                )
            if part in ("all", "cold-start"):
                results.cold_starts = asyncio.run(cold_start(endpoint, arguments.runs))
    print(summary(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
