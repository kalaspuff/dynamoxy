# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

### Fixed

- A parallel scan that stopped early (`scan().segments(n).limit(k)`, `first()`, or a `break`
  out of its iteration) could hang forever, and keep the interpreter from exiting: its segment
  tasks blocked on a full queue while they were cancelled. They now stop at once.
- `limit()` and `first()` read whole pages (up to 1 MB) for models with an entity type or
  unique attributes, since the filter dynamoxy adds for them counted as the caller's filter:
  `orders.query(customer_id="c1").descending().first()` cost 112 read units instead of 0.5.
  Requests now ask for what the limit still needs.
- `page()` ignored `limit()` when the query had a filter, and the page `pages()` cut at the
  limit kept the cursor of the whole page, so an API continuing with it skipped the items after
  the limit. `page()` now evaluates at most `limit` items, and a cut page gets a cursor right
  after its last item.
- Queries of the table (and of local indexes) of a model with unique attributes and no entity
  type failed in DynamoDB with `ValidationException: Filter Expression can only contain
  non-primary key attributes`: the guard-item filter read the partition key. Such queries need
  no filter and no longer send one.
- `where()` on a templated sort key (`orders.query(customer_id="c1").where(Order.sk.begins_with(
  "ORDER#2026"))`) failed in DynamoDB with `Conditions can be of length 1 or 2 only`, because
  dynamoxy had already added `begins_with(sk, "ORDER#")`. A `where()` condition now replaces it.
  `where()` takes one condition, as DynamoDB does: a second one raises `ValueError`.
- Queries of a model with an entity type through a `KEYS_ONLY` or `INCLUDE` global index
  returned nothing in DynamoDB, also with `.hydrate()`: the entity filter read `_type`, which
  the index did not hold. dynamoxy now projects the type attribute into such indexes when it
  creates them, keeps the entries of older indexes without it, and checks the entity of hydrated
  items.
- Model reads of an index that does not hold every field returned instances with defaults
  instead of the stored values (`Counter(count=0)` for a stored 42), and `save()` of one stored
  those defaults. Reads of such a global index now raise `ValueError` before any request unless
  they `.hydrate()` or `.project(...)`; reads of such a local index ask DynamoDB for every
  attribute; `exists()` reads keys only. Filters on attributes a global index does not hold
  raise `ValueError` (they could never match).
- `Model.field.set(None)` stored DynamoDB's `NULL` instead of removing the attribute: on a
  unique field, every later value failed with `ConditionalCheckFailedError`; on an index key,
  DynamoDB refused the update; and `not_exists()` did not match. It now removes the attribute,
  and `Model.field == None` matches a missing attribute or a `NULL` (`!= None`: the others).
  Unique fields that hold a `NULL` (from that bug, or another writer) take new values again.
- `update()` did not render templated attributes that use `updated_at` or `version` again, so
  an index sort key such as `"{status}#{updated_at}"` kept the time of an older write.
- Unique guards of numbers depended on how the number was written (`5.0` and `5` were
  different values), and changing or deleting a `Decimal("5.0")` deleted the wrong guard, so the
  value stayed taken forever. Guards now use DynamoDB's canonical numbers (ADR 0013).
- Key templates rendered `Decimal` values with their exponent and formatted datetimes in their
  own time zone, so an item read back rendered another key and `save()` wrote a duplicate. They
  now render what DynamoDB stores (ADR 0013).
- `datetime_format="epoch_ms"` stored about one timestamp in 160 a millisecond early, and every
  load and save moved it again; `"epoch"` rounded toward zero before 1970. Both are exact now.
- `save()` and `put()` of a loaded instance stored a changed `immutable=True` value, and a
  changed key (or a field a key template uses) wrote a second item next to the first. Both now
  raise `ValueError` before any I/O, also in batch puts.
- `put()` (and `save()` of unversioned models) of a model with unique attributes failed with an
  unexplained `ConditionalCheckFailedError` when another writer had changed the unique values
  since the instance was loaded. It now reads the stored item and writes again.
- Keys given as field values whose names are the key attributes (`devices.update({"device_id":
  uuid}, ...)`) were sent unencoded and failed; batch reads and `hydrate()` dropped items with
  float keys such as `0.1`.
- A `BatchWriter` flush ran `after_save` hooks and marked instances as stored for writes that
  stayed unprocessed, and a flush that failed before its request (a `before_save` hook raised)
  dropped every queued write. Only written instances are finalized now, and what a failed flush
  did not write stays queued. `batch_put()` and `batch_delete()` write a repeated key once (the
  last one wins) instead of failing in DynamoDB, and finalize only what was written.
- A failing `after_*` hook after a committed transaction (or batch) left the other written
  instances unfinalized: a later `save()` of one failed with `VersionConflictError`.
- Smaller ones: `copy()` silently ignored misspelled field names (now `TypeError`);
  `Model.field.increment(0.5)` stored a fraction in an `int` field, and `increment()` of a
  `timedelta` field failed in the core (amounts and arithmetic now use the field's codec);
  subclasses of repository subclasses did not find their model.
- `ensure_tables()` reported nothing to change when an index of the same name had another key,
  or when TTL was enabled on another attribute; it now raises `TypeError`, like for the table's
  key.

### Changed

- [**breaking**] Model reads of global indexes that do not hold every field raise `ValueError`
  unless they `.hydrate()` or `.project(...)` (see above, and
  [ADR 0012](docs/adr/0012-reads-of-partial-indexes.md)). `KEYS_ONLY` and `INCLUDE` indexes that
  dynamoxy creates for models with an entity type now also hold `_type`.
- Stored forms that change for new writes, with migration notes in
  [ADR 0013](docs/adr/0013-stored-forms-read-back-as-written.md): guard keys of non-canonical
  numbers, template keys of non-canonical `Decimal`s and non-UTC formatted datetimes, and epoch
  milliseconds that were a millisecond early.

## [0.2.0] - 2026-10-15

### Features

- Queries and scans take `consistent=True` for strongly consistent reads, like `get()`:
  `repository.query(...)`, `repository.scan()`, `repository.count()`, `table.query(...)`,
  `table.scan()` and the `query()` and `scan()` of collections (`.consistent()` still works). A
  consistent read of a global secondary index of a model raises `ValueError` before any request,
  also with `.consistent()`, instead of failing in DynamoDB with `ValidationError`.
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

- `.hydrate()` fetched the full items of a `.consistent()` query or scan with eventually
  consistent reads, so it could return an older version of an item, or leave out an item written
  just before. It now reads them as consistently as the query.
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

### Features

- **models:** Before_update and after_update lifecycle hooks (#11) (#11)
- **models:** [**breaking**] Model instances save, update and delete themselves through their repository (#12) (#12)
- **query:** Consistent=True for queries and scans (#18) (#18)

### Bug fixes

- Compare binary values when the in-memory backend matches transaction idempotency tokens (#3) (#3)
- **repository:** Render templated attributes from the new value after arithmetic updates (#9) (#9)
- **models:** Copy() makes new instances, and copies keep their own persistence state (#10) (#10)
- **query:** Hydrate() reads full items as consistently as its query (#17) (#17)

### Performance

- Faster in-memory backend, expression parser and numbers (#4) (#4)
- Replace pyo3-async-runtimes with an own asyncio bridge (#5) (#5)
- Skip unused work in the call pipeline (#6) (#6)
- Precomputed model plans, faster repositories and queries (#7) (#7)

### Documentation

- Document how credentials and region are resolved (#14) (#14)
- A skill for pull requests, stacks and publish kits (#15) (#15)
- Document table_prefix for environments sharing an AWS account (#16) (#16)

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
