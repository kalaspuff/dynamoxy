# Architecture

dynamoxy has three layers: a Rust core that speaks DynamoDB (`crates/dynamoxy-core`), a thin PyO3
binding (`crates/dynamoxy-py`, built into `dynamoxy._core`) and the Python API
(`python/dynamoxy`). The core owns protocol, timing and I/O; Python owns application objects:
models, validation, hooks, middleware, logging and OpenTelemetry. Read this document before any
non-trivial change. It defines the layers, the pipelines and the invariants every change must
preserve. Decisions behind it are recorded in [docs/adr](adr/README.md).

## Layers

```text
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ Python API  python/dynamoxy                                                              │
│                                                                                          │
│  models.py  attributes.py  _pydantic.py   declare models (Attr descriptors / pydantic),  │
│                                           Model.create/get/query(client, ...), instances │
│  schema.py  codecs.py                     ModelSchema: fields, keys, templates, indexes,  │
│                                           encode/decode/validate (per model, cached)      │
│  repository.py  transactions.py  batch.py  plan writes (_WritePlan), run hooks, guards    │
│  query.py  collection.py  table.py        reads, cursors, dict-level API, table admin    │
│  constraints.py                           unique guard items                             │
│  expressions.py                           typed paths → expression strings + placeholders │
│  client.py  middleware.py                 Client: lifecycle, call pipeline, factories    │
│  events.py  telemetry.py  results.py      events/logging/hooks, OpenTelemetry, collect() │
│  errors.py  testing.py  __main__.py       error tree, test helpers, CLI                  │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ Binding  crates/dynamoxy-py → dynamoxy._core (abi3, CPython ≥ 3.12)                      │
│                                                                                          │
│  runtime.rs   one Tokio runtime per process, asyncio bridge, fork reset, atexit shutdown  │
│  client.rs    CoreClient (one async method per DynamoDB operation), MemoryStore           │
│  convert.rs   Python values ⇄ AttributeValue, outputs → dicts; errors.rs: exception tree  │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ Core  crates/dynamoxy-core (no Python)                                                   │
│                                                                                          │
│  client.rs    validated config, retries + backoff, attempt timeouts, events, metrics,     │
│               managed batches (chunking, concurrency, unprocessed rounds), table waiters  │
│  backend/     Backend trait (port) with two adapters:                                     │
│    aws/       AWS SDK for Rust (SDK retries off); errors.rs classifies every SDK error    │
│    memory/    an in-memory DynamoDB: items, indexes, expressions, limits, transactions,   │
│               PartiQL subset, TTL, backups, fault injection                               │
│  expr/        DynamoDB expression language: parser, evaluator, update, projection,        │
│               reserved words                                                              │
│  value/ types/ AttributeValue/Number/item sizes; operation inputs and outputs             │
│  events.rs metrics.rs retry.rs config.rs error.rs                                         │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

The core can be used and tested from Rust alone. The binding contains conversions and the
asyncio bridge only: no logic.

## Pipelines

### A model write (`repository.create/put/save/update/delete`)

The instance methods (`user.save()`, `user.update(...)`, `user.add(...)`, `user.increment(...)`,
`user.delete()`, ...) call these on the repository the instance is bound to (see "Bound
instances" below); `add`/`increment`/`decrement` only build the update action.
`User.create(client, ...)` constructs the instance and calls `client.model(User).create(...)`
([ADR 0017](adr/0017-models-create-items-with-an-explicit-client.md)); `User.get(client, ...)`
and `User.query(client, ...)` call `client.model(User).get(...)` and `.query(...)`
([ADR 0018](adr/0018-models-read-items-with-an-explicit-client.md)). The client builds one
plain repository per model, on first use, and returns it afterwards.

1. **Python, `repository.py`**: validate the instance (`ModelSchema.validate`: codecs,
   constraints, validators), run `before_*` hooks (model methods, then client hooks), compute
   automatic fields (`created_at`, `updated_at`, `version`) and derived (templated) attributes.
   A write of a loaded instance refuses a changed key or immutable value, and keeps the
   attributes of the stored item that no field declares. Updates render derived attributes from
   the values they set (`updated_at` included), or, for arithmetic, functions and the version,
   from the update evaluated on the item as last read (`_core._apply_update`).
2. **Python, `_plan_*`**: build a `_WritePlan`: one operation (put, update or delete with its
   condition: key existence for `create`, version for `save`, the caller's condition) or, for
   models with unique attributes, a transaction with guard puts and deletes
   (`constraints.py`). Unique writes of instances that were not loaded first read the stored
   item (one consistent `GetItem`) to learn the old values. A write outside a transaction whose
   plan a failed condition shows to be outdated (guards of the values an instance was loaded
   with, and no version or condition of its own; or a read that found no item, and one is
   stored now) reads the item and is planned once more (`_WritePlan.outdated`).
3. **Python, `expressions.py`**: render conditions and update actions to expression strings
   with `#aN`/`:vN` placeholders. No user value ever appears in an expression string.
