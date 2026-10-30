"""Checks that code, stubs and documentation agree. Run by CI and ``make check``.

    uv run python scripts/check_consistency.py

Each check yields what to fix. Add a check whenever a new "remember to also update X" rule
appears, so that agents and humans cannot forget it.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import dynamoxy
from dynamoxy import _core, errors
from dynamoxy.events import PYTHON_EVENT_KINDS, EventKind

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_reference  # noqa: E402 - after sys.path setup

Check = Callable[[], Iterator[str]]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def parameters(target: Any) -> list[str]:
    """Documented names of a constructor, function or dataclass (``self``/``cls`` excluded)."""
    if dataclasses.is_dataclass(target):
        return [field.name for field in dataclasses.fields(target)]
    return [name for name in inspect.signature(target).parameters if name not in ("self", "cls", "kwargs")]


#: Sections of docs/reference/configuration.md and the callables they document.
CONFIGURATION: dict[str, Any] = {
    "`Client(...)`": dynamoxy.Client,
    "`RetryPolicy(...)`": dynamoxy.RetryPolicy,
    "`Credentials(...)`": dynamoxy.Credentials,
    "`Telemetry(...)`": dynamoxy.Telemetry,
    "`LoggingConfig(...)`": dynamoxy.LoggingConfig,
    "`class Model(..., table=...)`": dynamoxy.Model.__init_subclass__,
    "`field(...)`": dynamoxy.field,
    "`partition_key(...)`": dynamoxy.partition_key,
    "`sort_key(...)`": dynamoxy.sort_key,
    "`created_at(...)`": dynamoxy.created_at,
    "`updated_at(...)`": dynamoxy.updated_at,
    "`version(...)`": dynamoxy.version,
    "`ttl(...)`": dynamoxy.ttl,
    "`GlobalIndex(...)`": dynamoxy.GlobalIndex,
    "`LocalIndex(...)`": dynamoxy.LocalIndex,
    "`Throughput(...)`": dynamoxy.Throughput,
    "`Billing(...)`": dynamoxy.Billing,
    "`IndexDefinition(...)`": dynamoxy.IndexDefinition,
    "`client.create_tables(...)`": dynamoxy.Client.create_tables,
    "`Table.create(...)`": dynamoxy.Table.create,
    "`testing.memory_client(...)`": dynamoxy.testing.memory_client,
    "`testing.inject_fault(...)`": dynamoxy.testing.inject_fault,
}


@check
def generated_references() -> Iterator[str]:
    if read("docs/reference/events.md") != generate_reference.render_events():
        yield "docs/reference/events.md is stale: run `uv run python scripts/generate_reference.py`"


@check
def event_kinds_match() -> Iterator[str]:
    rust = [name for name, _, _ in _core.event_kinds()]
    python = [kind.value for kind in EventKind]
    if python[: len(rust)] != rust:
        yield (
            f"EventKind differs between crates/dynamoxy-core/src/events.rs {rust} and the first members of "
            f"python/dynamoxy/events.py {python[: len(rust)]}"
        )
    extra = python[len(rust) :]
    documented = [kind.value for kind in PYTHON_EVENT_KINDS]
    if extra != documented:
        yield f"python/dynamoxy/events.py: PYTHON_EVENT_KINDS {documented} must list the Python-only kinds {extra}"


def stub_names(tree: ast.Module) -> set[str]:
    names = {node.name for node in tree.body if isinstance(node, ast.ClassDef | ast.FunctionDef)}
    names |= {
        node.target.id for node in tree.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    names |= {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return names


@check
def stub_matches_native_module() -> Iterator[str]:
    tree = ast.parse(read("python/dynamoxy/_core.pyi"))
    names = stub_names(tree)
    names -= typed_dicts(tree)  # typing-only shapes of native dicts
    names -= {"RawEvent"}  # a type alias of the stub
    runtime = {name for name in dir(_core) if not name.startswith("__") or name == "__version__"}
    for name in sorted(runtime - names):
        yield f"python/dynamoxy/_core.pyi is missing `{name}` (exported by crates/dynamoxy-py)"
    for name in sorted(names - runtime):
        yield f"python/dynamoxy/_core.pyi declares `{name}`, which the native module does not export"
    for class_name in sorted(names & runtime):
        runtime_object = getattr(_core, class_name)
        if not isinstance(runtime_object, type) or issubclass(runtime_object, BaseException):
            continue
        stub_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        stub_members = {node.name for node in stub_class.body if isinstance(node, ast.FunctionDef)}
        runtime_members = {member for member in vars(runtime_object) if not member.startswith("_")}
        for member in sorted(runtime_members - stub_members):
            yield f"python/dynamoxy/_core.pyi: `{class_name}.{member}` is missing"
        for member in sorted(stub_members - runtime_members - {"__init__", "__eq__", "__hash__"}):
            yield f"python/dynamoxy/_core.pyi: `{class_name}.{member}` does not exist at runtime"


def typed_dicts(tree: ast.Module) -> set[str]:
    """Names of the stub's TypedDict classes, including subclasses of other TypedDicts."""
    found = {"TypedDict"}
    changed = True
    while changed:
        changed = False
        for node in tree.body:
            if (
                isinstance(node, ast.ClassDef)
                and node.name not in found
                and any(isinstance(base, ast.Name) and base.id in found for base in node.bases)
            ):
                found.add(node.name)
                changed = True
    return found - {"TypedDict"}


def sections(markdown: str) -> dict[str, str]:
    """Markdown text by `## ` heading."""
    parts: dict[str, str] = {}
    current = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            parts[current] = ""
        else:
            parts[current] = parts.get(current, "") + line + "\n"
    return parts


