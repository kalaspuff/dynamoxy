# dynamoxy usage guide

dynamoxy is a typed, async DynamoDB ORM for Python with a Rust core. This guide is the compact,
canonical reference for writing applications with it, for developers and AI agents alike. It
ships inside the package, so it always matches the installed version:

```bash
dynamoxy guide          # or: python -m dynamoxy guide
```

Contents: rules of thumb · a complete program · modeling · reading · writing · unique values ·
optimistic locking · transactions · single-table design · tables · the dict-level API and PartiQL
· application patterns · configuration · errors · observability · testing · deployment and limits.

## 1. Rules of thumb

1. **Create one `dynamoxy.Client` per process and share it.** It holds the connection pool,
   retries and metrics. Create it after `fork()` (worker processes create their own) and close it
   at shutdown (`async with client:` or `await client.close()`).
2. **Reach models through the client.** `User.create(client, ...)`, `User.get(client, ...)`
   and `User.query(client, ...)` create, read and query items; `client.model(User)` gives
   every typed operation (a `dynamoxy.Repository[User]`). Collect the queries of an aggregate
   in a subclass of `dynamoxy.Repository` instead of scattering raw calls. Instances a
   repository read or wrote write themselves through it: `await user.save()`,
   `await user.increment(User.visits)`, `await user.delete()`.
3. **Pick the write that states your intent.** `create()` fails if the key exists, `save()`
   checks the version (optimistic locking), `put()` overwrites, and `update()` changes attributes
   in place without reading. Prefer `update()` with atomic actions (`User.visits.add(1)`) over
   read-modify-write.
4. **Model access patterns, not tables.** Every query needs a partition key value. Add a
   `GlobalIndex` for each additional lookup (`by_email`, `by_status`). Never use `scan()` on hot
   paths.
5. **Let dynamoxy enforce invariants.** Use `unique=True` for values like emails, `version()`
   for concurrent edits, `created_at()`/`updated_at()` for timestamps, field constraints
   (`min_length`, `ge`, `pattern`, ...) and `@dynamoxy.validator` for rules across fields.
6. **Use transactions for multi-item invariants** (`async with client.transaction() as tx:`).
   They are atomic and all-or-nothing, with at most 100 operations, and they cost twice the
   capacity of the same writes.
7. **Catch specific errors.** `ItemAlreadyExistsError`, `VersionConflictError`,
   `UniqueConstraintError` and `ItemNotFoundError` describe what went wrong;
   `TransactionCanceledError.cancellation_reasons` explains each operation. Throttling and server
   errors are retried for you.
8. **Paginate with cursors.** `await query.page()` returns `items` and an opaque `cursor`;
   `query.after(cursor)` continues. `all()` reads every page: use it only for bounded results.
9. **Never put secrets or personal data in key names or events.** Keys appear in logs, metrics
   and traces as table names and counts only; dynamoxy never logs item contents, and its error
   messages leave out keys and values (`error.key`, `error.value` and `FieldError.value` hold
   them: log them only when they are not personal data).
10. **Test with `dynamoxy.testing.memory_client()`.** It is an in-memory DynamoDB with the same
    semantics (expressions, conditions, indexes, transactions, limits), so tests need no AWS
    account, Docker or emulator.

## 2. A complete program

```python
import asyncio
from datetime import datetime

import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True, pattern=r"[^@\s]+@[^@\s]+")
    name: Attr[str] = field(min_length=1)
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()
    version: Attr[str] = dynamoxy.version()

    by_email = dynamoxy.GlobalIndex(partition_key=email)


async def main() -> None:
    # Region and credentials come from the standard AWS configuration unless given.
    async with dynamoxy.Client(region="eu-west-1") as client:
        await client.ensure_tables(User)  # creates missing tables and indexes; deletes nothing

        ada = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
        await ada.update(User.visits.add(1), User.tags.add({"admin"}))

        found = await User.query(client, index=User.by_email, email="ada@example.com").first()
        assert found is not None and found.visits == 1


asyncio.run(main())
```

For local development and tests, replace the client with `dynamoxy.testing.memory_client()`, or
point it at an emulator with `dynamoxy.Client(endpoint_url="http://localhost:4566", ...)`.

## 3. Modeling

### Fields

A model is a class with `table=`; fields are annotated `Attr[T]`. The annotation decides how
values are stored (see `dynamoxy.codecs`):

| Annotation | Stored as |
| --- | --- |
| `str`, `UUID` | `S` |
| `int`, `float`, `Decimal` | `N` (exact; NaN and infinity rejected) |
| `bool` | `BOOL` |
| `bytes` | `B` |
| `datetime` | `S`: fixed-width ISO 8601 in UTC (`2026-10-13T08:48:47.966937+00:00`), so text order is time order. Naive datetimes are rejected. Formats: see "Timestamp formats" below. |
| `date` | `S` (`YYYY-MM-DD`) |
| `timedelta` | `N` (seconds) |
| `Enum`, `Literal[...]` | the member value |
| `list[X]`, `tuple[X, ...]` | `L` |
| `set[X]`, `frozenset[X]` | `SS` / `NS` / `BS` (empty sets are not stored, and read back empty) |
| `dict[str, X]` | `M` |
| a `dynamoxy.Document` or pydantic model | `M` |
| `X \| None`, `Literal[..., None]` | `X`, or nothing when `None`, which reads back as `None` (also with a `codec=` of its own) |
| `Any` | as given |

Reading is exact: a stored value of another type, or one the field's type cannot hold (`2.5` in an
`int` field, `"false"` in a `bool` field), raises `ItemDecodeError` instead of being converted.
A missing attribute reads as `None` in an `X | None` field and as an empty set in a set field,
whatever the field's default, since that is how they are stored; a `set[X] | None` field reads an
empty set back as `None`. Other fields read a missing attribute (an item written before the field
existed) as their default.