4. **Python, `client.call`**: middleware chain (skipped when there is none) → OpenTelemetry span
   (only when an SDK records spans) → native call.
5. **Binding**: convert parameters to core types, run the future on the Tokio runtime (in-memory
   clients poll it once on the calling thread first); the result is converted to Python on the
   event loop's thread (see the concurrency model).
6. **Core, `client.rs`**: validate, send through the backend with a per-attempt timeout; retry
   retryable errors (throttling, 5xx, transaction conflicts, timeouts) with exponential backoff
   and full jitter; emit events; record metrics; return the output with `OperationStats`. After
   an attempt that may have been applied (a timeout, a lost connection, a 5xx), only writes that
   apply once even when sent twice are sent again, and an answer that attempt may have caused
   (a failed condition, old values), or no answer when the attempts run out, is `OutcomeUnknown`
   (`WriteOutcomeUnknownError`). `close()` refuses new calls; calls in progress finish their
   retries.
7. **Backend**: AWS SDK call (errors classified in `aws/errors.rs`) or the in-memory backend.
8. **Python**: map failures to specific errors (`explain_failure`: `ItemAlreadyExistsError`,
   `VersionConflictError`, `UniqueConstraintError`, `ItemNotFoundError`), finalize the
   instance (assign the returned item, record it and the repository in its `ModelState`), run
   `after_*` hooks, deliver buffered events, record the `OperationRecord` (collectors,
   OpenTelemetry metrics).

### Bound instances

Every instance a repository decodes or writes keeps a `ModelState` in its `__dict__`: `stored`,
the item as last read or written (used for optimistic locking, unique guard transitions and
derived attributes), and `repository`, the repository that did it. Both are set together, by
`_decode` for reads and by `_apply`/`_assign` for writes ([ADR 0011](adr/0011-instances-remember-their-repository.md)).
Transactions and batch writers write an instance through its repository when it belongs to
their client. Model classes hold no client; instances nobody read or wrote are unbound, and
pickles drop the repository.

### A transaction (`client.transaction()`)