@check
def configuration_reference() -> Iterator[str]:
    docs = sections(read("docs/reference/configuration.md"))
    for section, target in CONFIGURATION.items():
        text = docs.get(section)
        if text is None:
            yield f"docs/reference/configuration.md has no section {section}"
            continue
        names = parameters(target)
        documented = set(re.findall(r"^\| `([a-z0-9_]+)` \|", text, re.MULTILINE))
        for name in names:
            if name not in documented:
                yield f"docs/reference/configuration.md section {section} does not document `{name}`"
        for name in sorted(documented - set(names)):
            yield f"docs/reference/configuration.md section {section} documents unknown parameter `{name}`"
    for section in sorted(set(docs) - set(CONFIGURATION) - {""}):
        yield f"docs/reference/configuration.md section {section} is not checked: add it to CONFIGURATION"


@check
def error_reference() -> Iterator[str]:
    text = read("docs/reference/errors.md")
    for name in errors.__all__:
        if f"`{name}`" not in text:
            yield f"docs/reference/errors.md does not document `{name}`"
    exported = set(dynamoxy.__all__)
    for name in errors.__all__:
        if name not in exported:
            yield f"dynamoxy.{name} is not exported from python/dynamoxy/__init__.py"


@check
def public_api_defined() -> Iterator[str]:
    for name in dynamoxy.__all__:  # ruff (RUF022) keeps the list sorted
        if not hasattr(dynamoxy, name):
            yield f"dynamoxy.__all__ lists `{name}`, which the package does not define"


@check
def examples_listed() -> Iterator[str]:
    readme = read("examples/README.md")
    for path in sorted((ROOT / "examples").glob("*.py")):
        if not path.stem.startswith("_") and f"]({path.name})" not in readme:
            yield f"examples/README.md does not list {path.name}"


@check
def guides_linked() -> Iterator[str]:
    index = read("docs/index.md")
    for path in sorted((ROOT / "docs" / "guides").glob("*.md")):
        if f"guides/{path.name}" not in index:
            yield f"docs/index.md does not link docs/guides/{path.name}"
    for path in sorted((ROOT / "docs" / "reference").glob("*.md")):
        if f"reference/{path.name}" not in index:
            yield f"docs/index.md does not link docs/reference/{path.name}"


@check
def adrs_indexed() -> Iterator[str]:
    index = read("docs/adr/README.md")
    for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        if f"({path.name})" not in index:
            yield f"docs/adr/README.md does not list {path.name}"


@check
def versions_agree() -> Iterator[str]:
    cargo = tomllib.loads(read("Cargo.toml"))
    version = cargo["workspace"]["package"]["version"]
    if dynamoxy.__version__ != version:
        yield f"installed dynamoxy {dynamoxy.__version__} != Cargo.toml {version} (rebuild: `uv sync`)"
    core_dependency = cargo["workspace"]["dependencies"]["dynamoxy-core"]["version"]
    if core_dependency != version:
        yield f"Cargo.toml workspace dependency dynamoxy-core = {core_dependency}, expected {version}"
    changelog = read("CHANGELOG.md")
    if "## [Unreleased]" not in changelog:
        yield "CHANGELOG.md must keep a '## [Unreleased]' section"
    if f"## [{version}]" not in changelog:
        yield f"CHANGELOG.md has no section for the current version {version}"


#: Deprecated APIs: (owner, attribute) and the version that removes them. From that version on,
#: this check fails until the API (and its entry) is gone, so a promised removal is not forgotten.
DEPRECATED: dict[str, tuple[Any, str, str]] = {
    "Client.repository": (dynamoxy.Client, "repository", "0.5.0"),
}


def version_tuple(version: str) -> tuple[int, ...]:
    """``"0.5.0"`` as ``(0, 5, 0)`` (pre-release and build suffixes are ignored)."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3])


@check
def deprecations_removed_on_schedule() -> Iterator[str]:
    version = tomllib.loads(read("Cargo.toml"))["workspace"]["package"]["version"]
    for name, (owner, attribute, removal) in DEPRECATED.items():
        if version_tuple(version) >= version_tuple(removal) and hasattr(owner, attribute):
            yield (
                f"{name} is deprecated and due for removal in {removal}, but version {version} still has it: "
                "remove it, its tests and docs, and its entry in DEPRECATED (scripts/check_consistency.py)"
            )


@check
def agent_guide_packaged() -> Iterator[str]:
    if len(read("python/dynamoxy/AGENT_GUIDE.md")) < 2000:
        yield "python/dynamoxy/AGENT_GUIDE.md looks empty; it is printed by `dynamoxy guide`"
    if "dynamoxy guide" not in read("skills/dynamoxy/SKILL.md"):
        yield "skills/dynamoxy/SKILL.md must point agents to `dynamoxy guide`"
    if "AGENT_GUIDE.md" not in read("llms.txt"):
        yield "llms.txt must link python/dynamoxy/AGENT_GUIDE.md"


@check
def skills_have_frontmatter() -> Iterator[str]:
    for path in sorted([*(ROOT / ".claude" / "skills").glob("*/SKILL.md"), ROOT / "skills" / "dynamoxy" / "SKILL.md"]):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n", text, re.DOTALL)
        relative = path.relative_to(ROOT)
        if match is None:
            yield f"{relative} needs frontmatter with `name` and `description`"
        elif match.group(1) != path.parent.name:
            yield f"{relative}: name {match.group(1)!r} must match its directory {path.parent.name!r}"
        elif "Use when" not in match.group(2):
            yield f"{relative}: the description must say when to use the skill ('Use when ...')"


def main() -> int:
    problems = [problem for function in CHECKS for problem in function()]
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        print(f"\n{len(problems)} consistency problem(s). See AGENTS.md for the rules.")
        return 1
    print(f"✓ {len(CHECKS)} consistency checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
