"""Every example runs on every backend (examples are documentation that cannot rot)."""

from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import dynamoxy

EXAMPLES = sorted(
    path.stem for path in (Path(__file__).parents[2] / "examples").glob("*.py") if path.stem != "__init__"
)
MEMORY_ONLY = {"testing_with_faults"}  # injects faults, which only the in-memory backend supports

EXPECTED: dict[str, Any] = {
    "observability": lambda summary: summary["calls"] == 5,
    "pagination": lambda pages: pages == 3,
    "repository_pattern": lambda open_tasks: open_tasks == 1,
    "single_table": lambda kinds: sorted(kinds) == ["Customer", "Order", "Order", "Order"],
    "transactions": lambda balances: balances == (Decimal(70), Decimal(30)),
    "unique_constraints": lambda outcomes: len(outcomes) == 3,
    "testing_with_faults": lambda checks: len(checks) == 3,
    "raw_tables": lambda low: len(low) == 2,
}


def test_every_example_is_checked() -> None:
    assert set(EXAMPLES) == set(EXPECTED) | {"quickstart", "pydantic_models"}


@pytest.mark.parametrize("name", EXAMPLES)
async def test_example_runs(name: str, client: dynamoxy.Client, backend: str) -> None:
    if backend != "memory" and name in MEMORY_ONLY:
        pytest.skip("needs the in-memory backend")
    module = importlib.import_module(f"examples.{name}")
    result = await module.main(client)
    check = EXPECTED.get(name)
    assert check is None or check(result), result
