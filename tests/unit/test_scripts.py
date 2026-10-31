"""The repository scripts (consistency checks, release helpers, wheel smoke test, benchmarks) keep working."""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_and_docs_are_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    assert load("check_consistency").main() == 0, capsys.readouterr().out


@pytest.mark.parametrize(("version", "due"), [("0.4.0", False), ("0.4.9", False), ("0.5.0", True), ("1.0.0", True)])
def test_deprecated_apis_fail_the_checks_once_due(monkeypatch: pytest.MonkeyPatch, version: str, due: bool) -> None:
    check_consistency = load("check_consistency")
    read = check_consistency.read
    cargo = f'[workspace.package]\nversion = "{version}"\n'
    monkeypatch.setattr(check_consistency, "read", lambda path: cargo if path == "Cargo.toml" else read(path))
    problems = list(check_consistency.deprecations_removed_on_schedule())
    assert bool(problems) == due
    if due:
        assert "Client.repository is deprecated and due for removal in 0.5.0" in problems[0]
    assert check_consistency.version_tuple("0.5.0-rc.1+build.7") == (0, 5, 0)


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (
            lambda init: init.replace("from dynamoxy import errors as errors\n", ""),
            "must import `from dynamoxy import errors as errors`",
        ),
        (
            lambda init: init + "\n\ndef __getattr__(name: str) -> object:\n    raise AttributeError(name)\n",
            "must define __getattr__ under `if not TYPE_CHECKING:`",
        ),
    ],
)
def test_submodules_the_type_checkers_cannot_see_fail_the_checks(
    monkeypatch: pytest.MonkeyPatch, edit: Callable[[str], str], problem: str
) -> None:
    check_consistency = load("check_consistency")
    read = check_consistency.read
    init = edit(read("python/dynamoxy/__init__.py"))
    assert init != read("python/dynamoxy/__init__.py")
    monkeypatch.setattr(check_consistency, "read", lambda path: init if path.endswith("__init__.py") else read(path))
    assert [problem in found for found in check_consistency.submodules_typed()] == [True]


def test_generated_reference_is_current() -> None:
    generate_reference = load("generate_reference")
    assert generate_reference.EVENTS.read_text(encoding="utf-8") == generate_reference.render_events()


@pytest.mark.parametrize(
    ("current", "requested", "expected"),
    [
        ("0.4.2", "patch", "0.4.3"),
        ("0.4.2", "minor", "0.5.0"),
        ("0.4.2", "major", "0.5.0"),
        ("1.2.3", "major", "2.0.0"),
        ("0.1.0", "1.0.0", "1.0.0"),
    ],
)
def test_next_version(current: str, requested: str, expected: str) -> None:
    assert load("release").next_version(current, requested) == expected


def test_invalid_versions_are_refused() -> None:
    with pytest.raises(SystemExit, match="invalid version"):
        load("release").next_version("0.1.0", "huge")


def test_changelog_update_moves_unreleased_notes(tmp_path: Path) -> None:
    release = load("release")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\nHand-written note.\n\n## [0.1.0] - 2026-01-01\n\nOld.\n")
    release.update_changelog(
        "0.2.0", generated="## [0.2.0] - 2026-02-02\n\n### Features\n\n- New thing", path=changelog
    )
    text = changelog.read_text()
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-02-02\n\nHand-written note.\n\n### Features" in text
    assert load("release_notes").section(text, "0.2.0") == "Hand-written note.\n\n### Features\n\n- New thing"


def test_first_release_dates_the_placeholder(tmp_path: Path) -> None:
    release = load("release")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - Unreleased\n\nFirst release.\n")
    release.update_changelog("0.1.0", path=changelog, today=datetime.date(2026, 10, 13))
    text = changelog.read_text()
    assert "## [0.1.0] - 2026-10-13\n" in text
    assert load("release_notes").section(text, "0.1.0") == "First release."


def test_cargo_bump(tmp_path: Path) -> None:
    release = load("release")
    cargo = tmp_path / "Cargo.toml"
    cargo.write_text((SCRIPTS.parent / "Cargo.toml").read_text(encoding="utf-8"))
    version = release.current_version()
    release.bump_cargo(version, "9.8.7", path=cargo, update_lock=False)
    text = cargo.read_text()
    assert 'version = "9.8.7"' in text
    assert 'dynamoxy-core = { path = "crates/dynamoxy-core", version = "9.8.7" }' in text


def test_release_notes_of_the_current_version() -> None:
    release = load("release")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_notes.py"), f"v{release.current_version()}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_smoke_test_passes() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "smoke_test.py")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "OK on" in result.stdout


def benchmark(*arguments: str) -> str:
    """Runs scripts/benchmark.py with tiny parameters and returns its output."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "benchmark.py"), *arguments, "--rounds", "1"],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_benchmark_on_the_memory_backend() -> None:
    assert "In memory: a get() " in benchmark("memory", "--seconds", "0.001")


@pytest.mark.e2e
def test_benchmark_against_the_emulator(emulator_endpoint: str) -> None:
    options = ("--endpoint", emulator_endpoint, "--calls", "2", "--query-calls", "1", "--runs", "1")
    assert "| `GetItem`, one item | " in benchmark("emulator", *options)
    assert "Cold start, from the first import to the first item: dynamoxy " in benchmark("cold-start", *options)


def test_probe_backends_runs_on_the_memory_backend(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Models declared in the script register their tables: load it once per process.
    probe = sys.modules.get("scripts_probe_backends") or load("probe_backends")
    sys.modules["scripts_probe_backends"] = probe
    out = tmp_path / "probe-memory.json"
    assert probe.main(["run", "--target", "memory", "--only", "versions", "index_reads", "--out", str(out)]) == 0
    facts = json.loads(out.read_text())["facts"]
    assert facts["versions"]["save_after_put"]["error"] == "VersionConflictError"
    assert facts["index_reads"]["lsi_scan_filter_ALL_ATTRIBUTES"] == {"ok": 1}
    assert "dynamoxy-probe-" not in out.read_text(), "the run's table prefix is left out"
    assert probe.main(["compare", str(out), str(out)]) == 0
    assert "no differences" in capsys.readouterr().out
    assert '"dynamodb:ListTables"' in json.dumps(probe.iam_policy("123456789012"))
