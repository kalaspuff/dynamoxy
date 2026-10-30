# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

### Changed

- [**breaking**] `create`, `get` and `query` are model methods now:
  `await User.create(client, user_id="u1", ...)` builds and writes a new item, and
  `await User.get(client, "u1")` and `User.query(client, ...)` read like `client.model(User)`
  ([ADR 0017](docs/adr/0017-models-create-items-with-an-explicit-client.md),
  [ADR 0018](docs/adr/0018-models-read-items-with-an-explicit-client.md)). Fields can no longer
  use these names: rename the field and keep the stored name with `attribute_name="query"`.
- [**breaking**] Pydantic models refuse fields named like the model API (`key`, `to_item`,
  `save`, `get`, ...) with a `TypeError`, as native models do. Pydantic only warned, and the
  field hid the method. Rename such a field and keep its stored name with
  `AttributeName(...)` (and its name in pydantic's input and output with an alias).
- `client.model(User)` returns the same repository on every call (built on first use), so the
  model's classmethods and inline `client.model(User).get(...)` calls cost a dictionary lookup.

### Deprecated

- `client.repository(User)` is now `client.model(User)`, which returns the same `Repository`: the
  old name read like the repository pattern of an application. `client.repository()` still
  works, warns with a `DeprecationWarning` (type checkers flag it too) and will be removed in
  0.5. `dynamoxy.Repository` keeps its name, as the base class of domain repositories.

### Fixed

- Writes through a loaded instance could change another item. `update()`, `delete()`,
  transactions and batch deletes of an instance whose key field changed since it was loaded
  (`order.customer_id = "c2"`) wrote the item of the new key, where `save()` and `put()` raise
  `ValueError`; they now raise it too, and `refresh()` reloads the item the instance was loaded
  from. Updates of a field that a key template uses (`Customer.customer_id.set("c2")` with
  `pk="CUSTOMER#{customer_id}"`) were accepted and left the item's key and fields disagreeing,
  so the instance's later writes went to another item: they now raise `ValueError` like
  updates of the key itself.
- Instances shared a mutable default given with `default=` (`field(default=[])`, a document
  instance, or a pydantic model's default model): appending to one instance's list, or changing
  one instance's document, changed it for every instance created or read later. Each instance
  now gets its own copy, as in pydantic.
- Projected reads without `limit()` lost the key attributes that dynamoxy needs on the client.
  `where()` on a templated sort key of a model without an entity type
  (`players.query(index=Player.by_rank, board="global").where(Player.rank >= "SCORE#0050")`)
  returned no rows with `.project(...)`, because the sort key prefix that `where()` replaced is
  checked on each item; and `.project(...).hydrate()` failed with `ValidationException`, because
  the entries lacked the table key. Such reads now ask for the key attributes too, and the
  dicts still hold only what was projected.
- Deeply nested input crashed the whole process with a stack overflow (`SIGSEGV`) instead of
  raising an error: values with thousands of nested lists or maps, or that contain themselves
  (`item["self"] = item`), in items, expression values, `to_wire()` and PartiQL parameters; a
  crafted pagination cursor passed to `after()`; and PartiQL statements or expressions with a
  few thousand nested parentheses, which crashed AWS clients too, since dynamoxy parses
  statements and update expressions to decide whether a retry is safe. Values nested more than
  32 levels deep (DynamoDB's limit) now raise `ValidationError` before any request, and the
  in-memory DynamoDB refuses expressions and statements nested more than 100 levels deep, and
  statements longer than DynamoDB's 8,192 characters, with a `ValidationException`.
- A character outside ASCII in an expression (`größe > :v`, or a non-breaking space pasted into
  `a = :v`) panicked the expression lexer: calls raised an internal error, and
  `dynamoxy._core._apply_update()` a `PanicException`, which `except Exception` does not catch.
  AWS clients failed the same way before sending an `UpdateItem`, whose expression dynamoxy
  parses to decide whether a retry is safe. Such expressions are now syntax errors
  (`ValidationException`), and AWS clients send them to DynamoDB.
- A write whose retries ran out after an attempt that may have been applied raised the last
  attempt's error, a `ThrottlingError` or `InternalServerError` that says it is retryable, so
  the write ids never settled it: `create()` raised `ThrottlingError` for an item it had stored,
  and the application's retry then raised `ItemAlreadyExistsError`; a transaction that was
  applied raised `TransactionInProgressError`, and repeating it applied it twice. A 5xx on the
  last attempt of an increment did the same, where one on an earlier attempt raised
  `WriteOutcomeUnknownError`. Such writes now raise `WriteOutcomeUnknownError` (model writes
  settle it by their write id), also with a single attempt. Reads still raise the last error.
- `close()` refused the retries of calls in progress, so a write whose first attempt may have
  been applied raised `ShutdownError`, as if nothing had happened. Calls in progress now finish,
  retries included, as the core documented; new calls are refused.
- Items stored before their model had a `version()` field could never be saved: `save()` of such
  a loaded instance (version `None`) required that the item did not exist, like a new instance,
  and raised an unexplained `ConditionalCheckFailedError`, also in transactions. It now requires
  that the item still has no version, stores version 1, and raises `VersionConflictError` when
  another writer saved or deleted the item meanwhile.
- A member of a `(str, Enum)` or `(int, Enum)` (the mixins that predate `StrEnum` and `IntEnum`)
  in a `str`, `int` or `Literal` field was stored as its value (`"book"`), but keys, templates
  and unique guards rendered its `str()` (`"Kind.BOOK"`): the item got the key
  `KIND#Kind.BOOK#s1`, so reads by the value missed it and `save()` of the loaded instance
  raised `ValueError` (key changed), and a unique value written as `"book"` was not taken. Such
  fields now hold the plain value.
- `first()`, `exists()`, `limit()` and the iteration of reads with an entity type, unique
  attributes or a `where()` on a templated sort key raised `OverflowError` once about 16 pages
  in a row held only items that dynamoxy's own filters remove (16 MB of other entities, in a
  shared table): each such page made the next request ask for four times as many items, without
  a bound. Requests now ask for at most 1,048,576 items, more than a 1 MB page holds.
- dynamoxy's errors could not cross process boundaries. The native ones (`ValidationError`,
  `ConditionalCheckFailedError`, `ThrottlingError`, ...) named `_core` as their module, so
  pickling one failed with `PicklingError`: an error raised in a `ProcessPoolExecutor` worker,
  multiprocessing or a task queue reached the caller as a `PicklingError` (or broke the pool),
  and `except dynamoxy.ConditionalCheckFailedError` never matched. `UnprocessedItemsError` could
  not be unpickled. They now name `dynamoxy._core` and pickle with their attributes.

### Security


### Security

- The native extension bundles rustls 0.23.46 and rustls-webpki 0.103.16. rustls 0.23.46 fixes
  GHSA-h2v8-394m-p7vv, GHSA-c8m5-c952-6cm4, GHSA-w44r-93j7-qv28 and GHSA-vp88-798v-h67f. The
  extension no longer contains the AWS SDK's legacy HTTP client, which dynamoxy never used, or
  its rustls 0.21 and rustls-webpki 0.101, which get no fixes for these advisories or for
  GHSA-82j2-j2ch-gfr8, GHSA-xgp8-3hg3-c2mh and GHSA-965h-392x-2mh5.

## [0.3.0] - 2026-10-17

### Features

- The in-memory DynamoDB runs PartiQL `SELECT ... ORDER BY` with DynamoDB's rules and
  messages: `ORDER BY` takes the partition and sort key of the table or index (`ASC`, `DESC`),
  the `WHERE` clause must name the partition key values, and with several of them the partition
  key must be ordered too. A `SELECT` whose `WHERE` clause names the partition key values
  (`IN`, `OR`) reads only those partitions, so `Limit` and consumed capacity match DynamoDB.

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
- Error messages included keys, unique values and the values that failed validation or
  decoding (`User {'user_id': 'ada@example.com'} does not exist`, pydantic's `input_value=...`),
  which may be personal data and end up in logs. Messages now name models, fields and types
  only, and point to the attributes that hold the values: `User does not exist (see .key)`. The
  `ConditionalCheckFailedError` of a model write has `.key` too, and `repr()` of `FieldError`
  and `CancellationReason` leaves out the value and the item.

### Changed

- [**breaking**] Model reads of global indexes that do not hold every field raise `ValueError`
  unless they `.hydrate()` or `.project(...)` (see above, and
  [ADR 0012](docs/adr/0012-reads-of-partial-indexes.md)). `KEYS_ONLY` and `INCLUDE` indexes that
  dynamoxy creates for models with an entity type now also hold `_type`.
- Stored forms that change for new writes, with migration notes in
  [ADR 0013](docs/adr/0013-stored-forms-read-back-as-written.md): guard keys of non-canonical
  numbers, template keys of non-canonical `Decimal`s and non-UTC formatted datetimes, and epoch
  milliseconds that were a millisecond early.
- [**breaking**] Writes of loaded instances (`save()`, `put()`, batch puts) keep the attributes
  of the stored item that their model does not declare, so an older version of an application
  no longer erases fields a newer one wrote during a rolling deploy
  ([ADR 0014](docs/adr/0014-writes-keep-undeclared-attributes.md)). Remove a retired attribute
  with `update(item, F("name").remove())`.
- [**breaking**] Reading is exact ([ADR 0015](docs/adr/0015-stored-values-decode-exactly.md)): a
  stored value of another type, or one the field's type cannot hold, raises `ItemDecodeError`
  instead of being converted. A `2.5` read as `2` in an `int` field (and the next `save()`
  stored the `2`), an `int` sort key with a fraction pointed at another item (`delete(instance)`
  deleted that one), `"false"` read as `True` in a `bool` field, a string as the set of its
  characters, and unions (`int | str`) returned types they do not name. `update()` and
  `delete()` say when the write was applied but the item it returned does not decode, and
  `timedelta` fields decode exactly. Find such items with a scan of `repository.table` and
  rewrite them, or declare the stored types (`Attr[int | str]`) while both are stored.
- [**breaking**] A write retried after an attempt that may have been applied (a timeout, a lost
  connection, a 5xx) no longer reports a failure it may have caused itself, and is no longer
  applied twice. A failed condition after such an attempt (`create()` of the item the first
  attempt wrote, a version check, a PartiQL `INSERT`) and old values it may have shaped raise
  `WriteOutcomeUnknownError` (a `TransportError`, with `.item`) instead of
  `ItemAlreadyExistsError`, `VersionConflictError` or `ConditionalCheckFailedError`. Updates that
  would apply twice (`add()` and `increment()` of numbers, `append()`, arithmetic in PartiQL)
  are not sent again and raise it too; before, they were applied twice. Transactions, and writes
  that apply once even when sent twice, are still sent again. Each such call emits
  `request.outcome_unknown`, and `testing.inject_fault(..., lose_response=True)` reproduces it.
- Model writes recognise their own earlier attempt: each write that runs alone (not in a
  transaction or batch) stores a random id in the item's `_wid` attribute
  ([ADR 0016](docs/adr/0016-writes-store-a-write-id.md)). When the outcome is unknown and the
  stored item holds the write's id, `create()`, `save()`, `update()` and `increment()` succeed,
  applied once, and emit `request.outcome_settled`. Items gain the `_wid` attribute;
  `write_ids=False` (a class keyword) stores none.

### Features

- **memory:** PartiQL SELECT with ORDER BY, by DynamoDB's rules (#42) (#42)
- **repository:** Model writes recognise their own earlier attempt by a write id (#44) (#44)

### Bug fixes

- **query:** Parallel scans that stop early no longer hang (#21) (#21)
- **query:** Limit() reads only what it needs and cursors of cut pages skip nothing (#22) (#22)
- **repository:** Table queries of unique models and where() on templated sort keys (#23) (#23)
- **errors:** Error messages leave out keys and stored values (#40) (#40)
- **models:** [**breaking**] Stored values decode exactly, or raise ItemDecodeError (#41) (#41)
- **client:** [**breaking**] Retried writes never apply twice or fail on their own earlier attempt (#43) (#43)

### Documentation

- Highlights, design and performance sections in the README (#37) (#37)
- A benchmark script reproduces the README's performance numbers (#38) (#38)
- A shorter README that is easier to read (#46) (#46)
- Show the dynamoxy logo in the README heading (#47) (#47)

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
