"""Type-checks the typing suite (``tests/typecheck``) and the examples with pyright, mypy and pyrefly.

    uv run python scripts/check_typing.py

Applications use any of the three checkers (VS Code's Pylance is pyright), and they disagree on
details such as which metaclasses apply ``dataclass_transform``. The suite pins what the public API
promises: ``assert_type`` lines must hold, and each line marked ``# type: ignore[<mypy's code>]``
must be refused. mypy (``--warn-unused-ignores``) and pyright (``reportUnnecessaryTypeIgnoreComment``
in ``tests/typecheck/pyrightconfig.json``) check the marks themselves; pyrefly runs with ``# type:
ignore`` disabled, and its errors must fall on exactly the marked lines.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tests" / "typecheck"
#: The oldest Python dynamoxy supports, so that newer syntax in the suite or the examples fails.
PYTHON_VERSION = "3.12"
IGNORE = re.compile(r"#\s*type:\s*ignore\b")


def run(name: str, command: list[str]) -> bool:
    print(f"==> {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    return result.returncode == 0


def pyrefly() -> bool:
    """Runs pyrefly on the suite and compares the lines of its errors with the marked lines."""
    command = [
        sys.executable,
        "-m",
        "pyrefly",
        "check",
        str(SUITE.relative_to(ROOT)),
        "--enabled-ignores=pyrefly",
        "--output-format=json",
    ]
    print(f"==> pyrefly: {' '.join(command[2:])}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    try:
        errors = json.loads(result.stdout)["errors"]
    except (json.JSONDecodeError, KeyError):
        print(result.stdout, result.stderr, sep="\n")
        return False
    found: dict[tuple[str, int], list[str]] = {}
    for error in errors:
        path = (ROOT / error["path"]).resolve().relative_to(ROOT).as_posix()
        found.setdefault((path, error["line"]), []).append(f"{error['description']} [{error['name']}]")
    expected: set[tuple[str, int]] = set()
    for file in sorted(SUITE.glob("*.py")):
        path = file.relative_to(ROOT).as_posix()
        with file.open("rb") as source:
            for token in tokenize.tokenize(source.readline):
                if token.type == tokenize.COMMENT and IGNORE.match(token.string):
                    expected.add((path, token.start[0]))
    problems = [
        f"{path}:{line}: unexpected error: {message}"
        for (path, line), messages in sorted(found.items())
        if (path, line) not in expected
        for message in messages
    ]
    problems += [
        f"{path}:{line}: pyrefly accepts a line marked as refused" for path, line in sorted(expected - found.keys())
    ]
    for problem in problems:
        print(problem)
    print(f"pyrefly: {len(problems)} problems, {len(expected)} refused lines checked")
    return not problems


def main() -> int:
    targets = [str(SUITE.relative_to(ROOT)), "examples"]
    results = [
        run(
            "pyright",
            [
                sys.executable,
                "-m",
                "pyright",
                "--pythonpath",
                sys.executable,
                "--project",
                str((SUITE / "pyrightconfig.json").relative_to(ROOT)),
            ],
        ),
        run(
            "mypy",
            [
                sys.executable,
                "-m",
                "mypy",
                "--python-version",
                PYTHON_VERSION,
                "--warn-unused-ignores",
                "--strict-equality",
                "--no-incremental",
                *targets,
            ],
        ),
        pyrefly(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
