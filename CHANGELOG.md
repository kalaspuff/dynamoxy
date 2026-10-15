# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

### Features

- Model instances persist themselves through the repository that last read or wrote them
  ([ADR 0011](docs/adr/0011-instances-remember-their-repository.md)): `save()`, `delete()`,
  `update(*actions)`, `add(path, value)`, `increment(path, amount=1)`, `decrement(...)` and
  `refresh()`, on native and pydantic models. They call the repository's public methods, so
  validation, hooks, versions, unique guards and the overrides of repository subclasses apply.
  `add()` adds to numbers, adds set elements and appends to lists (creating missing lists);
  `add()`, `increment()` and `decrement()` work on nested paths, check the value against the
  attribute before any I/O, and are typed for type checkers. Every read and write binds
  instances, transactions and batch writers keep the binding, and pickles drop it.
- Lifecycle hooks for updates: `@dynamoxy.before_update` and `@dynamoxy.after_update` (and
  `client.hook("after_update", Model)`) run around updates of instances, in repositories,
  transactions and the new instance methods.

### Changed

- [**breaking**] `save`, `delete`, `update`, `add`, `increment`, `decrement` and `refresh` are model
  methods now, so `Model` fields can no longer use these names: rename the field and keep the
  stored name with `attribute_name=`. Documents (nested maps) no longer reserve the names of model
  methods (`key`, `primary_key`, `is_loaded`).

### Performance

- Overall, an in-memory `get` takes under 10 µs instead of about 60 µs, and against an emulated
  DynamoDB the client uses about 40% less CPU per single-item call and half as much for batch
  writes.
- A new asyncio bridge (ADR 0010) replaces `pyo3-async-runtimes`: Tokio threads never take the
  GIL; results are queued on a per-event-loop port, the loop is woken through a socket it
  watches, and outputs are converted on the loop's thread. In-memory calls run inline (no thread
  hop) and still yield to the event loop once.
- The call pipeline skips work nobody consumes: no `Request`/`Response` objects without
  middleware, no spans or metric attributes while no OpenTelemetry SDK is configured, operation
  records only for collectors, metrics or events, and cached event routes and hook lookups.
- Models: precomputed per-schema plans for validation, encoding, decoding and keys; cheaper
  constraint checks; decoders skip values that are already of the decoded type and convert
  numbers, strings and booleans with builtins; keys given as positional values (`get`,
  `delete`, `batch_get`, `Model.key`) skip the argument matching; collections of plain values
  are encoded by copying; query pages are decoded in one pass, and `first()` closes its page
  iterator at once.
- The in-memory backend reads only the keys a query or scan can return (bounded, lazy ranges),
  stops cloning the items a write replaces, prunes idempotency tokens incrementally, and its
  managed batches no longer spawn tasks (nor do single-chunk batches on AWS). Its expression
  parser no longer copies tokens or placeholder names, and the reserved-word check no longer
  allocates (parsing takes about 40% less time).
- Numbers in canonical form (as DynamoDB returns them) are parsed without copying, and compared
  and measured without allocating.
- Conversions between Python and DynamoDB values: error paths are only formatted on errors,
  attribute names and decimals are shared between the items of a page, and output dict keys are
  interned.

### Fixed

- `Model.copy()` kept the copied `version` (and timestamps), so saving a copy of a versioned
  instance under a new key failed with `VersionConflictError`. Copies now start with empty
  automatic fields, as the documentation promised: saving one writes a new item.
- Shallow copies (`copy.copy()`, pydantic's `model_copy()`) shared their original's persistence
  state, so writing one changed what the other knew about its stored item (updates of unique
  attributes of the original could then fail). Each copy now has its own.
- Updates that change a field a key template uses by arithmetic or a function (`add`,
  `increment`, `set(path + n)`, `set_if_not_exists`, nested paths) wrote the templated attribute
  rendered from the old value, so a leaderboard key such as `SCORE#{score:08d}` kept the old
  score. It is now rendered from the update evaluated on the item as last read.
- The in-memory backend compared binary values by length only when it checked that a reused
  transaction idempotency token belonged to the same request.

## [0.1.0] - 2026-10-13

First release.

### Features

- Models with `Attr[T]` descriptor fields (`dataclass_transform`): codecs for every common
  Python type, constraints, validators, nested documents, enums, sets, TTL, automatic
  `created_at`/`updated_at`/`version` fields, lifecycle hooks; pydantic models with `Annotated`
  markers.
- Repositories: `get`, `batch_get`, `create`, `put`, `save` (optimistic locking), `update` with
  typed actions, `delete`, `batch_put`/`batch_delete`, queries and scans with filters, sort key
  conditions, projections, hydration, parallel segments and opaque cursors.
- Unique attributes enforced with guard items in transactions; specific errors
  (`ItemAlreadyExistsError`, `VersionConflictError`, `UniqueConstraintError`).
- Transactions with model and dict-level operations, condition checks, idempotency tokens,
  per-operation cancellation reasons, and transactional reads.
- Single-table design: entities, key templates, derived index keys, collections.
- Table management: create/ensure tables and indexes, settings, streams, TTL, tags, backups,
  point-in-time recovery; the dict-level table API, PartiQL, typed-JSON conversion.
- Rust core on the AWS SDK for Rust with retries (exponential backoff, full jitter), per-attempt
  timeouts, managed batches, events and metrics; an in-memory DynamoDB with fault injection for
  tests (`dynamoxy.testing`).
- Observability: structured events and logging, hooks, middleware, OpenTelemetry spans and
  metrics, `dynamoxy.collect()` cost accounting, `client.metrics()`.
- `dynamoxy` command line: `guide`, `version`, `events`, `operations`, `reserved`.