Field specifiers configure fields. Fields without a default are required by the constructor
(type checkers know this too). Give defaults with `field(default=...)`, not a bare value
(`visits: Attr[int] = 0` works at runtime, but type checkers reject it):

```python
from decimal import Decimal
from enum import StrEnum

from dynamoxy import Attr, Document, Model, field, partition_key, sort_key


class Status(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class Address(Document):  # stored as a map inside the item
    street: Attr[str]
    city: Attr[str]


class Account(Model, table="accounts"):
    account_id: Attr[str] = partition_key()
    owner: Attr[str] = field(min_length=1, max_length=200)
    balance: Attr[Decimal] = field(default=Decimal(0), ge=0)
    status: Attr[Status] = field(default=Status.ACTIVE)
    address: Attr[Address | None] = field(default=None)
    labels: Attr[dict[str, str]] = field(default_factory=dict)
    legacy_name: Attr[str | None] = field(default=None, attribute_name="name")  # stored as "name"
    country: Attr[str] = field(default="SE", immutable=True)  # changes of a stored value are rejected


class Entry(Model, table="ledger"):
    account_id: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    amount: Attr[Decimal]
```

`field()` options: `default`, `default_factory`, `unique`, `min_length`, `max_length`,
`pattern` (full match), `ge`, `gt`, `le`, `lt`, `validators` (callables `value -> value`),
`immutable`, `attribute_name`, `codec`, `template`, `description`, `repr`.

Automatic fields are set by dynamoxy and are not constructor arguments:

| Specifier | Value |
| --- | --- |
| `dynamoxy.created_at()` | `datetime` of the first write |
| `dynamoxy.updated_at()` | `datetime` of every write |
| `dynamoxy.version()` | `str`, a random token, new on every write; enables optimistic locking |
| `dynamoxy.ttl(default=None)` | `datetime` (stored as epoch seconds) after which DynamoDB deletes the item |

Timestamp formats: `"iso"` (default, `2026-10-13T08:48:47.966937+00:00`), `"iso_z"`
(`2026-10-13T08:48:47.966937Z`, the RFC 3339 style with a `Z` suffix), `"epoch"` and
`"epoch_ms"` (numbers). Choose one per model hierarchy with a class keyword; it covers every
`datetime` field and `created_at()`/`updated_at()`:

```python
from datetime import datetime

from dynamoxy import Attr, Model, created_at, partition_key, sort_key


class Base(Model, abstract=True, datetime_format="iso_z"):  # inherited by subclasses
    created_at: Attr[datetime] = created_at()


class Reading(Base, table="readings"):
    sensor_id: Attr[str] = partition_key()
    taken_at: Attr[datetime] = sort_key()  # "2026-10-13T08:48:47.966937Z"
```

A field overrides it with `created_at(format=...)` or `codec=dynamoxy.codecs.DateTimeCodec(...)`.
A subclass that sets another format stores the fields it inherits in it too: build its conditions
and updates from its own attributes (`Reading.created_at`), as those of the base class encode
values in the base's format. Nested `Document`s declare their own
(`class Address(Document, datetime_format="iso_z")`); `PydanticModel` takes the same keyword and
`CreatedAt("iso_z")`. Both ISO styles are always read, but keys, templates and conditions compare
stored text: keep one format per attribute.

Rules that are easy to miss:

- Assigning a field validates it immediately (`user.age = -1` raises `ValidationError`), and so
  does the constructor. `ValidationError.errors` lists every `FieldError`.
