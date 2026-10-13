# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

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
