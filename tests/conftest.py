"""Shared fixtures.

* ``name``: unique resource names, so tests never collide (also on a shared emulator).
* ``emulator_endpoint``: URL of the DynamoDB emulator (MiniStack). Uses
  ``DYNAMOXY_TEST_ENDPOINT_URL`` when set; otherwise starts MiniStack for the test session on a
  free port (no Docker: it is a pip package in the test dependency group).
* ``backend`` / ``client_factory`` / ``client``: integration scenarios run once per backend - the
  in-memory DynamoDB and the emulator (marked ``e2e``, so ``pytest -m "not e2e"`` skips it).
  ``target`` tells which emulator it is (MiniStack, or LocalStack through
  ``DYNAMOXY_TEST_ENDPOINT_URL``; docs/parity shows how to run it).
  Emulator clients get a unique ``table_prefix`` per test, and their tables are deleted after it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

import dynamoxy
from dynamoxy import testing

# How to start the emulator ("{port}" is replaced by a free port) and how to tell it is ready.
EMULATOR_COMMAND = ["ministack"]
EMULATOR_ENV = {"GATEWAY_PORT": "{port}", "LOG_LEVEL": "WARNING"}
HEALTH_PATH = "/_ministack/health"

FAST_RETRIES = dynamoxy.RetryPolicy(max_attempts=4, base_delay=0.001, max_delay=0.01)


def unique_name(prefix: str = "t") -> str:
    """A resource name that does not collide between tests."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def name() -> Callable[[str], str]:
    return unique_name


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(url: str, process: subprocess.Popen[bytes], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the emulator exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"{url}{HEALTH_PATH}", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"the emulator at {url} did not become healthy within {timeout} seconds")


@pytest.fixture(scope="session")
def emulator_endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    configured = os.environ.get("DYNAMOXY_TEST_ENDPOINT_URL")
    if configured:
        yield configured.rstrip("/")
        return
    if shutil.which(EMULATOR_COMMAND[0]) is None:
        pytest.skip(f"{EMULATOR_COMMAND[0]} is not installed and DYNAMOXY_TEST_ENDPOINT_URL is not set")
    port = _free_port()
    command = [part.format(port=port) for part in EMULATOR_COMMAND]
    env = {**os.environ, **{key: value.format(port=port) for key, value in EMULATOR_ENV.items()}}
    log = tmp_path_factory.mktemp("emulator") / "emulator.log"
    with log.open("wb") as output:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(url, process)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(params=["memory", pytest.param("emulator", marks=pytest.mark.e2e)])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="session")
def emulator_kind(emulator_endpoint: str) -> str:
    """``"ministack"`` or ``"localstack"`` (DynamoDB Local inside): what the emulator is.

    MiniStack answers LocalStack's health path too, so it is told apart by its own.
    """
    try:
        with urllib.request.urlopen(f"{emulator_endpoint}{HEALTH_PATH}", timeout=2) as response:
            return "ministack" if response.status == 200 else "localstack"
    except urllib.error.HTTPError as error:  # LocalStack: 501
        error.close()
        return "localstack"
    except OSError:
        return "localstack"


@pytest.fixture
def target(backend: str, request: pytest.FixtureRequest) -> str:
    """What a test runs against: ``"memory"``, ``"ministack"`` or ``"localstack"``.

    For the gaps of an emulator (docs/parity): LocalStack's free edition has no backups or
    point-in-time recovery, for example.
    """
    return "memory" if backend == "memory" else str(request.getfixturevalue("emulator_kind"))


type ClientFactory = Callable[..., dynamoxy.Client]


def emulator_client(endpoint: str, **options: Any) -> dynamoxy.Client:
    """A client for the emulator (dummy credentials, fast retries)."""
    options.setdefault("retry", FAST_RETRIES)
    return dynamoxy.Client(
        endpoint_url=endpoint,
        region="us-east-1",
        credentials=dynamoxy.Credentials("test", "test"),
        **options,
    )


@pytest.fixture
async def client_factory(backend: str, request: pytest.FixtureRequest) -> AsyncIterator[ClientFactory]:
    """Creates clients on the parametrized backend and cleans up after the test."""
    created: list[dynamoxy.Client] = []
    endpoint = request.getfixturevalue("emulator_endpoint") if backend == "emulator" else None
    prefix = f"{unique_name('dx')}-"

    def factory(**options: Any) -> dynamoxy.Client:
        if endpoint is None:
            client = testing.memory_client(**options)
        else:
            options.setdefault("table_prefix", prefix)
            client = emulator_client(endpoint, **options)
        created.append(client)
        return client

    yield factory
    if endpoint is not None:
        cleaner = emulator_client(endpoint)
        try:
            for table in await cleaner.list_tables():
                if table.startswith(prefix):
                    await cleaner.table(table).delete_table(wait=False, missing_ok=True)
        finally:
            await cleaner.close()
    for client in created:
        await client.close()


@pytest.fixture
async def client(client_factory: ClientFactory) -> dynamoxy.Client:
    """A client on the parametrized backend."""
    return client_factory()