- Field names cannot shadow the model API (`key`, `primary_key`, `to_item`, `create`, `get`,
  `query`, `save`, `delete`, `update`, `add`, `increment`, `decrement`, `refresh`, ...), in
  pydantic models too; native models also reserve `validate`, `copy` and `to_dict`. Name the
  field differently and keep the stored name with `attribute_name=`
  (`search_query: Attr[str] = field(attribute_name="query")`; pydantic:
  `Annotated[str, dynamoxy.AttributeName("query")]`, plus `Field(alias="query")` to keep the
  name in pydantic's input and output).
- A field annotated without a specifier (`kind: Attr[str]`) has no class attribute while the
  class body runs. Refer to it by name in index declarations: `GlobalIndex(partition_key="kind")`.
- `item.copy(**changes)` is a new, unsaved instance: its automatic fields (`created_at`,
  `updated_at`, `version`) start empty, so saving it writes a new item. Unknown field names raise
  `TypeError`. `copy.copy(item)` and pydantic's `model_copy()` duplicate the instance as it is,
  loaded state included.
- A loaded instance keeps its key and its `immutable=True` values: `save()`/`put()` of one whose
  key (or a field its key template uses) or immutable value changed raises `ValueError`, and so
  do `update()`, `delete()` and transactions of one whose key changed. Write a copy instead
  (`create(item.copy(...))`) and delete the old item; `refresh()` reloads the stored item.
- DynamoDB reserved words (`name`, `status`, `count`, ...) are fine as attribute names:
  dynamoxy always uses placeholders.

### Validators and lifecycle hooks

```python
import dynamoxy
from dynamoxy import Attr, partition_key


class Booking(dynamoxy.Model, table="bookings"):
    booking_id: Attr[str] = partition_key()
    nights: Attr[int]
    guests: Attr[int]

    @dynamoxy.validator
    def check_capacity(self) -> None:  # runs on construction and before every write
        if self.guests > 2 * self.nights + 4:
            raise ValueError("too many guests for this stay")

    @dynamoxy.before_save
    async def normalize(self) -> None:  # sync or async; may change the instance; raising aborts the write
        ...

    @dynamoxy.after_save
    async def announce(self) -> None:  # after create, put, save (also booking.save()), batch puts
        ...

    @dynamoxy.after_update
    def forget(self) -> None:  # after update(), add(), increment(), decrement() of an instance
        ...

    @dynamoxy.after_load
    def migrate(self) -> None:  # sync; upgrade old data shapes in memory
        ...
```

Hook points: `before_save`, `after_save` (create, put, save, batch puts), `before_update`,
`after_update` (updates of instances: `update()` and the instance's `add()`, `increment()`,
`decrement()`), `before_delete`, `after_delete`, `after_load`. They run in repositories and
transactions alike; writes by key run none. `before_update` hooks can abort, but the update only
writes its actions, so changes they make to the instance are not stored. Client hooks apply to
any model and receive a `HookContext` (operation, model, client):

```python
@client.hook("after_save", User)
async def audit(user: User, context: dynamoxy.HookContext) -> None:
    await audit_log.write(context.operation, user.user_id)
```

### Keys, indexes and key templates

```python
from datetime import datetime

from dynamoxy import Attr, GlobalIndex, LocalIndex, Model, Projection, field, partition_key, sort_key


class Event(Model, table="events"):
    stream: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    kind: Attr[str]
    at: Attr[datetime]

    by_kind = GlobalIndex(partition_key="kind", sort_key="at", projection=Projection.keys_only())
    by_time = LocalIndex(sort_key="at")  # same partition key, another sort key
```

Index projections: `Projection.all()` (default), `Projection.keys_only()`,
`Projection.include("a", "b")`. Index names default to the attribute name. Items without the
index key attributes are not in the index (sparse indexes). For models with an entity type,
dynamoxy also projects the type attribute (`_type`) into `KEYS_ONLY`/`INCLUDE` indexes it creates.

Key templates build keys from other fields (`partition_key(template="CUSTOMER#{customer_id}")`,
`field(template="STATUS#{status}")`). Templated fields are derived, never passed to the
constructor, and are recomputed on every write, including `update()` of the fields they use.
See section 9.

### Pydantic models

```python
from decimal import Decimal
from typing import Annotated

import dynamoxy


class Wallet(dynamoxy.PydanticModel, table="wallets"):
    wallet_id: Annotated[str, dynamoxy.PartitionKey()]
    handle: Annotated[str, dynamoxy.Unique()]
    balance: Decimal = Decimal(0)
    version: Annotated[str | None, dynamoxy.Version()] = None

    by_handle = dynamoxy.GlobalIndex(partition_key="handle")
```

Pydantic validates; dynamoxy maps. Markers: `PartitionKey`, `SortKey`, `Unique`,
`AttributeName`, `Template`, `CreatedAt`, `UpdatedAt`, `Version`, `TTL`, `UseCodec`. Class
attributes are expression paths at runtime; wrap them in `dynamoxy.attr()` for static typing:
`dynamoxy.attr(Wallet.balance) >= 10`. Install with `pip install 'dynamoxy[pydantic]'`.
`use_enum_values=True`, forward references to classes declared later, `RootModel` fields
(stored as their root value) and default factories that take the data work as in pydantic.

To store an existing pydantic model without changing it, subclass it together with
`dynamoxy.PydanticModel` and re-declare only the fields that need a marker
(`class ContactRecord(Contact, dynamoxy.PydanticModel, table="contacts")` with
`contact_id: Annotated[str, dynamoxy.PartitionKey()]`). Fields, defaults and validators are
inherited, and loaded records are instances of the original model.

## 4. Reading

```python
user = await User.get(client, "u1")  # None when missing; User | None for type checkers
orders = await Order.query(client, customer_id="c1").descending().limit(10).all()

users = client.model(User)  # the same repository on every call: every read of the model
user = await users.get("u1")  # what User.get(client, "u1") calls
user = await users.get_or_raise("u1")  # ItemNotFoundError when missing
user = await users.get("u1", consistent=True)  # strongly consistent read
present = await users.exists("u1")
await users.refresh(user)  # reload in place
many = await users.batch_get(["u1", "u2", "u3"])  # any number of keys; missing ones are skipped
```

Keys are given positionally (partition, then sort), by key field name, or by the fields a key
template uses: `orders.get(customer_id="c1", order_id="o1")`. `User.key("u1")` builds a stored
key for APIs that take one. `User.get(client, ...)` and `User.query(client, ...)` take the same
arguments as the repository's `get` and `query`, after the client.

Key values (of `get`, `query` and index queries, `User.key(...)`, writes by key) are validated
like stored values: a normalizing validator applies (`field(validators=(str.lower,))` makes
`email="Ada@Example.com"` find `ada@example.com`), and a value the field refuses raises
`ValueError` before any request; conditions (`where()`, `filter()`) compare the value as given.
Stored keys (`{"user_id": "u1"}`, `User.key(...)`) are used as they are by the calls that take
one: `batch_get`, `update`, `delete`, `batch_delete` and transaction operations, but not `get`,
`exists` or `query`. Read an item whose key predates a constraint or validator with
`users.batch_get([{"user_id": "Ada"}])`. Pydantic models run the field's pydantic validators on
key values and on values `update()` sets, except validators that take a `ValidationInfo` and
model validators (a field whose `PlainValidator` takes one has only its type checked).

### Queries

A query reads one partition of the table or of an index:

```python
entries = client.model(Entry)

recent = (
    entries.query(account_id="a1")
    .where(Entry.seq >= 100)  # sort key condition
    .filter(Entry.amount > 0)  # filter: applied after reading (still costs the read)
    .descending()
    .limit(20)
)
async for entry in recent:
    ...

first = await recent.first()  # or .one(), .all(), .count(), .exists()
by_kind = await client.model(Event).query(index=Event.by_kind, kind="signup").all()
latest = await entries.query(account_id="a1", consistent=True).all()  # strongly consistent read
```

Query builders are immutable: every method returns a new query, so queries can be shared and
reused. `where()` takes the one sort key condition DynamoDB allows: a comparison, `between()` or
`begins_with()` (combine bounds with `between()`); on a templated sort key it replaces the prefix
condition dynamoxy adds. Entries of a `KEYS_ONLY` or `INCLUDE` global index hold only some
attributes: model reads of one need `.hydrate()` (the full items) or `.project(...)` (dicts of the
attributes it holds) and raise `ValueError` otherwise, before any request; `count()` and
`exists()` need neither. Local indexes read the missing attributes from the table. A query filter
on an attribute an index does not hold (global or local), a scan filter of such a global index
(or of a local one that counts, hydrates or projects only what it holds), and a `.project(...)`
of one from a global index without `.hydrate()`, raise `ValueError`: DynamoDB refuses them (or
they would never match).

Reads are eventually consistent by default. `consistent=True` on `query`, `scan` and `count` (as
on `get`) reflects every write that succeeded before the read, at twice the read cost: use it to
read your own writes. Global secondary indexes do not support it (`ValueError` before any
request); the table and local indexes do.

### Pagination

```python
page = await entries.query(account_id="a1").page_size(50).page()
page.items  # up to 50 items
page.cursor  # opaque string, None on the last page

next_page = await entries.query(account_id="a1").page_size(50).after(page.cursor).page()
```

Cursors are URL-safe but **not signed**: they encode the last key read. Do not trust cursors
from untrusted clients for authorization decisions; re-check access on every page (the query's
partition key must come from your own logic, not from the cursor).

`page_size(n)` is DynamoDB's `Limit` (items *evaluated* per request, before filters); `limit(n)`
caps the items returned in total, and `page()` evaluates at most `limit` items too. A filtered page
can be empty while `cursor` is set. `pages()` cuts its last page at the limit, with a cursor right
after the page's last item. `first()` and `limit()` read only what they need, also for models with
an entity type or unique attributes (whose reads carry a filter dynamoxy adds).

### Scans

```python
async for user in users.scan().filter(User.visits > 100):  # reads the whole table
    ...
everyone = await users.scan().segments(4).all()  # parallel scan
part = users.scan().segment(0, 8)  # one segment of a scan spread over workers
```

### Conditions and paths

Model attributes are typed paths: `User.visits > 3`, `User.name.begins_with("A")`,
`User.tags.contains("admin")`, `User.age.between(18, 65)`, `User.status.is_in([...])`,
`User.email.exists()`, `User.age.not_exists()`, `User.tags.size() > 2`,
`User.address["city"] == "Lund"`. Combine with `&`, `|` and `~` (or `dynamoxy.and_`, `or_`,
`not_`). Values are validated and encoded as the field stores them, also the element of
`contains()` (`Bird.seen.contains(moment)` on a `list[datetime]`). Like in DynamoDB, a comparison with a missing attribute is false (except `!=`): items
whose `tags` set is empty store no `tags` attribute, so select them with `User.tags.not_exists()`
or `User.tags == set()`, not `User.tags.size() == 0`. `None` is no attribute for model fields:
`User.age == None` matches items without `age`, or with a `NULL` (which reads as `None` too).
`dynamoxy.F("raw", "path")` builds untyped paths for dict-level code.
`dynamoxy.condition("#s = :s", names={"#s": "status"}, values={":s": "x"})` is the escape
hatch for hand-written expressions.

## 5. Writing

```python
user = await User.create(client, user_id="u2", email="bob@example.com", name="Bob")  # new key only
# or, with an instance: user = await users.create(User(user_id="u2", ...))
await users.put(user)  # create or replace
user.name = "Robert"
await users.save(user)  # replace, but only if nobody changed it since it was loaded

await users.update(user, User.visits.add(1), User.tags.add({"vip"}))  # atomic, no read
await users.update(User.key("u2"), User.name.set("Bob"))  # by key, without an instance
await users.update(user, User.visits.set(User.visits + 10), condition=User.visits < 100)
await stats.update(PageStats.key("home"), PageStats.views.add(1), upsert=True)  # creates it when missing

deleted = await users.delete(user)  # returns the deleted item, or None
await users.delete("u2", condition=User.visits == 0)  # the condition fails for a missing item too
```

Update actions: `set(value or path or arithmetic)`, `set_if_not_exists(value)`, `add(n or set)`,
`increment(n)`, `decrement(n)`, `remove()`, `delete(set)` (alias `discard`), `append(list)`,
`append_or_create(list)`, `prepend(list)`. Nested paths work: `User.address["city"].set("Lund")`.
`User.nickname.set(None)` and `User.tags.set(set())` remove the attribute (as `put()` stores
nothing for `None` and empty sets). Numbers are checked against the field
(`User.visits.increment(0.5)` raises `ValidationError`). Updates refresh the instance in place
with the stored result, and re-render templated attributes whose fields change, `updated_at` and
`version` included. Key, derived (templated) and immutable attributes cannot be updated, nor the
fields a key template uses. Keys can be stored keys or field values
(`devices.update({"device_id": uuid}, ...)`).

`User.create(client, ...)` validates its values like the constructor (type checkers check
them too) and writes through `client.model(User)`, a plain repository: code that relies on the
overrides of a repository subclass calls the subclass's `create()`. The client comes first, so
a field may be named `client`.

Every write of a repository takes `condition=`; when it does not hold,
`ConditionalCheckFailedError` is raised with `.item` set to the stored item.

`upsert=True` creates the item when it is missing, so the result must be a complete item: fields
the actions leave alone get their defaults (and existing items keep their values), derived keys
and the entity type are filled in, and every required field without a default must be set by the
actions, typically with `set_if_not_exists(...)`. Otherwise dynamoxy raises `ValueError` before
any I/O. `X | None` and set fields of key targets get no default, since `if_not_exists` would
replace an existing item's `None` or empty set: a created item reads those unless the actions set
them. For templated keys, pass the key as field values
(`orders.update({"customer_id": "c1", "order_id": "o1"}, ..., upsert=True)`).

### Writing through an instance

An instance that a repository read or wrote (`get`, queries, scans, `create`, `save`,
transactions, ...) remembers that repository and writes itself through it, with the same
semantics (validation, hooks, versions, unique guards, overrides of repository subclasses):

```python
user = await users.get_or_raise("u1")
user.name = "Robert"
await user.save()  # users.save(user): create or replace, version-checked
await user.increment(User.visits)  # users.update(user, User.visits.increment()); a missing number counts as 0
await user.decrement(User.visits, 2, condition=User.visits >= 2)
await user.add(User.tags, "vip")  # a set element, or a set of them
await user.update(User.name.set("Bob"), User.tags.delete({"vip"}))  # any update actions
await user.refresh()  # reload in place
deleted = await user.delete()  # users.delete(user)
```

`add(path, value)` adds to numbers, adds set elements and appends one element to lists (creating
missing lists). `add`, `increment` and `decrement` take nested paths too
(`player.increment(Player.stats["wins"])`) and validate the value against the attribute before
any I/O. New instances, `copy()` results and unpickled instances are not bound: write them with
a repository first (`await users.create(user)`, or build and write in one call with
`User.create(client, ...)`); their methods raise `RuntimeError` before any I/O. Instance
updates check the version of versioned models; update hot counters by key instead.

### Batches

```python
await entries.batch_put(new_entries)  # any number; chunked, sent concurrently, unprocessed items retried
await entries.batch_delete([("a1", 1), ("a1", 2)])  # keys (partition, sort), instances or key dicts

async with client.batch_writer() as batch:  # buffers and flushes every 100 operations
    for event in events_to_store:
        await batch.put(event)
    await batch.delete(Event.key("s", 1), model=Event)
```

Batch writes are unconditional and not atomic, and DynamoDB allows no two writes to the same key
in one request: `batch_put()`, `batch_delete()` and the batch writer keep the last one. They are
refused for models with unique attributes (guard items need transactions). If items stay
unprocessed after every retry round, `UnprocessedItemsError.unprocessed` lists them; only the
writes that were made update their instances and run `after_*` hooks. A batch writer flush that
fails before its request (a hook or validation raised), or whose request fails, keeps the other
writes queued. A block that raises writes nothing more; `await batch.flush()` writes what is still
queued.

A write of a loaded instance (`save()`, `put()`, batch puts) keeps the stored attributes that no
field declares (written by a newer version of the application during a rolling deploy, or by
another service); remove one with `update(item, F("old_name").remove())`. Documents read from an
item keep the map keys no field declares too; a new document replaces the map.

## 6. Unique values

```python
class Member(dynamoxy.Model, table="members"):
    member_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
```

dynamoxy stores a guard item per unique value (`_unique#Member#email#<value>`) in the same table
and writes it in the same transaction as the item. Creates, saves, puts, updates and deletes
keep the guards consistent; a taken value raises `UniqueConstraintError` (`.field`, `.value`).

- Change unique values with `set(...)` or `remove()` (or `set(None)`; dynamoxy needs the new
  value). Numbers are guarded as DynamoDB stores them: `5`, `5.0` and `Decimal("5.00")` collide.
  Only strings, numbers, binary values and booleans can be unique (not sets, lists or maps).
- Writes of unique models are transactions: twice the write capacity, plus one guard write per
  changed value. Batch writes are refused for them.
- When another writer changed the unique values of a loaded instance, `put()`, and `save()`,
  `update()` and `delete()` of unversioned models without a `condition=`, read the stored item
  and write again. In a transaction, or for an upsert (`update(..., upsert=True)`), such a write
  raises `ConditionalCheckFailedError` instead: `refresh()` the instance and retry.
- Guard items are hidden from repository queries, scans and counts; dict-level scans see them.
- Writing such items with other tools bypasses the guards: route writes through dynamoxy.
  Items stored before a field became `unique=True` have no guards either. A guard is only
  deleted by a write of the item that owns it: when two items hold one value, writes that would
  remove the value of the other one raise `UniqueConstraintError` ("belongs to another item").

## 7. Optimistic locking

With a `version()` field, every write of a loaded instance checks that the stored version is the
one it was loaded with:

```python
async def rename(user_id: str, name: str) -> User:
    user = await users.get_or_raise(user_id)
    for _ in range(3):
        user.name = name
        try:
            return await user.save()
        except dynamoxy.VersionConflictError:
            await user.refresh()  # somebody else saved it meanwhile: reload and re-apply
    raise RuntimeError("too much contention")
```

`update()` of an instance checks the version too; `update()` of a key does not (the atomic
actions are safe without it). Versions are random tokens (`Attr[str]`), not counters: an instance
loaded before the item was replaced or deleted and created again never matches them.

## 8. Transactions

```python
async with client.transaction() as tx:  # commits when the block ends without an exception
    tx.create(order)
    tx.update(account, Account.balance.add(-order.total), condition=Account.balance >= order.total)
    tx.check(Inventory, Inventory.key("sku-1"), Inventory.stock > 0)
    tx.delete(cart)
    tx.put_item("audit", {"pk": "order#o1", "sk": "created"})  # dict-level operations work too
```

Model operations behave exactly like their repository counterparts (validation, hooks, versions,
timestamps, unique guards, derived keys). Updated instances are refreshed after the commit.
Limits: 100 operations (guard items count), 4 MB, and one operation per item.

```python
try:
    async with client.transaction() as tx:
        ...
except dynamoxy.TransactionCanceledError as error:
    for reason in error.cancellation_reasons:
        if reason.failed:
            print(reason.operation, reason.code, reason.error)  # e.g. "create Order", ItemAlreadyExistsError
```

`error.errors` holds the specific errors (`ItemAlreadyExistsError`, `VersionConflictError`,
`UniqueConstraintError`).

dynamoxy's own retries of a commit reuse its idempotency token, so a retried commit is applied
once. To make *your* retries idempotent (after a timeout, say), pass
`client.transaction(token=transfer_id)` (1 to 36 characters, unique across the account, e.g. a
UUID): an identical transaction with the same token succeeds
without being applied again for 10 minutes, and a different one raises
`IdempotentParameterMismatchError`. Automatic fields (`created_at()`, `updated_at()`) change
between attempts, so for such models make the transaction itself refuse duplicates, for example
with `tx.create()` of an item keyed by the operation id.

Consistent multi-item reads:

```python
customer, order = await client.transact_get(
    dynamoxy.Get(Customer, customer_id="c1"),
    dynamoxy.Get(Order, customer_id="c1", order_id="o1"),
)
```

## 9. Single-table design

Several models can share a table. Give each an `entity=` (stored in the `_type` attribute) and
key templates:

```python
class Customer(Model, table="app", entity="Customer"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="PROFILE")
    customer_id: Attr[str]
    name: Attr[str]


class Order(Model, table="app", entity="Order"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="ORDER#{order_id}")
    customer_id: Attr[str]
    order_id: Attr[str]
    status: Attr[str] = field(default="new")
    placed_at: Attr[datetime]
    gsi1pk: Attr[str] = field(template="STATUS#{status}")
    gsi1sk: Attr[str] = field(template="{placed_at}")

    by_status = GlobalIndex("gsi1", partition_key=gsi1pk, sort_key=gsi1sk)
```

- `orders.query(customer_id="c1")` queries `pk = "CUSTOMER#c1"` and adds
  `begins_with(sk, "ORDER#")` automatically, so only orders come back. A `where()` condition
  replaces it (`.where(Order.sk.begins_with("ORDER#2026"))`); the entity type keeps other models
  out.
- Templates render numbers as stored (`Decimal("1.50")` → `1.5`) and format datetimes with a spec
  (`{placed_at:%Y-%m}`) in UTC, so items read back render the same keys.
- `orders.query(index=Order.by_status, status="paid")` renders the index key from the template.
- Updating `status` recomputes `gsi1pk` (dynamoxy reads the item first when the template needs
  fields the update does not set, and requires them unchanged when it writes: it reads again if
  another writer changed them).
- `client.collection(Customer, Order).query(customer_id="c1").all()` returns both entity types,
  decoded by `_type`.
- A subclass (`class Admin(Member, entity="Admin")`) has its own repository: `client.model(Member)`
  raises `TypeError` for an `Admin` instance and `ItemDecodeError` when it reads an `Admin` item.
  Document fields refuse instances of subclasses of their document class.
- `client.create_tables(Customer, Order)` creates the shared table once, with every index.

## 10. Tables

```python
await client.create_tables(User, Event)  # ResourceInUseError if one exists (exist_ok=True skips)
await client.ensure_tables(User, Event)  # creates what is missing: tables, global indexes, TTL
await client.delete_tables(Event)

table = client.table("events")  # logical name; table_prefix/table_names apply
await table.describe()  # TableDescription: keys, indexes, billing, stream, ...
await table.update_settings(billing=dynamoxy.Billing.provisioned(5, 5), stream="NEW_AND_OLD_IMAGES")
await table.update_settings(deletion_protection=True)
await table.enable_time_to_live("expires_at")
await table.tag({"team": "payments"})
await table.enable_point_in_time_recovery()
backup = await table.create_backup("before-migration")
await client.table("events-restored").restore_backup(backup.arn)
await table.restore_to("events-yesterday", at=some_datetime)
```

`ensure_tables()` never deletes or changes keys; it raises `TypeError` when a table's key, the key
of an index with the same name or the TTL attribute differs from the models (create a new table
or index and migrate). Local indexes can only be created with the table. In production, prefer
infrastructure as code for tables and call `ensure_tables()` only in development and tests.

`Client(table_prefix="dev-")` separates environments sharing an account (`users` →
`dev-users`, for every operation including `create_tables()`); read the prefix from settings
rather than putting environment names in models. `table_names={"users": "legacy_users"}` maps
individual logical names, used as given without the prefix.

## 11. The dict-level API and PartiQL

For data without a model (migrations, admin tools, other teams' tables):

```python
from dynamoxy import F

table = client.table("legacy")
await table.put({"pk": "a", "sk": 1, "payload": {"x": 1}}, condition=F("pk").not_exists())
item = await table.get({"pk": "a", "sk": 1})
await table.update({"pk": "a", "sk": 1}, F("payload", "x").add(1))
rows = await table.query(F("pk") == "a", F("sk").between(1, 10)).filter(F("payload").exists()).all()
await table.batch_write(puts=items, deletes=keys)

rows = await client.execute('SELECT * FROM "legacy" WHERE pk = ?', "a")
rows = await client.execute('SELECT * FROM "legacy" WHERE pk IN [?, ?] ORDER BY pk, sk DESC', "a", "b")
await client.execute_transaction([("INSERT INTO \"legacy\" VALUE {'pk': ?, 'sk': ?}", ["b", 1])])
```

Values are plain Python values (`str`, `int`, `Decimal`, `bytes`, `bool`, `None`, `list`,
`dict`, `set`). PartiQL statements take physical table names. `dynamoxy.to_wire()` and
`dynamoxy.from_wire()` convert items to and from DynamoDB's typed JSON (boto3, Streams, exports).

## 12. Application patterns

### Repositories as the data layer

Subclass `Repository` for each aggregate and give it intention-revealing methods. Services
depend on repositories, never on raw DynamoDB calls:

```python
class UserRepository(dynamoxy.Repository[User]):
    async def by_email(self, email: str) -> User | None:
        return await self.query(index=User.by_email, email=email).first()

    async def record_visit(self, user_id: str) -> User:
        return await self.update(User.key(user_id), User.visits.add(1))


class SignupService:
    def __init__(self, users: UserRepository) -> None:
        self.users = users

    async def sign_up(self, user_id: str, email: str, name: str) -> User:
        try:
            return await self.users.create(User(user_id=user_id, email=email, name=name))
        except dynamoxy.UniqueConstraintError:
            raise EmailTakenError(email) from None
```

Build repositories once from the shared client: `UserRepository(client)`. Instances read
through a repository subclass write through it too (`await user.save()` calls
`UserRepository.save`), so overrides for auditing or tenant checks apply to them.

### Unit of work

Collect the writes of one business operation in a transaction, and let the repositories plan
them:

```python
async def place_order(client: dynamoxy.Client, order: Order, account: Account) -> None:
    async with client.transaction() as tx:
        tx.create(order)
        tx.update(account, Account.balance.add(-order.total), condition=Account.balance >= order.total)
```

### Web applications (FastAPI or similar)

Create the client in the application's lifespan and share it:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with dynamoxy.Client() as client:
        app.state.users = UserRepository(client)
        yield


app = FastAPI(lifespan=lifespan)
```

Measure what a request costs with `dynamoxy.collect()` (section 15) and expose it in logs.

### Idempotent consumers

For queue consumers that may see a message twice, make the write conditional on the message id:
`create()` of an item keyed by the message id, or
`update(..., condition=Entry.last_message_id != message_id)`.

### Migrations of stored data

Add new fields with defaults (old items decode with the default; `X | None` and set fields read
them as `None` and an empty set, so backfill the default with `set_if_not_exists()` if they need
it). Upgrade old shapes in `@dynamoxy.after_load`, and rewrite items in the background with a scan
and `save()`. `save()` keeps attributes no field declares, so instances of an older version of the
application do not erase what a newer one wrote; drop a retired attribute with
`update(item, F("old").remove())`.
Changing a field's type: items stored with the old type raise `ItemDecodeError`, so rewrite them
first (`repository.table.scan()` returns dicts), or declare both types (`Attr[int | str]`) until
every item is rewritten. Changing a key or a key template is a new table.

## 13. Configuration cheat sheet

| `Client(...)` option | Default | When to change it |
| --- | --- | --- |
| `region`, `profile`, `credentials` | AWS configuration chain | Explicit environments; prefer roles over static keys |
| `endpoint_url` | AWS | DynamoDB Local, emulators, VPC endpoints |
| `timeout` | `10.0` | Time limit of one attempt (seconds) |
| `connect_timeout` | `3.1` | Connection establishment |
| `retry` | `RetryPolicy(max_attempts=4, base_delay=0.05, max_delay=5.0)` | `RetryPolicy.none()` for latency-critical paths; more attempts for batch jobs |
| `table_prefix`, `table_names` | none | Environments sharing an account |
| `consumed_capacity` | `"TOTAL"` | `"INDEXES"` for per-index costs, `"NONE"` to skip reporting |
| `batch_concurrency`, `batch_max_rounds` | `4`, `8` | Large batch jobs |
| `slow_operation_threshold` | none | Emit `request.slow` events for slow calls |
| `telemetry` | `Telemetry()` | `None` to disable OpenTelemetry spans and metrics |
| `logging` | every event to `dynamoxy.*` loggers | Levels per kind, disabled kinds, structured fields |
| `event_buffer` | `10000` | Very bursty hooks |

Retries cover throttling, server errors, transaction conflicts and timeouts, with exponential
backoff and full jitter. Conditional and validation failures are never retried. A write whose
attempt timed out, lost its connection or got a 5xx may have been applied: it is sent again
only when it applies once even when sent twice (transactions, puts, deletes, `SET` updates).
Model writes store a random write id (`_wid`) and recognise their own earlier attempt by it, so
a timed-out `create()`, `save()` or `increment()` that was applied succeeds, applied once.
Otherwise (another writer wrote in between, deletes, the dict-level API, models declared with
`write_ids=False`) the call raises `WriteOutcomeUnknownError`, never a conflict error, and so
does a write whose attempts ran out after one that may have been applied (never a retryable
`ThrottlingError` or `InternalServerError`).

### Credentials and region

Without `credentials=`, the AWS SDK's default credential chain is used; the first source that
provides credentials wins:

1. `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN`) in the environment
   (AWS Lambda provides its role this way).
2. `~/.aws/credentials` and `~/.aws/config` for `profile=`, else `AWS_PROFILE`, else `default`
   (SSO, `credential_process` and assume-role profiles work).
3. Web identity: `AWS_WEB_IDENTITY_TOKEN_FILE` with `AWS_ROLE_ARN` (EKS IRSA).
4. Container credentials (ECS task roles, EKS Pod Identity).
5. EC2 instance metadata (instance profiles).

- `credentials=dynamoxy.Credentials(...)` replaces the chain entirely; reserve it for emulators
  and tests (`Credentials("test", "test")`), prefer roles in deployments.
- `profile=` only picks the profile of step 2: environment keys still win over it.
- Credentials are resolved on the first request; missing credentials raise `CredentialsError`
  there, not when the client is created.
- Region: `region=`, else `AWS_REGION`, else `AWS_DEFAULT_REGION`, else the profile's `region`,
  else EC2 instance metadata. None found → `ConfigurationError` on connect, except with
  `endpoint_url`, where `us-east-1` is used.

## 14. Errors

Every error derives from `dynamoxy.DynamoxyError` and has `.retryable`. Service errors carry
`.code`, `.message`, `.operation`, `.request_id`, `.status_code`, `.attempts`, `.throttles`,
`.requests` and `.duration`.

| Error | Raised when | What to do |
| --- | --- | --- |
| `ValidationError` | A value or request is invalid (before any I/O for models); `.errors` lists fields | Fix the input |
| `ItemNotFoundError` | `get_or_raise()`, `one()`, or `update()` of a missing item | 404 or create it |
| `ItemAlreadyExistsError` | `create()` of an existing key | Conflict; choose another key |
| `VersionConflictError` | `save()`/`update()`/`delete()` of a stale instance | Reload, re-apply, retry |
| `UniqueConstraintError` | A unique value is taken (`.field`, `.value`) | Conflict; ask for another value |
| `ConditionalCheckFailedError` | Your `condition=` failed (`.item` is the stored item) | Business rule violated |
| `TransactionCanceledError` | A transaction was canceled; see `.cancellation_reasons`, `.errors` | Inspect reasons |
| `TransactionConflictError` | Concurrent transactions on the same items (retried first) | Retry later |
| `ThrottlingError` | Throughput exceeded after every retry | Back off; raise capacity |
| `ResourceNotFoundError` | The table or index does not exist | Create it (`ensure_tables`) |
| `ResourceInUseError` | The table exists or is being changed | Wait or skip |
| `UnprocessedItemsError` | A batch still had unprocessed items after every round | Retry `.unprocessed` later |
| `ItemDecodeError` | A stored item does not match its model (another type, a fraction in an `int`) | Fix the data, or declare the stored types (`int \| str`) |
| `TransportError` | Network failure or timeout (retried first) | Retry later |
| `WriteOutcomeUnknownError` | A write may have been applied, and no answer said whether (a `TransportError`; `.item`) | Read the item, then decide; never repeat an increment blindly |
| `CredentialsError`, `AccessDeniedError` | Missing credentials or IAM permissions | Fix configuration |
| `ShutdownError` | The client was closed, or the interpreter is exiting | Create a new client |

## 15. Observability

### Events and logging

Events are structured and logged to `dynamoxy.<category>` loggers (`dynamoxy.request`,
`dynamoxy.batch`, `dynamoxy.table`, `dynamoxy.client`, `dynamoxy.operation`, ...). Every log
record carries the event as `record.dynamoxy`. Run `dynamoxy events` to list every kind.

```python
client.on("request.throttled", lambda event: metrics.increment("dynamodb.throttled"))


@client.on("operation.failed")
async def report(event: dynamoxy.Event) -> None:  # sync or async; failures never break dynamoxy
    await alerts.send(event.message, event.fields)


client = dynamoxy.Client(logging=dynamoxy.LoggingConfig(levels={"operation.*": "debug"}, disabled=("client.*",)))
```

Useful kinds: `request.retry`, `request.throttled`, `request.gave_up`, `request.slow`,
`batch.unprocessed`, `batch.incomplete`, `operation.failed`, `hook.failed`, `events.dropped`.

### OpenTelemetry

With `opentelemetry-api` installed (`pip install 'dynamoxy[otel]'`) and an SDK configured by the
application, every call becomes a `CLIENT` span (`PutItem users`) with the database semantic
conventions (`db.system.name=aws.dynamodb`, `db.operation.name`, `db.collection.name`,
`aws.dynamodb.*`), and metrics are recorded: `db.client.operation.duration`,
`db.client.response.returned_rows`, `dynamoxy.consumed_capacity`, `dynamoxy.request.attempts`,
`dynamoxy.request.throttles`. Configure with `dynamoxy.Telemetry(...)` (providers, and
`record_expressions=True` to add expressions, never values, to spans).

### On demand

```python
with dynamoxy.collect() as usage:  # everything in the block, including tasks it starts
    await handle_request()
logger.info("dynamodb usage", extra=usage.summary())  # calls, capacity units, retries, errors

client.metrics()  # counters per operation since the client started: calls, errors, latency buckets
```

### Middleware

Middleware wraps every call and may inspect or change the request:

```python
async def tenant_guard(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
    if request.table is not None and not request.table.startswith(current_tenant()):
        raise PermissionError(request.table)
    return await call_next(request)


client.use(tenant_guard)  # returns a function that removes it
```

## 16. Testing

```python
import pytest

import dynamoxy
from dynamoxy import testing


@pytest.fixture
async def client():
    async with testing.memory_client() as client:
        await client.create_tables(User)
        yield client


async def test_signup(client: dynamoxy.Client) -> None:
    users = client.model(User)
    await users.create(User(user_id="u1", email="a@example.com", name="Ada"))
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await users.create(User(user_id="u2", email="a@example.com", name="Bob"))
```

- `testing.memory_client(store=...)`: clients sharing a `dynamoxy.MemoryStore` share data.
- `testing.inject_fault(client, operation="PutItem", error="throttling", times=2)`: throttling,
  server errors, latency (`latency=0.5`), unprocessed batch items (`unprocessed=True`) and
  lost responses of applied writes (`lose_response=True`).
- `testing.purge_expired(client)`: deletes expired TTL items, as DynamoDB eventually does.
- `testing.capture_events(client, "request.*")`: collects events in a block.
- Emulators (MiniStack, DynamoDB Local) work with `Client(endpoint_url=..., region="us-east-1",
  credentials=dynamoxy.Credentials("test", "test"))`.

## 17. Deployment, permissions and limits

- **Processes**: one client per process, created after forking (gunicorn/uvicorn workers,
  multiprocessing). The client works across event loops in one process.
- **Shutdown**: close the client (`async with`) so pending events reach hooks. Calls after
  interpreter shutdown raise `ShutdownError`.
- **IAM**: grant the operations you use on the table ARNs and their `/index/*`:
  `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`, `Scan`, `BatchGetItem`,
  `BatchWriteItem`, `ConditionCheckItem` and `TransactWriteItems`/`TransactGetItems` (unique
  attributes and transactions), `PartiQL*` for statements, `DescribeTable` and the table
  management operations for `ensure_tables()`.
- **Limits** (DynamoDB's): items up to 400 KB, together with their entries in local indexes;
  transactions up to 100 operations and 4 MB;
  queries and scans read up to 1 MB per page; batch writes 25 and batch gets 100 per request
  (dynamoxy chunks them); keys up to 2048 (partition) / 1024 (sort) bytes; numbers up to 38
  significant digits; lists and maps nested up to 32 levels; no empty sets.
- **Consistency**: global secondary indexes are eventually consistent (no `consistent=True` reads);
  a write is visible in a GSI shortly after it succeeds.
- **Hot partitions**: spread writes over many partition key values; a single partition serves
  about 3000 reads and 1000 writes per second.
- **Costs**: `consumed_capacity` and `dynamoxy.collect()` show what each request costs; unique
  attributes and transactions double write costs; filters do not reduce read costs.
