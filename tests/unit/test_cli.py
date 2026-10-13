"""The ``dynamoxy`` command line helpers."""

from __future__ import annotations

import subprocess
import sys

import pytest

import dynamoxy
from dynamoxy.__main__ import main


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert main(list(argv)) == 0
    return capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "version").strip() == dynamoxy.__version__


def test_events_lists_every_kind(capsys: pytest.CaptureFixture[str]) -> None:
    output = run(capsys, "events")
    names = [line.split()[0] for line in output.splitlines()]
    assert names == [kind.value for kind in dynamoxy.EventKind]
    assert "request.throttled      warning" in output


def test_operations(capsys: pytest.CaptureFixture[str]) -> None:
    output = run(capsys, "operations").split()
    assert {"GetItem", "TransactWriteItems", "ExecuteStatement"} <= set(output)


def test_reserved(capsys: pytest.CaptureFixture[str]) -> None:
    output = run(capsys, "reserved", "name", "email")
    assert output.splitlines() == ["name: reserved (dynamoxy uses #placeholders for it)", "email: not reserved"]


def test_guide_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "guide").startswith("# dynamoxy")
    assert "guide" in run(capsys)


def test_module_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "dynamoxy", "version"], capture_output=True, text=True, check=True, timeout=60
    )
    assert completed.stdout.strip() == dynamoxy.__version__
