---
name: dynamoxy-dev-workflow
description: Default workflow for any change to the dynamoxy library (Rust core, PyO3 bindings, Python API) - where code belongs, test-first steps, docs to update, and the checks to run before finishing. Use when making any feature, fix or refactor in this repository.
---

# Changing dynamoxy

1. **Orient.** Read `AGENTS.md` and the parts of `docs/architecture.md` you touch. Decide the layer:
   - protocol, timing, I/O, DynamoDB emulation → `crates/dynamoxy-core`
   - models, validation, codecs, repositories, transactions planning, guards, hooks, middleware,
     OpenTelemetry → `python/dynamoxy`
   - conversions only → `crates/dynamoxy-py`
2. **Write the failing test first.**
   - Core logic: unit test next to the code; memory backend rules in
     `crates/dynamoxy-core/src/backend/memory/tests.rs`; timing with
     `#[tokio::test(start_paused = true)]`.
   - Python behaviour: `tests/integration/test_*.py` with the `client` fixture (runs on the memory
     backend and on MiniStack), or `tests/unit/` for pure logic. Reuse `tests/models.py`.
3. **Implement** following the surrounding style: small functions, docstrings with examples,
   precise types, actionable error messages. Model writes go through `_plan_*` in
   `repository.py` so transactions behave identically.
4. **Wire through every layer** the change crosses:
   - native API → `crates/dynamoxy-py` + `python/dynamoxy/_core.pyi`
   - client option → `Client` + `to_core()` + `crates/dynamoxy-core/src/config.rs` +
     `docs/reference/configuration.md`
   - events → Rust and Python `EventKind` + `uv run python scripts/generate_reference.py`
   - public names → `python/dynamoxy/__init__.py` `__all__` (sorted)
5. **Document**: guide in `docs/guides/`, `python/dynamoxy/AGENT_GUIDE.md` and
   `skills/dynamoxy/SKILL.md` for user-facing behaviour, `docs/architecture.md` for structure, an
   ADR in `docs/adr/` for decisions, examples if the API changed, `CHANGELOG.md` `[Unreleased]`
   notes for anything a commit title cannot say.
6. **Verify** (all must pass): `make check`, which runs
   ```bash
   cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings
   uv run ruff format --check python tests examples scripts && uv run ruff check python tests examples scripts
   uvx --from actionlint-py actionlint && uv run pyrefly check
   uv run python scripts/check_consistency.py
   cargo test --workspace && uv run pytest
   ```
7. **Commit** with a conventional commit message (`feat(repository): ...`, `fix(core): ...`);
   breaking changes use `feat!:` and explain the migration - they feed the changelog. Open the
   pull request (or a stack of them, or a publish kit when you cannot push) with
   `dynamoxy-pull-requests`.
