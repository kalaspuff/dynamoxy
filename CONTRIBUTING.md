# Contributing

Thank you for helping! In short:

1. Install Rust (stable) and [uv](https://docs.astral.sh/uv/), then run `uv sync` (it builds the
   native extension).
2. Make your change with tests and docs, following [AGENTS.md](AGENTS.md) (it is written for
   humans too): where code belongs, checklists for common changes, and the invariants in
   [docs/architecture.md](docs/architecture.md).
3. Run `make check` (what CI runs) and open a pull request with a conventional-commit title
   (`feat: ...`, `fix: ...`, `docs: ...`).

Bug reports are most useful with a failing test or a small script using
`dynamoxy.testing.memory_client()`.
