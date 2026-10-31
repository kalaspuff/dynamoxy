# AGENTS.md - working on dynamoxy

This file is for AI agents (and humans) changing dynamoxy itself. If you are *using* dynamoxy in an
application, read `python/dynamoxy/AGENT_GUIDE.md` (`dynamoxy guide`) instead.

dynamoxy is a typed, async DynamoDB ORM: a Rust core (`crates/dynamoxy-core`), a thin PyO3 binding
(`crates/dynamoxy-py` → `dynamoxy._core`) and the Python API (`python/dynamoxy`). Read
[docs/architecture.md](docs/architecture.md) before any non-trivial change. It defines the layers,
the pipelines and the invariants you must preserve.

## Repository map

```text
crates/dynamoxy-core/src/        Rust core (no Python)
  client.rs (+ client/tests.rs)  retries, timeouts, events, metrics, managed batches, waiters
  backend/mod.rs                 the Backend port (one method per DynamoDB operation)
  backend/aws/                   AWS SDK adapter; errors.rs classifies every SDK error
  backend/memory/                in-memory DynamoDB (items, query, batch, transact, partiql, admin)
  expr/                          expression language: parser, eval, update, projection, reserved words
  value/ types/                  AttributeValue, Number, item sizes; operation inputs/outputs
  events.rs metrics.rs retry.rs config.rs error.rs
crates/dynamoxy-core/tests/      emulator.rs: parity scenarios (memory + MiniStack)
crates/dynamoxy-py/src/          PyO3 bindings: runtime.rs (Tokio + asyncio bridge), client.rs, convert.rs, errors.rs
python/dynamoxy/                 Python API (models, schema, codecs, repository, transactions, query, ...)
python/dynamoxy/_core.pyi        Type stubs of the native module - keep in sync with crates/dynamoxy-py
python/dynamoxy/AGENT_GUIDE.md   Usage guide shipped in the wheel (`dynamoxy guide`)
tests/unit|integration|e2e       pytest suites (integration runs on the memory backend AND MiniStack)
tests/models.py                  Shared test models (plain, single-table, pydantic)
tests/typecheck/                 Typing suite: what pyright, mypy and pyrefly infer and refuse (scripts/check_typing.py)
examples/                        Runnable examples (executed by tests/integration/test_examples.py)
docs/                            Guides, reference (configuration/errors checked, events generated), ADRs
skills/dynamoxy/SKILL.md         Skill for agents of applications using dynamoxy (copy into a project)
.claude/skills/                  Skills for agents developing dynamoxy (this repository)
scripts/                         check_consistency.py, check_typing.py, generate_reference.py, release.py, release_notes.py, smoke_test.py, benchmark.py,
                                 probe_dynamodb.sh and probe_backends.py (DynamoDB's answers, docs/parity)
.github/workflows/               ci.yml, pr-title.yml, prepare-release.yml, tag-release.yml, release.yml
```

## Setup and commands

