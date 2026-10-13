---
name: dynamoxy-usage
description: Write or review code that uses dynamoxy (examples, docs snippets, tests, the usage guide) following the library's recommended patterns. Use when editing examples/, docs/guides/, python/dynamoxy/AGENT_GUIDE.md or skills/dynamoxy/SKILL.md, or when answering how to use dynamoxy.
---

# Writing code that uses dynamoxy

1. Read `python/dynamoxy/AGENT_GUIDE.md`: it is the canonical usage reference and ships in the
   wheel (`dynamoxy guide`). Keep it, `skills/dynamoxy/SKILL.md`, the guides and the examples
   consistent with each other.
2. Patterns to show: one shared client (`async with`), repositories (subclasses with domain
   methods), the right write (`create`/`save`/`update`/`put`), atomic update actions, conditions,
   unique fields, versions, transactions for multi-item invariants, cursors for pagination,
   `dynamoxy.testing.memory_client()` in tests.
3. Anti-patterns to avoid in docs: scans on request paths, read-modify-write without a version,
   formatting values into PartiQL or `condition()` text, logging items, creating clients per
   request.
4. Examples (`examples/*.py`) expose `async def main(client=None)`, run on the memory backend by
   default, return something `tests/integration/test_examples.py` can assert on, and are listed in
   `examples/README.md` (checked).
5. Code blocks in `python/dynamoxy/AGENT_GUIDE.md` are formatted by `ruff format`; keep them valid
   Python.
