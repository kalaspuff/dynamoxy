<!-- Title: conventional commit, e.g. "feat(repository): add conditional batch deletes". It becomes the changelog entry. -->

## What and why

## Checklist

- [ ] Tests cover the change (in-memory backend and MiniStack where behaviour touches DynamoDB)
- [ ] Docs updated (guide, `docs/reference/*`, `AGENT_GUIDE.md`/`skills/dynamoxy` if usage changed, ADR if a decision was made)
- [ ] `make check` passes locally (lint, types, clippy, consistency, tests)
- [ ] Breaking change? Marked with `!` in the title and explained above
