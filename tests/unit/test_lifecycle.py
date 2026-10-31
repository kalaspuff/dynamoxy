"""Interpreter lifecycle: exit, shutdown and fork (each scenario runs in a fresh interpreter)."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import textwrap

import pytest


def run_script(source: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_interpreter_exits_with_an_open_client() -> None:
    completed = run_script(
        """
        import asyncio
        from dynamoxy import testing

        async def main():
            client = testing.memory_client()
            await client.table("items").create(partition_key=("pk", "S"))
            await client.table("items").put({"pk": "a"})
            print("done")

        asyncio.run(main())
        """
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "done"


def test_calls_after_shutdown_raise() -> None:
    completed = run_script(
        """
        import asyncio
        import dynamoxy
        from dynamoxy import _core, testing

        async def main():
            client = testing.memory_client()
            await client.connect()
            _core._shutdown()
            try:
                await client.list_tables()
            except dynamoxy.ShutdownError as error:
                print("ShutdownError", error.retryable)

        asyncio.run(main())
        """
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ShutdownError False"


def test_atexit_hooks_registered_later_can_still_use_dynamoxy() -> None:
    completed = run_script(
        """
        import asyncio
        import atexit
        from dynamoxy import testing

        client = testing.memory_client()

        async def work():
            return await client.list_tables()

        def at_exit():
            print("tables", asyncio.run(work()))

        atexit.register(at_exit)
        """
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "tables []"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
def test_children_of_fork_get_a_fresh_runtime() -> None:
    completed = run_script(
        """
        import asyncio
        import os
        import warnings
        from dynamoxy import testing

        warnings.simplefilter("ignore", DeprecationWarning)  # fork() with threads

        async def roundtrip(label):
            client = testing.memory_client()
            await client.table("items").create(partition_key=("pk", "S"))
            await client.table("items").put({"pk": label})
            item = await client.table("items").get({"pk": label})
            await client.close()
            return item["pk"]

        print(asyncio.run(roundtrip("parent")), flush=True)
        pid = os.fork()
        if pid == 0:
            print(asyncio.run(roundtrip("child")), flush=True)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        print("child exit", os.waitstatus_to_exitcode(status))
        """
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["parent", "child", "child", "exit", "0"]


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="needs a free-threaded interpreter (3.13t, 3.14t)"
)
def test_free_threaded_interpreters_turn_the_gil_on_for_dynamoxy() -> None:
    # The extension declares that it needs the GIL until dynamoxy is tested without it (#178).
    env = {name: value for name, value in os.environ.items() if name != "PYTHON_GIL"}
    completed = subprocess.run(
        [sys.executable, "-W", "always::RuntimeWarning", "-c", "import sys, dynamoxy; print(sys._is_gil_enabled())"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "True"
    assert "dynamoxy._core" in completed.stderr
