# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor versions may contain breaking changes).
Entries are generated from conventional commits with [git-cliff](https://git-cliff.org)
(`cliff.toml`) by the release workflow; edit the generated text if it needs polishing.

## [Unreleased]

### Changed

- Building dynamoxy from source (the sdist, or a checkout) needs Rust 1.99 or newer, up from 1.94.

### Fixed

- An error with an attribute that does not pickle could not cross process boundaries: the
  `ItemNotFoundError` of a model class defined inside a function (its `.model`), or a
  `UniqueConstraintError` whose `.value` does not pickle, reached the caller of a
  `ProcessPoolExecutor` worker, multiprocessing or a task queue as a `PicklingError`. Pickles of
  dynamoxy's errors now leave such values out (`None`), as they already did for
  `FieldError.value`; copies keep them.
- An application's subclass of a dynamoxy error that takes more arguments than the message could
  not be unpickled (`TypeError: missing 1 required positional argument`), so it never reached the
  caller of a worker process or task queue. Unpickling and copying no longer call an error's
  constructor; they restore its attributes. A value that does not pickle, or does not unpickle,
  is now left out at the finest level that works (one value of a `.key` or `.item` dict, one field
  of a dataclass) instead of as a whole attribute.
- With `RetryPolicy(retry_timeouts=False)`, a write whose attempt timed out or lost its
  connection raised a `TransportError` with `.retryable == True`, although it may have been
  applied: a caller that trusted `.retryable` applied an `increment()` twice. Such a write now
  raises `WriteOutcomeUnknownError` (a `TransportError` that is not retryable) and emits
  `request.outcome_unknown`, and model writes settle it by their write id like any other unknown
  outcome. Reads keep raising a retryable `TransportError`.
- A write that removed or changed a unique value deleted the value's guard item even when another
  item owned it, so when two items held one value (items stored before the field became
  `unique=True`, or written by other tools), deleting or changing one of them silently removed
  the other's protection and a third item could take the value. Guard deletes now require that
  the guard is missing or owned by the written item; otherwise the write raises
  `UniqueConstraintError` ("the stored value's guard item belongs to another item", the guard in
  `.item`) and writes nothing.

## [0.4.0] - 2026-10-31

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
- [**breaking**] `None` and empty sets read back as written, not as the field's default
  ([ADR 0019](docs/adr/0019-missing-attributes-read-as-none-or-empty-sets.md)). dynamoxy stores
  both as no attribute, and read a missing attribute as the default: a
  `nickname: Attr[str | None] = field(default="anon")` written as `None` came back as `"anon"`,
  and a set field whose default is `{"member"}`, written empty or emptied by deleting its last
  element, came back as `{"member"}`, so a revoked role was granted again and the next `save()`
  stored it (in pydantic models and documents too). A missing attribute now reads as `None` in
  an `X | None` field and as an empty set in a set field, also in items written before the
  field existed: backfill the default (`await item.update(Model.field.set_if_not_exists(...))`)
  where they need it. A `set[X] | None` field reads an empty set back as `None`. `set(set())` of
  a model field removes the attribute like `set(None)` (it stored a `NULL`, on which later
  `add()` failed), `remove()` and `set(set())` of a set field empty it (they raised
  `ValidationError`), and `== set()` and `!= set()` compare like `== None` and `!= None`.
  Upserts by key no longer give `X | None` and set fields their defaults, which
  `if_not_exists` wrote over an existing item's `None` or empty set: an item they create reads
  `None` or an empty set there unless the actions set it.
- [**breaking**] `unique=True` on a set, list, map or document field is a `TypeError` when the
  model is used: the guard key was the value's text, which has no canonical form for
  collections (a set's depends on the hash seed of the process), so processes computed
  different guards for the same value. A collection stored in a unique `Any` field raises
  `ValueError` before any write. Drop `unique=True` from such fields, or make the value a
  string (`",".join(sorted(tags))`) in a field of its own.
- [**breaking**] `version()` fields hold a random token (`Attr[str]`), new on every write, instead
  of a counter ([ADR 0022](docs/adr/0022-versions-are-random-tokens.md)). A counter started
  again at 1 on a `put()` of an instance that was not loaded, and on creating an item again after
  it was deleted, so an instance loaded before at version 1 passed its check again and
  overwrote both writes (#84); a token never repeats, and such an instance raises
  `VersionConflictError`. Declare version fields `Attr[str]` (`Annotated[str | None, Version()]`
  in pydantic models; other types raise `TypeError`), and compare versions for equality only;
  stored numeric versions are not read. Updates set the new token instead of adding 1, and the
  operations of a transaction with an idempotency token derive their tokens from it, so that the
  transaction planned again with the token is the same request.

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
  that the item still has no version, stores its first version, and raises `VersionConflictError` when
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
  not be unpickled. They now name `dynamoxy.errors`, where they are documented, and pickle with
  their attributes; tracebacks show `dynamoxy.errors.AccessDeniedError` like those of the
  Python-defined errors (`dynamoxy.errors.ItemAlreadyExistsError`), not `_core.AccessDeniedError`.
- A field with a codec of its own refused `None` although its annotation allowed it:
  `Attr[datetime | None] = field(default=None, codec=DateTimeCodec("iso_z"))` and
  `Attr[dict[str, Any] | None] = field(default=None, codec=JsonCodec())` failed on construction
  with their own default ("must not be None"), and so did `Literal["on", "off", None]`; a
  pydantic `Annotated[datetime | None, UseCodec(...)] = None` validated, but `create()` and
  `save()` raised "is required". Such fields are optional now, as `X | None` fields are: `None`
  is not stored (the codec never sees it), a missing attribute reads as `None`, and `remove()`
  works. A key field annotated `X | None` raises `TypeError` with a codec too ("key attributes
  cannot be optional"), as it did without one.
- The in-memory DynamoDB ran PartiQL transactions (`execute_transaction()`) that named an item
  more than once, and lost writes: each statement saw the item as it was before the transaction,
  so of `UPDATE ... SET a = 1` and `UPDATE ... SET b = 2` on one item only the second took
  effect, and an `INSERT` and a `DELETE` of one item left it stored. Such transactions, of reads
  too, now raise `ValidationError` ("Transaction request cannot include multiple operations on
  one item"), as on DynamoDB and as `TransactWriteItems` and `TransactGetItems` do.
- An update that set several elements past the end of a list, not in the order of their indexes
  (`Player.history[4].set("b"), Player.history[3].set("a")`, or `SET l[4] = :b, l[3] = :a`),
  appended them in the order of the actions: on `["x", "y", "z"]` the in-memory backend stored
  `["x", "y", "z", "a"]` and lost `b`, where DynamoDB sets list elements in order of their index
  and stores `["x", "y", "z", "a", "b"]`. Instances updated in a transaction are refreshed with
  the same evaluator, so on AWS too they could differ from the stored item. List elements are
  now set in order of their index.
- The in-memory DynamoDB answered reads of a local index from its entries alone when a
  projection named attributes the index does not hold, where DynamoDB reads them from the
  table: `client.model(Note).query(index="by_kind", owner="ada").project("body", "kind")`
  returned `[{"kind": "todo"}]`, without the body, and PartiQL `SELECT`s of such attributes
  did the same. Reads that did fetch the items (`Select=ALL_ATTRIBUTES`, which model reads of
  such indexes send) counted only the entries towards the 1 MB of a page and the read units:
  ten items of 300 KB came back in one page for 1 read unit. Such reads now fetch the items,
  count each, rounded up to 4 KB, towards the page, and charge a read of each to the table, as
  DynamoDB does.
- Reads by key and index queries skipped the field's validators and constraints, which writes
  run, so a validator that normalizes made items unreachable: with
  `email = field(unique=True, validators=(str.lower,))` and `GlobalIndex(partition_key=email)`,
  `query(index=User.by_email, email="Ada@Example.com")` returned nothing for the item stored with
  `ada@example.com`, and `get("Ada")` missed the item whose partition key a validator stored as
  `"ada"`. Key values given to `get()`, `exists()`, `batch_get()`, `query()` (table and index
  keys), collection queries, `Model.key()`, `Get()` and writes by key are now validated like
  stored values. A value the field refuses raises `ValueError` before any request, where such
  reads found nothing (an item whose key predates the rule is still read with a stored key:
  `batch_get([{"user_id": ...}])`), and so does `None`, which templated keys sent to DynamoDB.
  Stored keys, which `batch_get()`, `update()`, `delete()` and transactions take, are used as
  they are, and conditions compare with the value as given. Pydantic models run the field's
  pydantic validators on them (see below).
- Fields that a subclass inherits encoded expression values in the `datetime_format` of the
  class that declared them, while the subclass stored them in its own. With an abstract
  `Timestamped` declaring `seen_at` and `Article(Timestamped, datetime_format="iso_z")`,
  `filter(Article.seen_at == at)` compared `...+00:00` with the stored `...Z` and found nothing,
  conditions on writes failed, and `update(Article.seen_at.set(at))` stored `...+00:00` among
  `...Z` values (text among numbers with an epoch format). Transactions took the declaring class
  as the model of updates by key (`TypeError: Timestamped is not stored in a table`), and
  assigning or comparing such a field raised `TypeError` when the base class's key template
  names a field of the subclass. A subclass now has attributes of its own for the fields it
  inherits; those of the base class keep its format. So `add()`, `increment()` and `decrement()`
  of an instance refuse the attribute of a sibling class, as they did for pydantic models: with
  `Page` and `Post` both inheriting `views`, `post.increment(Page.views)` raises `TypeError`,
  which now names the attribute to use (`Post.views`).
- Deletes of models with unique attributes ignored their `condition=` when no item was stored:
  `delete(key, condition=...)` returned `None`, and `tx.delete(...)` was left out of the
  transaction, so a delete used as the guard of a transaction let the rest commit. As for other
  models, the condition now applies to the missing item, as in DynamoDB: one that does not hold
  for it raises `ConditionalCheckFailedError` or cancels the transaction. A delete of a missing
  item, with or without a condition, now also runs its `after_delete` hooks and leaves the
  instance unloaded, as for other models.
- Deletes and updates of loaded instances of unversioned models with unique attributes raised an
  unexplained `ConditionalCheckFailedError` ("the condition of delete failed", without a
  condition) when another writer had changed a unique value since the instance was loaded, and
  `delete()` failed the same way when another writer had deleted the item, where other models
  return `None`. Like `put()`, they now read the stored item and write again: `update()` moves
  the guards of the values stored now, and `delete()` deletes the item with them or returns
  `None`. In transactions, which cannot read again, and for upserts, which planned again after a
  delete elsewhere would create the item without the unique values their actions do not set, the
  error says to `refresh()` the instance and retry. `delete()` of an instance without unique
  values set whose item another writer had deleted returned the item as loaded instead of
  `None`.
- A `PydanticModel` that subclasses another `PydanticModel` (an abstract base with shared
  fields) got the base's attribute paths as the defaults of the fields it inherits: required
  inherited fields became optional, `create()` raised `ValidationError` ("expected int, got
  FieldPath") for a field with a default, `to_item()` returned the paths as values, and items
  without an inherited attribute could not be read. Subclasses now inherit the fields with their
  defaults and required state, as in pydantic, and redeclaring an inherited field no longer
  warns that it "shadows an attribute in parent".
- `contains()` on a model field that holds a list or set of values with a codec
  (`list[datetime]`, `list[date]`, `set[UUID]`, `list[timedelta]`, a list of plain `Enum`
  members or of documents) put the value into the expression unencoded, so every filter or
  condition with it raised `ValidationError` ("cannot store a value of type datetime") before
  any request. The value is now validated and encoded as the field stores its elements, as
  comparisons already were.
- Large sets were slow to write, and stalled the event loop while they converted. Converting a
  number set to DynamoDB's format took time quadratic in its size, with the GIL held on the event
  loop's thread: `set(range(60_000))`, an item of about 210 KB that DynamoDB accepts, took 3 s,
  during which no other task or thread ran. The in-memory DynamoDB's duplicate check, set
  comparisons and `ADD`/`DELETE` of set elements were quadratic too (a put of that set took
  10 s). All of them now take linear time (13 ms for the conversion, 40 ms for the put).
- `batch_write_item` and `transact_write_items` of `client.call(...)` dropped the item
  collection metrics that `return_item_collection_metrics=True` asks for: the core requested and
  parsed them, and the in-memory DynamoDB computed them, but the outputs left them out. They now
  return `item_collection_metrics`, a list of metrics per table (empty when not asked for), as
  `put_item`, `update_item` and `delete_item` return theirs.
- The in-memory DynamoDB applied part of an `UpdateTable` or `TagResource` request that it then
  refused, where DynamoDB applies nothing: `UpdateTable` set the billing mode, and created and
  backfilled a new global index, before it found an error later in the request (a stream that
  is already enabled, an index that does not exist). `billing_mode=PROVISIONED` without
  throughput left the table provisioned without capacity, so that every later `UpdateTable`
  failed, even one that only turns on deletion protection. `TagResource` added the tags before
  it refused the request for going over 50 tags. Both now validate the whole request first.
- PartiQL `SELECT` pages of the in-memory DynamoDB continued at a position in the list of
  results (`NextToken` was an offset), not after the last item the page read: with `Limit=2`,
  deleting the two rows of the first page made the next page skip two rows, and inserting rows
  before the position repeated rows, so a "select a page, delete it, continue" cleanup loop
  missed items. `NextToken` now stands for the key of the last item the page evaluated, which
  pages also return as `last_evaluated_key`, as DynamoDB's do.
- Client options that pass validation failed later. `RetryPolicy(base_delay=timedelta(...))`
  raised `TypeError`, and with both delays as `timedelta` every client using the policy failed
  to connect with `ConfigurationError`, although durations accept `timedelta`: the policy now
  stores them as float seconds. `max_attempts=2.5`, `batch_concurrency=2.5` (and `bool` or `str`
  counts) were accepted and failed at the first call; counts must now be `int`s, and
  `Client(retry=...)` a `RetryPolicy`, or the constructor raises `TypeError`. Huge but finite
  durations (`timeout=1e20`, `RetryPolicy(base_delay=1e300, max_delay=1e300)`,
  `wait_until_active(timeout=1e19)`) panicked in the core (`internal error: a dynamoxy task
  panicked`); durations longer than about 31 years now mean no limit. `wait_until_active()` and
  `wait_until_deleted()` take a `timedelta` too.
- `close()` closed the client before the async hooks it waited for had finished, so a hook could
  not use the client: an `operation.failed` hook that awaited something and then wrote an audit
  item got `ShutdownError` (reported as `hook.failed`), and the item was never written. It now
  waits for the hooks of the current event loop first. An async hook that called
  `await client.close()` waited for itself and never finished, and a hook still pending on
  another (closed) event loop made `close()` raise `ValueError: The future belongs to a
  different loop` after it had closed the core: `close()` and `events.drain()` now leave out the
  calling task and hooks of other event loops.
- `ensure_tables()` compared the names of key attributes only, although it promises a
  `TypeError` when a table's key differs from the models: a table created with a number
  partition key and a string sort key passed for a model with a string partition key and a
  number sort key, and every later write failed with "Type mismatch for key". It now compares the
  types of the table's and its indexes' key attributes too. Concurrent `create_tables(...,
  exist_ok=True)` or `ensure_tables()` calls (replicas or pytest-xdist workers starting
  together) raised `ResourceInUseError` when another one created the table between the
  existence check and `CreateTable`; they now treat it as an existing table.
- `count()` of a projected read always failed: it dropped the projection but sent its attribute
  names, so `Note.query(client, user_id="u1").project(Note.text).count()` raised
  `ValidationException` ("Value provided in ExpressionAttributeNames unused in expressions"), for
  table reads too. `scan().after(cursor).segments(n).count()` applied one segment's cursor to
  every segment and returned a wrong number; it now raises `TypeError`, as iterating it does.
  `query()` and `count()` refused sort keys that repeat a field of the partition key: with
  `pk="ORG#{org_id}"` and `sk="MEMBER#{org_id}#{user_id}"`,
  `Membership.query(client, org_id="o1", user_id="u1")` raised `ValueError: unexpected arguments
  user_id` (index keys such as `"STATUS#{status}"` and `"{status}#{placed_at}"` too), where
  `get()` worked; they now select the sort key.
- Key templates rendered `Decimal` values of union and `Any` fields as given: with
  `amount: Attr[Decimal | str]` and `sk="PRICE#{amount}"`, `Decimal("1.50")` wrote
  `PRICE#1.50`, the instance read back rendered `PRICE#1.5`, and its `save()` raised "key
  changed". They now render numbers of such fields as the field reads them back, like `Decimal`
  fields ([ADR 0013](docs/adr/0013-stored-forms-read-back-as-written.md)), so an `int | float`
  field renders `2.0` as `2`. Items written before under a non-canonical number keep that key:
  move them as ADR 0013 describes. A `Literal` of a plain `Enum`'s members
  (`Literal[Color.RED]`) passed validation and failed to write: it now stores the member's value
  and reads back the member, and a member whose value DynamoDB cannot store is a `TypeError`
  when the model is declared. `int` fields raised `OverflowError` for `Decimal("Infinity")` and
  `decimal.InvalidOperation` for `Decimal("sNaN")`, instead of a `ValidationError`.
- Removing a middleware while a call ran skipped the next one for that call: a one-shot
  middleware that called its `remove()` and then `call_next` kept the next registered
  middleware (a guard, say) from running, and a `remove()` from another task changed calls in
  flight. A call now runs the middleware registered when it starts. Only 1024 events of a call
  reached hooks before it returned, although the guides promise all of them: the rest waited
  for the next call or the event task, and `testing.capture_events()` missed them (a put
  throttled 600 times captured 1024 of its 1200 request events); now all of them do.
  `LoggingConfig.levels` used the first matching pattern in the mapping's order, so
  `{"request.*": "debug", "request.gave_up": "error"}` logged `request.gave_up` at DEBUG: an
  exact kind now wins over globs, and the longest matching glob over shorter ones.
  `from dynamoxy import *` raised `ImportError` without pydantic installed, as `__all__` listed
  `PydanticModel`; it is now listed only when pydantic is installed.
- Pydantic models that pydantic accepts failed in dynamoxy: with
  `model_config = ConfigDict(use_enum_values=True)`, writes raised `AttributeError: 'str'
  object has no attribute 'value'`; a field that names a class declared later in the module
  (`shipping: Address | None`) made `create_tables()` and writes raise `TypeError: cannot store
  ... ForwardRef(...)`, as dynamoxy read the fields before pydantic had rebuilt the model;
  nested `RootModel` fields were written as `{"root": [...]}`, and reading them raised
  `ItemDecodeError`; and a `default_factory` that takes the validated data (pydantic 2.10 and
  later) raised `TypeError` when an item without that attribute was read. Enum fields now take
  the members' values, dynamoxy resolves forward references when it first needs the fields
  (an undefined name raises a `TypeError` that names it), `RootModel` fields are stored as their
  root value, and such factories get the data (upserts by key pass the key's fields and the
  values the update sets).
- Unique guards could get out of step with their items. An upsert that set a unique value and
  found no item did not require that the item was still missing: when another writer created it
  between the read and the write, the upsert overwrote its value and never deleted its guard,
  so the value stayed taken forever. An upsert that read the item to render a derived attribute
  (`Player.score.add(1)` with `rank="SCORE#{score:08d}"`) wrote a rank of the missing item's
  values over the created item's (score 6, rank `SCORE#00000001`). Such upserts now require
  that the item is still missing, and read it again and write once more when another writer
  created it (a transaction cancels and says to retry). A unique `float` field of an instance
  read back could not be written: the plan compared the stored `Decimal("0.1")` with `0.1` and
  deleted and put the same guard item, which DynamoDB refuses, so `save()`, `put()` and
  `update(latitude.set(0.1))` failed with the value unchanged.
- `commit()` could raise after DynamoDB had applied the transaction. Updated instances are
  refreshed by evaluating the update on the item as it was loaded, and for an unversioned item
  that another writer changed since (a `list_append` to a list the instance read as missing)
  that evaluation failed, so `commit()` raised the `ValidationError` that normally means "not
  applied", and a caller who retried applied the change twice. Such instances are now refreshed
  from the item as stored now, and if it cannot be read, forget their stored copy, without an
  error. The guides now say what a `client.batch_writer()` block that raises does: it writes
  nothing more, and keeps what is queued in the writer for `flush()`.
- Operation records and metrics miscounted. `client.metrics()` counted the capacity of PartiQL
  writes as reads (three `INSERT`s showed 3 read units and no write units), and both it and
  `collect()` counted capacity that DynamoDB reports without a read/write split by operation
  only: PartiQL statements now count as reads for `SELECT` and as writes otherwise. Failed calls
  lost their throttles: a `GetItem` throttled on every attempt recorded 0 throttles in
  `collect()`, `dynamoxy.request.throttles` and the span's `dynamoxy.throttles`; errors of a call
  now carry `.throttles` and `.requests` as well as `.attempts`. `collect().summary()["retries"]`
  counted the requests of managed batches as retries (a 60-item `batch_write` without
  throttling reported 2): a single call now sends one request however many attempts it takes
  (`OperationRecord.requests` and `summary()["requests"]` were its attempts), and retries are
  the attempts beyond the first of each request. A managed batch that ended in
  `UnprocessedItemsError` was recorded as a success, with `operation.succeeded` and an unset span
  status; it is now a failure (and counts only the items it wrote). And
  `db.client.response.returned_rows` recorded the items that writes wrote, for an instrument of
  "Items returned": it now records reads only.
- The in-memory DynamoDB's test helpers differed from DynamoDB. `testing.purge_expired()`
  deleted items whose TTL is more than five years in the past (a TTL of `0`, or of ten years
  ago), which DynamoDB never considers for expiry; it now leaves them. `inject_fault(table=...)`
  only looked at the first table of a call, so a fault for `bbb` never fired for a
  `BatchWriteItem` or transaction that writes `aaa` and `bbb`, nor for `SELECT * FROM bbb`
  (it stayed pending and fired on a later call), and `unprocessed=True` for `aaa` returned both
  tables' requests unprocessed: a fault now matches any table of a call, and returns only its
  table's requests unprocessed. A `TransactWriteItems` replayed with the same client request
  token reported no consumed capacity; it now reports what DynamoDB bills a replay, a
  transactional read of the items the first call wrote.
- The in-memory DynamoDB accepted queries and writes that DynamoDB refuses. A key condition with
  an empty string or binary value (`pk = :p` with `""`) returned nothing instead of raising
  `ValidationError` ("The AttributeValue for a key attribute cannot contain an empty string
  value. Key: pk"). A query's `ExclusiveStartKey` was checked only for the table key: an index
  start key with a number for a string key, or with an extra attribute, restarted at the first
  item, and start keys in another partition or outside the sort key condition were accepted;
  they now fail with DynamoDB's messages ("The provided starting key is invalid", "... is
  outside query boundaries based on provided conditions", "... does not match the range key
  predicate", ...), as captured against DynamoDB by dynoxide-rs, and an index scan names the
  index of a start key of the wrong type. Secondary index key values had no size limit (a
  3,000-byte global index partition key was accepted): they now have the 2,048- and 1,024-byte
  limits of table keys. `TransactWriteItems` counted only `Put` items towards its 4 MB limit, so
  eleven updates that store 390 KB items each were accepted; updates now count with the item
  they store.
- Smaller binding and AWS adapter issues. The native module's functions took only `dict`s where
  its type stubs promise any mapping: a `MappingProxyType` or `UserDict` as
  `expression_attribute_names`/`expression_attribute_values`, `request_items`, `tags`, or the
  item of `from_wire()` and `value_from_wire()` raised `TypeError`; they now take any mapping,
  and PartiQL `parameters` given as a `str` raise `TypeError` instead of being split into
  characters. The typed wire format accepted invalid input: `"===="`, `"A==="` and `"AA==AAAA"`
  read as zero bytes, `{"NULL": false}` as `None` and `{"SS": []}` as an empty set, while
  `{"N": 5}`, `{"BOOL": 1}` and non-`str` names raised `TypeError`; they now raise
  `ValidationError`, with DynamoDB's wording where it has one. A lone surrogate in text raised
  `UnicodeEncodeError`, an `int` of more than 4,300 digits Python's `ValueError`, and an `int`
  subclass with its own `__str__` failed above 2**63; they are now a `ValidationError` and the
  number. In-memory calls could not be awaited with `yield from call.__await__()` in
  `types.coroutine` code (their awaitable had no `__iter__`). A wake-up write of the event loop
  bridge that failed with anything but a full buffer left later calls undelivered; it is now
  retried. Table statuses the AWS SDK adds later (and `REPLICATION_NOT_AUTHORIZED`) read as
  `ACTIVE`, and index statuses it adds later as no status, so waiters took such tables for
  ready; they now read as `UNKNOWN` (or their name).
- Expressions that `&`, `|` and `~` built from a hand-written `dynamoxy.condition(...)` wrapped
  it in redundant parentheses (`(#a0 = :v0) AND ((#a1 > :v1))`), which DynamoDB refuses with
  "The expression has redundant parentheses"; the in-memory DynamoDB accepted them. Hand-written
  conditions now compile without parentheses of their own.
- The in-memory DynamoDB handled PartiQL and expressions differently from DynamoDB, as captured
  against DynamoDB by dynoxide-rs. It now refuses a `BatchExecuteStatement` that mixes reads and
  writes or names an item twice; `RETURNING` in `ExecuteTransaction`; an `UPDATE` or `DELETE`
  that gives a key attribute two values; `DELETE ... RETURNING ALL NEW *`; writes to an index
  (`INSERT INTO "t"."i"`); an `UPDATE` without `SET` or `REMOVE`; `exists()`, which DynamoDB's
  PartiQL does not have (it now has `attribute_exists()` and `attribute_not_exists()`); `Limit=0`;
  `ExecuteTransaction` tokens longer than 36 characters; `set_add(b, ...)` assigned to another
  attribute, and `set_add(n, 5)` of a value that is not a set, which added a number; expressions
  with more than 300 operators; redundant parentheses; empty attribute names (in items, maps and
  `ExpressionAttributeNames`); and `contains(a, a)`. It parses negative numbers after keywords
  (`n BETWEEN -5 AND 5`). `ReturnValuesOnConditionCheckFailure=ALL_OLD` returns the stored item in
  batches and transactions. Mixed transactions, unkeyed transaction and batch reads, batch reads
  of missing tables, the hash key size ("of2048 bytes"), updates over 400 KB and empty index key
  values set by updates have DynamoDB's messages. PartiQL reads report capacity in the requested
  detail, a `DELETE` of a missing item costs a write, batch reads and read transactions report
  their reads, and transactions double the table's write units but not the indexes'. The
  expression checks of the test suite (`_core._check_expressions`) validate update paths too.
- PartiQL `SELECT`s of an index in the in-memory DynamoDB returned rows where DynamoDB refuses
  them, as captured against DynamoDB by dynoxide-rs: a projection of a global index that names
  attributes it does not hold ("Global secondary index i does not project [a]"), and a `WHERE`
  clause keyed on the index's partition key that filters on them, of either kind ("Secondary
  index i does not project one or more filter attributes: [a]"). Both are now refused. An
  unkeyed `WHERE` clause still scans the index and matches nothing on such attributes.
- Loaded instances whose key another writer stored in a form that decodes to the same values but
  that dynamoxy does not write (an upper-case UUID, a date as `20260102`, also inside a key
  template) rendered the canonical form, which addresses no item: `save()`, `delete()` and
  `update()` through them raised "the key of this instance changed", and `primary_key()`
  returned a key of no item, and an immutable value stored that way made `save()` raise "is
  immutable". Writes through a loaded instance (batches and transactions included) and its
  `primary_key()` now use the key as stored, as ADR 0015 says, and keep such immutable values.
- A repository stored instances of its model's subclasses as its own model:
  `client.model(Member).create(Admin(...))` wrote `_type: "Member"` and dropped the subclass's
  fields, and a `Member` read by key of an `Admin` item decoded it as a `Member`, whose `save()`
  rewrote its type attribute, so that `Admin` queries no longer found it. Writes through a
  repository now raise `TypeError` for instances of subclasses (write them through
  `client.model(Admin)`), and its reads raise `ItemDecodeError` for items whose type attribute
  names another model of the table (entity names no model declares still decode); upserts set the
  type attribute only on items without one, so they no longer re-type another model's item. A
  field typed as a native document refuses instances of the document's subclasses, whose fields it
  dropped.
- Saves by an older version of an application erased the keys a newer one added to documents
  (`address.zip_code`): writes of loaded instances kept the item's undeclared top-level
  attributes only, although the usage guide said older versions do not erase what a newer one
  wrote. A document read from an item (native, a model used as a document, or pydantic) now
  writes back the map keys no field declares, also in lists and maps of documents and in update
  values; a document the application builds replaces the map (ADR 0020).
- Pydantic models checked the key values of reads and writes by key, and the values `update()`
  sets, against the field's type only: a key that a validator normalizes
  (`AfterValidator(str.lower)`, a `@field_validator`) found no item, and an update wrote values
  that `Field` constraints refuse, then raised `ItemDecodeError` after the write. Such values now
  run the field's pydantic validation before any I/O: its type, constraints, `Annotated`
  validators and `@field_validator`s, under the model's config. Validators that take a
  `ValidationInfo` (they may read other fields) and model validators still run on instances
  only.
- Updates rendered derived (templated) attributes from the wrong item. An instance that was not
  loaded, used as an update target, had its own values taken for the stored ones:
  `players.update(Player(player_id="p1", name="Ada"), Player.score.add(10))` of a stored score of
  5 stored a score of 15 and a rank of `SCORE#00000010`. Updates by key read the item, and updates
  of loaded instances without a version used the item they were loaded with, but neither required
  it unchanged, so another writer's change in between left the attribute behind (score 26, rank
  `SCORE#00000025`), also for versioned models updated by key. Such updates now read the item
  first and require that the fields the attribute is rendered from are unchanged; when another
  writer changed them, `update()` reads the item again and renders it from that.
- `table.update_settings(billing=Billing.provisioned(...))` of an on-demand table sent capacity
  for the table only, although DynamoDB needs it for every global index too; it now gives each
  global index the table's capacity. `ensure_tables(wait=False)` added a second missing index
  while DynamoDB was still creating the first (also when an earlier call's waiter had given up
  during a long backfill), and `create_tables`/`ensure_tables(wait=False)` enabled Time to Live
  on a table DynamoDB was still creating: each step now waits for the one before it, and Time to
  Live is enabled before indexes are added, so that it does not wait for their backfills.
- Type checkers typed `dynamoxy.errors.AccessDeniedError` as `Any` after a plain
  `import dynamoxy` (Pylance and pyright resolved `dynamoxy.errors` and `dynamoxy.events` through
  the package's `__getattr__`), and every misspelled name of the package too. The public
  submodules are now attributes of the package with their types, and type checkers report names
  the package does not define.
- Reads of an index that name attributes it does not hold passed in tests and failed against
  DynamoDB, which refuses a query filter on them, for local indexes too and also when the query
  reads the items from the table, and a projection of a global index naming them
  ([ADR 0021](docs/adr/0021-index-reads-follow-dynamodbs-refusals.md), measured against DynamoDB).
  The in-memory backend now refuses them with DynamoDB's messages, and its scan filters see the
  index entries (where such attributes never match) unless the scan reads the items from the
  table. Model queries raise `ValueError` before any request for a filter of a
  `KEYS_ONLY`/`INCLUDE` local index on an attribute it does not hold (filter the hydrated items in
  Python instead), and model reads for `.project(...)` of such a global index naming one, unless
  the query adds `.hydrate()`, which now reads the index for the keys only.
  Queries of an index that does not hold the type attribute (created by other tools or by
  dynamoxy 0.2) fail in DynamoDB: create the index again with it.
- The in-memory backend accepted table changes DynamoDB refuses (measured against DynamoDB):
  switching a table with global indexes to provisioned billing without capacity for them, adding
  an index without capacity to a provisioned table, giving an index capacity on an on-demand
  table, and changing a table, or adding an index, while DynamoDB is still creating the table or
  another index (and, following DynamoDB's message, deleting an index then). It now refuses them with DynamoDB's errors, drops the indexes'
  capacity on a switch back to on-demand, and a new table or index reads `CREATING` in the first
  `DescribeTable` after it and `ACTIVE` from the next on (the waiter polls it without sleeping),
  so tests catch a missing wait. `table.add_index()` without `billing=`
  gives an index added to a provisioned table the table's capacity (DynamoDB refused it).
  `ensure_tables()` described the Time to Live of a table another process was still creating,
  which DynamoDB refuses ("Cannot describe time to live while table is in CREATING state"): it
  waits for the table first, and the in-memory backend refuses it too.
- The in-memory backend accepted expressions DynamoDB refuses (measured against DynamoDB):
  placeholders (`#name`, `:value`) longer than 255 bytes or with characters other than ASCII
  letters, digits and `_` (`#a-b` was reported as undefined or unused), attribute names of 64 KB
  or more in items and through placeholders, and `attribute_type(a, b)` with a path as the type.
  It now refuses them with DynamoDB's messages, cancels a read-only `ExecuteTransaction` of a
  missing table with reason `ResourceNotFound` (it raised `ResourceNotFoundException`), and puts
  the expression errors of `PutItem`, `UpdateItem` and `DeleteItem` and the attribute names they
  refuse in DynamoDB's `1 validation error detected: ` envelope.
- The in-memory backend accepted an item that fits 400 KB alone but not together with its
  entries in the table's local indexes, which DynamoDB refuses (measured against DynamoDB):
  `PutItem` and `UpdateItem` raise its `ValidationError`, and a transaction cancels with it as
  the reason. It also refuses a `TransactGetItems` that would return more than 4 MB ("Payload in
  response is too large."), and refuses a `TransactWriteItems` over 4 MB with DynamoDB's message
  ("Transaction payload size cannot exceed 4MB. Payload Size: ..."), counting the payload as
  DynamoDB does: a put's item, an update's key and request charge, a delete's and a check's key,
  and their conditions. A standalone `UpdateItem` is
  held to 400 KB by that charge before the item is read, and item collection metrics report
  whole gigabytes (`[0.0, 1.0]` for a small collection) as DynamoDB does, instead of the exact
  fraction.
- A PartiQL `INSERT` of an item that exists raised a plain `ServiceError` against LocalStack,
  whose DynamoDB Local sends the code `DuplicateItem` instead of `DuplicateItemException`: both
  raise `DuplicateItemError` now. dynamoxy's end-to-end tests pass against LocalStack too
  (`docs/parity`).

### Security

- The native extension bundles rustls 0.23.46 and rustls-webpki 0.103.16. rustls 0.23.46 fixes
  GHSA-h2v8-394m-p7vv, GHSA-c8m5-c952-6cm4, GHSA-w44r-93j7-qv28 and GHSA-vp88-798v-h67f. The
  extension no longer contains the AWS SDK's legacy HTTP client, which dynamoxy never used, or
  its rustls 0.21 and rustls-webpki 0.101, which get no fixes for these advisories or for
  GHSA-82j2-j2ch-gfr8, GHSA-xgp8-3hg3-c2mh and GHSA-965h-392x-2mh5.

### Features

- **client:** Client.model(User) replaces the deprecated client.repository(User) (#55)
- **models:** [**breaking**] Model.create(client, ...) builds and writes a new item in one call (#56)
- **models:** [**breaking**] Model.get(client, ...) and Model.query(client, ...) read items (#58)

### Bug fixes

- **deps:** Update rustls and drop the AWS SDK's legacy TLS client (#57)
- **repository:** Writes through a loaded instance never reach another item (#61)
- **models:** Instances no longer share a mutable default (#62)
- **query:** Projections keep what where() and hydrate() need without a limit (#63)
- **core:** Deeply nested input raises an error instead of crashing the process (#64)
- **expressions:** Characters outside ASCII are syntax errors, not panics (#65)
- **client:** Writes whose retries run out after an uncertain attempt are unknown (#67)
- **repository:** Items stored before the model had a version can be saved (#68)
- **models:** Members of mixin enums are the plain values of str and int fields (#69)
- **query:** Requests of growing reads ask for at most a page's worth of items (#70)
- **errors:** Errors pickle, so they cross process boundaries (#72)
- **models:** Optional fields stay optional with a codec of their own (#105)
- **memory:** PartiQL transactions that name an item twice are refused (#104)
- **expressions:** List elements set past the end append in order of their index (#103)
- **memory:** Local index reads fetch what the index does not hold (#108)
- **models:** Key and index lookups run the field's validators, as writes do (#109)
- **models:** [**breaking**] None and empty sets read back as written, not as the default (#113)
- **models:** Expressions on inherited fields use the subclass's datetime_format (#111)
- **repository:** Unique models check delete conditions on missing items, and stale deletes and updates read again (#110)
- **models:** Pydantic subclasses of a PydanticModel inherit its fields with their defaults (#115)
- **expressions:** Contains() encodes members of lists and sets as the field stores them (#116)
- **core:** Sets of many elements convert and compare in linear time (#117)
- **client:** Batch and transaction writes return the item collection metrics they ask for (#118)
- **memory:** Refused UpdateTable and TagResource requests change nothing (#119)
- **memory:** PartiQL pages continue after the last item the page read (#120)
- **client:** Retry policies and client options refuse values that would fail later (#121)
- **client:** Close() waits for async hooks before it closes the client (#122)
- **tables:** Ensure_tables() compares key types, and tables created meanwhile count as existing (#123)
- **query:** Counts of projected reads work, and sort keys may repeat partition key fields (#125)
- **models:** Numbers of union fields render as stored, and literals of plain enum members write (#126)
- **client:** Calls keep their middleware chain and deliver all their events (#127)
- **pydantic:** Use_enum_values, forward references, RootModel fields and data-taking factories work (#128)
- **unique:** [**breaking**] Upserts that race with creates, unique floats written back, and collections refused (#129)
- **transactions:** Commit() no longer raises after the transaction was applied (#130)
- **telemetry:** Records and metrics count PartiQL writes, throttles of failed calls, batch retries and rows (#131)
- **testing:** Purge_expired, inject_fault(table=) and replayed transactions behave like DynamoDB (#132)
- **memory:** Queries refuse empty key values and start keys outside the key condition, and index keys have size limits (#133)
- **binding:** Conversions take any mapping and refuse invalid wire values, and unknown table statuses are not active (#134)
- **memory:** PartiQL and expressions follow DynamoDB's rules, and hand-written conditions get no redundant parentheses (#135)
- **memory:** PartiQL index reads refuse attributes the index does not hold (#137)
- **models:** Writes through loaded instances use keys stored in another form (#138)
- **models:** Repositories refuse instances of subclasses and items of other models (#139)
- **models:** Documents read from an item keep the keys their class does not declare (#140)
- **pydantic:** Key values and update values run the field's pydantic validators (#141)
- **repository:** Updates render derived attributes from the stored item, and require it unchanged (#142)
- **tables:** Provisioned billing gives global indexes capacity, and table steps wait for the one before (#143)
- **errors:** Native errors name dynamoxy.errors as their module (#145)
- **typing:** Dynamoxy.errors and the other public submodules keep their types after import dynamoxy (#146)
- **indexes:** Reads refuse filters and projections of attributes an index does not hold (#148)
- **tables:** The in-memory backend refuses table changes DynamoDB refuses (#149)
- **versions:** [**breaking**] Version() fields hold a random token, so a stale instance never matches again (#150)
- **expressions:** The in-memory backend refuses placeholders, names and operands DynamoDB refuses (#151)
- **limits:** Local index entries count toward the 400 KB of an item, and transactions keep to 4 MB (#152)
- **errors:** LocalStack's DuplicateItem code raises DuplicateItemError, and the e2e tests pass on LocalStack (#153)
- **release:** Generated changelog entries list their pull request once (#156)

### Refactoring

- **models:** Import annotationlib only on Python 3.14 and later (#54)

### Documentation

- Agents that can push open their pull requests themselves (#53)
- **parity:** Keep DynamoDB's measured answers and the probes that ask them (#147)

### Tests

- Probe dynamoxy itself on DynamoDB, MiniStack, LocalStack and the in-memory backend (#154)

### Build and CI

- **release:** 0.x releases are full releases, and the README shows parity with DynamoDB (#157)

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

- **memory:** PartiQL SELECT with ORDER BY, by DynamoDB's rules (#42)
- **repository:** Model writes recognise their own earlier attempt by a write id (#44)

### Bug fixes

- **query:** Parallel scans that stop early no longer hang (#21)
- **query:** Limit() reads only what it needs and cursors of cut pages skip nothing (#22)
- **repository:** Table queries of unique models and where() on templated sort keys (#23)
- **errors:** Error messages leave out keys and stored values (#40)
- **models:** [**breaking**] Stored values decode exactly, or raise ItemDecodeError (#41)
- **client:** [**breaking**] Retried writes never apply twice or fail on their own earlier attempt (#43)

### Documentation

- Highlights, design and performance sections in the README (#37)
- A benchmark script reproduces the README's performance numbers (#38)
- A shorter README that is easier to read (#46)
- Show the dynamoxy logo in the README heading (#47)

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

- **models:** Before_update and after_update lifecycle hooks (#11)
- **models:** [**breaking**] Model instances save, update and delete themselves through their repository (#12)
- **query:** Consistent=True for queries and scans (#18)

### Bug fixes

- Compare binary values when the in-memory backend matches transaction idempotency tokens (#3)
- **repository:** Render templated attributes from the new value after arithmetic updates (#9)
- **models:** Copy() makes new instances, and copies keep their own persistence state (#10)
- **query:** Hydrate() reads full items as consistently as its query (#17)

### Performance

- Faster in-memory backend, expression parser and numbers (#4)
- Replace pyo3-async-runtimes with an own asyncio bridge (#5)
- Skip unused work in the call pipeline (#6)
- Precomputed model plans, faster repositories and queries (#7)

### Documentation

- Document how credentials and region are resolved (#14)
- A skill for pull requests, stacks and publish kits (#15)
- Document table_prefix for environments sharing an AWS account (#16)

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
