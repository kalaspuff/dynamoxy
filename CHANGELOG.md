# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

### Performance

- A new asyncio bridge (ADR 0010) replaces `pyo3-async-runtimes`: Tokio threads never take the
  GIL; results are queued on a per-event-loop port, the loop is woken through a socket it
  watches, and outputs are converted on the loop's thread. In-memory calls run inline (no thread
  hop) and still yield to the event loop once.
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