Operations are registered without I/O. `commit()` plans each one with the same `_plan_*`
functions as the repository (so hooks, validation, guards and versions behave identically),
checks the 100-operation limit, and sends one `TransactWriteItems` with an idempotency token
(reused by the core's retries). On cancellation, each reason is mapped back to the operation
that produced it and explained (`cancellation_reasons`, `errors`). On success, updated instances
are refreshed by evaluating the update on the item as loaded with the core's evaluator
(`_core._apply_update`); no extra read.

### A query (`repository.query(...)`)

1. **Python**: resolve the index and render key values through templates
   (`ModelSchema.index_key`); add `begins_with` for templated sort keys with a constant prefix
   (replaced by a `where()` condition, since DynamoDB takes one sort key condition), and an
   entity filter (`_type`) or guard-item filter where needed (`repository._reader_options`).
   Such filters are kept apart from the caller's filters: they rarely remove items, so a limit
   still bounds each request. A guard-item filter is left out where DynamoDB refuses one, on the
   key attributes of what a query reads.
2. Each page is one `Query` call (`Limit` = `page_size`, or what a `limit` still needs);
   `LastEvaluatedKey` becomes an opaque cursor (`query.encode_cursor`: base64url JSON of typed
   values), and a page cut at the limit gets the key of its last item. Items are decoded into
   model instances (`after_load` hooks run), or hydrated with `BatchGetItem`: entries of a
   global index that does not hold every field are never decoded into instances, and reads of
   such a local index ask for every attribute (`Select=ALL_ATTRIBUTES`).

### Events

The core records events in a bounded buffer (`events.rs`) only for kinds the Python layer
enabled (`set_event_filter`: what logging and hooks want). Python drains the buffer
synchronously after every call and in the event pump task (which waits for a signal, then
drains), and routes events to loggers and hooks (`events.py`). Rust threads never call Python.

## Invariants

These are relied upon across layers; tests cover each of them.

1. **Fail fast before I/O.** Model validation, key rendering, expression building, limits
   (100 transaction operations, 25/100 batch requests, 100 `IN` values) and option combinations
   are checked locally, with messages that say what to change.
2. **No hidden I/O on import or construction.** Importing does nothing; a `Client` connects on
   first use or in `async with`. `pydantic` and `opentelemetry` are imported only when used.
3. **Everything is bounded.** The event buffer (`event_buffer`), batch concurrency
   (`batch_concurrency`), unprocessed-item rounds (`batch_max_rounds`) and retries
   (`RetryPolicy`) have limits. Overflow and exhaustion are reported (`events.dropped`,
   `batch.incomplete`, `request.gave_up`, `UnprocessedItemsError`), never silent.
4. **Nothing is silently dropped.** Every batch item ends written or reported in
   `UnprocessedItemsError.unprocessed`; every failed call raises with its attempts and duration;
   every transaction cancellation explains each operation. Retries never apply a write twice,
   and never report a failure that an earlier attempt of the same call may have caused.
5. **Values never appear in expressions.** All user values travel as expression attribute
   values and all names as placeholders, so reserved words and injection are non-issues.
6. **Unique guards stay consistent.** Every write that sets, changes or removes a unique value,
   and every delete, updates the item and its guard items in one transaction. Batch writes of
   such models are refused.
7. **Optimistic locking is checked on every write of a loaded versioned instance** (`save`,
   `update`, `delete`, in repositories and transactions alike).
8. **Both backends behave the same.** The in-memory backend models DynamoDB's semantics, limits,
   error codes and capacity; integration tests run every scenario on both it and MiniStack
   (Python) and the Rust parity scenarios run on both (`tests/emulator.rs`).
9. **Events are cheap when unused.** A disabled kind costs one atomic load in Rust; Python
   builds an event only when logging or a hook wants it (`EventDispatcher.wants`).
10. **Fork safety.** One Tokio runtime per process; a forked child gets a fresh runtime, and
    clients created before the fork refuse to work in the child.
11. **Clean interpreter exit.** An `atexit` hook stops the runtime before finalization; later
    awaitables raise `ShutdownError` (`tests/unit/test_lifecycle.py`).
12. **Instances know where they are stored.** Every read and write through a repository
    records the stored item and the repository on the instance; instance methods, transactions
    and batch writers write through that repository. Model classes never hold a client.
13. **Stored formats are API.** Codecs (`codecs.py`), guard item keys (`constraints.py`), the
    entity type attribute (`_type`), write ids (`_wid`) and cursors are formats: changing one is a breaking change
    that needs an ADR, a migration path and a changelog entry.
14. **Secrets and payloads are never logged.** Events carry operation names, table names,
    counts, codes and durations; `Credentials` hide secrets in `repr`; spans record expressions
    only on request and never values. Error messages name models, fields and types, never keys
    or values: errors carry those as attributes (`.key`, `.value`, `.item`, `FieldError.value`).

## Concurrency model

- **One Tokio multi-thread runtime per process** (`runtime.rs`), created lazily, reset after
  `fork()`, shut down at exit. Every awaitable from `_core` runs there and resolves on the asyncio
  loop that awaited it ([ADR 0010](adr/0010-asyncio-bridge.md)).
- **The asyncio bridge** (`runtime.rs`): runtime threads never take the GIL. A finished call's
  outcome (a `Deferred` that builds the Python result) is queued on the `Port` of its event loop;
  the first one after a drain writes a byte to a socket pair the loop watches (`add_reader`),
  and the loop's thread builds every queued result and resolves the futures. Loops without
  `add_reader` are woken with `call_soon_threadsafe` from a blocking thread. In-memory clients
  poll each call once on the calling thread (without the GIL); a call that finishes there needs
  no thread at all, and its awaitable (`Ready`) still yields to the loop once, like real I/O.
  Cancelling an awaitable aborts its task.
- **The core client is `Send + Sync`**: the backend is shared behind `Arc`, metrics are atomics,
  the event buffer is a `parking_lot::Mutex<VecDeque>` held only for push/drain (never across
  `.await`), with a `tokio::sync::Notify` for waiters.
- **Managed batches** run up to `batch_concurrency` requests at once (`JoinSet`), and send
  unprocessed items again for up to `batch_max_rounds` rounds with backoff. A batch of one chunk,
  and every batch of the in-memory backend (which applies one request at a time anyway), sends its
  chunks from the calling task.
- **The in-memory backend** keeps tables behind a lock; each operation (including a whole
  transaction) is applied atomically.
- **Python**: no threads. Each `Client` has a per-event-loop connect lock (`LoopLocal`), so it
  can be used from several loops (tests) in one process. Async hooks run as tasks; `close()`
  drains them.

## Extension points

| Extension | How | Where |
| --- | --- | --- |
| Field storage | `codec=` / `UseCodec(...)`, any object with the `Codec` interface | `codecs.py` |
| Validation | field constraints, `validators=`, `@dynamoxy.validator`, pydantic | `attributes.py`, `models.py` |
| Lifecycle hooks | model methods (`@before_save`, `@after_update`, ...) or `client.hook(point, Model)` | `models.py`, `middleware.py` |
| Middleware | `client.use(async def mw(request, call_next))` | `middleware.py`, `client.py` |
| Event hooks | `client.on("kind.*", hook)` | `events.py` |
| Logging | `LoggingConfig`, `dynamoxy.*` loggers, `record.dynamoxy` | `events.py` |
| Tracing and metrics | `Telemetry(tracer_provider=..., meter_provider=...)` | `telemetry.py` |
| Domain repositories | subclass `Repository[Model]` | `repository.py` |
| Test doubles | `MemoryStore`, `testing.inject_fault`, `purge_expired`, `capture_events` | `testing.py` |

## Where things live

| Concern | Rust | Python |
| --- | --- | --- |
| Errors | `error.rs`, `backend/aws/errors.rs` | `errors.py` (re-exports `_core` errors, adds model errors) |
| Configuration | `config.rs` (serde, `deny_unknown_fields`) | `Client`, `RetryPolicy`, `Telemetry`, `LoggingConfig` with `to_core()` |
| Events | `events.rs` (`event_kinds!`) | `events.py` (`EventKind`, same order, plus Python kinds) |
| Metrics | `metrics.rs` | `results.MetricsSnapshot`, `telemetry.py` |
| Expressions | `expr/` (parse, validate, evaluate) | `expressions.py` (build) |
| Values | `value/` | `convert.rs` (binding), `codecs.py` |
| Tests | `#[cfg(test)]` modules, `tests/emulator.rs` | `tests/unit`, `tests/integration` (both backends), `tests/e2e` |
