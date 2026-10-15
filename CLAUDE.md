@AGENTS.md

## Claude Code notes

- Skills for working on this repository live in `.claude/skills/` (development workflow, adding an
  option or event kind, backend operations, model features, end-to-end testing, pull requests and
  stacks, releases, usage). Use them.
- The usage guide for applications is `python/dynamoxy/AGENT_GUIDE.md`; `skills/dynamoxy/` is the
  skill application repositories can copy.
- Prefer `uv run` for every Python command; it keeps the native extension in sync with Rust
  changes.