Requirements: Rust (stable, see `rust-toolchain.toml`) and [uv](https://docs.astral.sh/uv/). No Docker.

| Task | Command |
| --- | --- |
| Install/refresh everything (builds the extension) | `uv sync` |
| All checks (what CI runs) | `make check` |
| Python tests (unit, integration on memory and MiniStack, e2e) | `uv run pytest` |
| Only fast tests (no emulator) | `uv run pytest -m "not e2e"` |
| Rust tests | `cargo test --workspace` |
| Rust parity tests against the emulator | `DYNAMOXY_TEST_ENDPOINT_URL=<url> cargo test -p dynamoxy-core --test emulator` |
| Lint/format Python | `uv run ruff check --fix python tests examples scripts && uv run ruff format python tests examples scripts` |
| Type check Python | `uv run pyrefly check` |
| Typing suite and examples with pyright, mypy and pyrefly | `uv run python scripts/check_typing.py` |
| Lint/format Rust | `cargo fmt --all && cargo clippy --workspace --all-targets -- -D warnings` |
| Docs drift checks | `uv run python scripts/check_consistency.py` |
| Regenerate generated reference docs | `uv run python scripts/generate_reference.py` |
| Release wheels for all Linux targets | `make wheels` |
| Smoke-test an installed wheel | `python -I scripts/smoke_test.py` |
| Measure the README's performance numbers | `uv run --with aiobotocore python scripts/benchmark.py` |
| Lint GitHub workflows | `uvx --from actionlint-py actionlint` (part of `make lint`) |

`uv run ...` rebuilds the native extension automatically when Rust sources change (`[tool.uv]
cache-keys`). The pytest fixture starts MiniStack (a pip-installed AWS emulator, part of the test
dependency group) by itself unless `DYNAMOXY_TEST_ENDPOINT_URL` is set. Set `CARGO_TARGET_DIR`
outside the repository if the workspace is a slow mounted volume.

## Golden rules

1. **Respect the layer split.** Protocol, timing, I/O and the DynamoDB emulation → Rust core.
   Models, validation, codecs, repositories, guards, hooks, middleware, OpenTelemetry → Python.
   The binding crate contains conversions only.
2. **Never break the invariants** in docs/architecture.md (fail fast before I/O, no values in
   expressions, unique guards consistent, versions checked, both backends equal, nothing dropped
   silently, fork safety, clean exit, instances bound to their repository, stored formats
   stable, no payload logging).
3. **Every behaviour change ships with tests, docs and a changelog-worthy commit message.** CI
   enforces formatting, lint, types (pyrefly; `tests/typecheck` with pyright and mypy too), clippy `-D warnings`, tests on Python 3.12-3.15,
   and `scripts/check_consistency.py`.
4. **Both backends.** A backend operation is implemented in the AWS adapter *and* the memory
   backend, with DynamoDB's semantics and error codes, and tested against both. Python
   integration tests use the `client` / `client_factory` fixtures, so they run on both.
5. **Errors are actionable.** Validation errors say what to change; service errors keep the code,
   the request id and a retry classification; model-level failures map to specific errors.
6. **The public API is typed and documented.** Google-style docstrings, PEP 695 generics,
   `from __future__ import annotations`, no `Any` where a real type exists, `@override` on
   overrides. Rust public items need docs (`missing_docs`); `unsafe` is forbidden.
7. **No required runtime dependencies** in the Python package. pydantic and OpenTelemetry are
   extras, imported lazily.
8. **Stored formats are API** (codecs, timestamps, `_type`, guard keys, cursors): keep reading the
   old format, write an ADR, mention it in the changelog.

## Change checklists

**New or changed client option**:
- [ ] `Client.__init__` parameter with docstring and validation (or a dataclass field)
- [ ] Rust `ClientConfig` field (serde, `deny_unknown_fields`) if the core needs it, and `to_core()`
- [ ] tests for validation and the mapping
- [ ] `docs/reference/configuration.md` (CI checks every parameter) and the guides
- [ ] `python/dynamoxy/AGENT_GUIDE.md` cheat sheet if applications should know it

**New field option or codec**: `attributes.py` (specifier and overloads), `schema.py` (`FieldSpec`,
validation), `codecs.py` (codec, `resolve_codec`), pydantic marker if applicable, unit tests,
`docs/reference/configuration.md`, modeling guide, AGENT_GUIDE.

**New event kind**:
- [ ] `event_kinds!` in `crates/dynamoxy-core/src/events.rs` (name, level, description), or for
      Python-only kinds `EventKind` + `PYTHON_EVENT_KINDS` in `python/dynamoxy/events.py`
- [ ] `EventKind` in `python/dynamoxy/events.py` in the same position as in Rust
- [ ] emit it, and test that it is emitted
- [ ] `uv run python scripts/generate_reference.py`

**New native API**: Rust binding in `crates/dynamoxy-py`, `python/dynamoxy/_core.pyi` (CI compares
names and methods), Python wrapper, tests.

**New backend operation**: `Backend` trait method (`backend/mod.rs`), types in `types/ops.rs`, AWS
adapter (+ conversions, errors in one place), memory backend with DynamoDB's semantics and error
messages, `Client` method in `client.rs` (retries, events, metrics), binding, stub, Python wrapper,
tests in Rust (memory + `tests/emulator.rs`) and Python (`tests/integration`).

**New repository or model behaviour**: plan it in `repository.py` (`_plan_*` so transactions get it
too), map failures in `explain_failure`, test on both backends (`tests/integration`), update the
guides and AGENT_GUIDE.

**Renamed or retired API**: keep the old one working for one minor version, decorated with
`@deprecated("... will be removed in dynamoxy 0.N: use ...")` from `dynamoxy._utils` (a
`DeprecationWarning` at runtime, flagged by type checkers), add it to `DEPRECATED` in
`scripts/check_consistency.py` (the check fails from the removal version on), test that it warns,
move the docs, examples and tests to the new API, and note it under `### Deprecated` in
`CHANGELOG.md`.

**Bug fix**: reproduce with a failing test first (memory backend if possible, emulator if it is a
protocol issue), fix, keep the test.

## Testing guidance

* Rust unit tests live next to the code (`#[tokio::test(start_paused = true)]` for timing).
  Memory backend behaviour tests are in `backend/memory/tests.rs`; each documents a DynamoDB rule.
* Python integration tests take the `client` fixture: each scenario runs on the memory backend and
  on MiniStack. Faults (`dynamoxy.testing.inject_fault`) only exist in the memory backend: such
  tests build their own memory client (`tests/integration/test_faults.py`).
* Assert on events, stats, counts and `dynamoxy.collect()` records instead of sleeping. Use unique
  names; the fixtures give emulator clients a unique table prefix.
* A test that fails only on MiniStack usually reveals real DynamoDB behaviour: check the DynamoDB
  documentation, model it in the memory backend, and only treat it as an emulator quirk (with a
  comment) when the documentation says otherwise.
* [docs/parity](docs/parity/README.md) keeps DynamoDB's answers measured against the service, and
  MiniStack's and LocalStack's, with the probe script that asked them. Look there before guessing
  a message or a limit; when neither it nor the documentation settles a question, add a round of
  probes for a maintainer to run against AWS.
* Examples are tests: if you change the API, update `examples/`.
* Never pipe test output through `head`/`tail` without `set -o pipefail`.

## Documentation duties

Update in the same change: the guide in `docs/guides/`, `docs/reference/*` (generated files via
the script), `python/dynamoxy/AGENT_GUIDE.md` and `skills/dynamoxy/SKILL.md` for user-facing
behaviour, `docs/architecture.md` for structural changes, a new ADR for decisions, docstrings
everywhere, and `CHANGELOG.md` notes under `[Unreleased]` when the commit message is not enough.
The README stays short and links to the docs.

## Commits, PRs and releases

* Conventional commits (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `test:`, `ci:`, `build:`,
  `chore:`; `feat!:` or `BREAKING CHANGE:` for breaking changes). PR titles are checked, squash
  merges use them, and git-cliff builds the changelog from them.
* Agents that can push to GitHub work in their own checkout like any contributor: commit on a
  branch, push it, open the pull request with `gh pr create`, and follow its checks
  (`gh pr checks`) and reviews. That takes no publish kit, no scratch clone and no separate
  go-ahead. Merging, pushes to main and releases stay with the maintainer unless they ask.
  Publish kits are only for agents that cannot push.
* Agents never force-push a published branch or rewrite its commits: fixes, review changes and
  updates from main are new commits or merges.
* Pull requests are squash-merged: the title becomes the subject of the commit on main and its
  changelog line, the description its body. Write descriptions for that history: what changes
  and why, how it was verified, the migration of a breaking change, and no instructions for
  whoever merges. Write each paragraph and list item on one line; GitHub wraps them at 72
  columns in the squash commit. Split large changes into stacked pull requests that each pass CI.
  `.claude/skills/dynamoxy-pull-requests` covers pull requests, stacks and their updates after
  merges, fixing failed checks, and publish kits.
* One version for the workspace (`Cargo.toml` `[workspace.package]`); the Python package takes it
  from the binding crate. While 0.x, breaking changes bump the minor version.
* Release: run the "Prepare release" workflow, polish the generated CHANGELOG.md section in the PR
  and squash-merge it. The tag is created and `release.yml` builds, tests and publishes. See
  `.claude/skills/dynamoxy-release`.

## Things to avoid

* Calling into Python (or taking the GIL) from Rust threads: events are pulled by Python, and a
  call's future returns a `runtime::Deferred` that builds its result on the event loop's thread.
* Threads or Tokio runtimes outside `crates/dynamoxy-py/src/runtime.rs`, or awaitables not created
  with `runtime::future_into_py` (`PyCoreClient::run` picks it or `future_into_py_inline`).
* Holding a `parking_lot` lock across `.await`.
* Putting values into expression strings, or attribute names without placeholders.
* Long timeouts in tests; `time.sleep` in async code; global mutable state in Python modules.
* Logging item contents or key values (they may contain personal data).
* Weakening validation to make a test pass; skipping the memory backend; listing a class (rather
  than functions) in `dataclass_transform(field_specifiers=...)`.
