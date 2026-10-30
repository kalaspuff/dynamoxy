# Modeling

## Models and documents

- `dynamoxy.Model` subclasses with `table="..."` are stored as items.
- `dynamoxy.Document` subclasses are stored as maps inside items (addresses, line items).
- Both declare fields the same way and validate on construction and on assignment.
- A model without `table=` (or with `abstract=True`) is a base class: its fields are inherited.
- `item.copy(**changes)` makes a new, unsaved instance with the same values: automatic fields
  (`created_at`, `updated_at`, `version`) start empty, so saving the copy writes a new item
  (pass `created_at=item.created_at` to keep one); an unknown field name raises `TypeError`.
  `copy.copy(item)`, `copy.deepcopy(item)` and pydantic's `model_copy()` make exact duplicates
  that still know the item they mirror.

```python
class Timestamped(dynamoxy.Model, abstract=True):
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()


class Article(Timestamped, table="articles"):
    article_id: Attr[str] = partition_key()
    title: Attr[str] = field(min_length=1, max_length=300)
```

## Fields and codecs

The annotation selects a codec. The full table is in the [usage guide](../../python/dynamoxy/AGENT_GUIDE.md#fields);
the important rules:

- Numbers are stored exactly. `int` reads back as `int`, `Decimal` as `Decimal`, `float` as
  `float`. NaN and infinity are rejected; DynamoDB keeps 38 significant digits.
- Reading is exact: a stored value of another type, or one the field's type cannot hold exactly
  (`2.5` in an `int` field, `"false"` in a `bool` field, a list in a `set` field), raises
  `ItemDecodeError` instead of being converted, since the next write would store the converted
  value ([ADR 0015](../adr/0015-stored-values-decode-exactly.md)). Unions (`int | str`) read the
  types they name.
- `datetime` values must be timezone-aware; they are stored in UTC as fixed-width ISO 8601 text
  (`2026-01-02T03:04:05.000000+00:00`, or `2026-01-02T03:04:05.000000Z` with
  `datetime_format="iso_z"`), so string order is time order and sort keys and `BETWEEN` work.
  Epoch seconds or milliseconds are possible too (see [timestamp formats](#timestamp-formats)).
- Empty sets are not stored (DynamoDB has no empty sets) and read back as empty sets.
- `X | None` fields store nothing for `None`; `REMOVE` and `None` are the same.
- Enums store their value; `Literal["a", "b"]` restricts strings (or numbers). A `str`, `int`
  or `Literal` field holds a plain `str` or `int`: a member of a `(str, Enum)` or `(int, Enum)`
  becomes its value, so keys, templates and unique values render what DynamoDB stores.
- Nested `Document`s and pydantic models are maps; `list[Document]` works too.

Custom storage: pass `codec=` (or `Annotated[T, dynamoxy.UseCodec(codec)]`). `JsonCodec` stores
any JSON-compatible structure as one string. A codec implements `encode`, `decode`, `validate`
and `key_type` (see `dynamoxy.codecs.Codec`).

## Constraints and validators

```python
class Product(dynamoxy.Model, table="products"):
    sku: Attr[str] = partition_key(pattern=r"[A-Z]{3}-\d{4}")
    name: Attr[str] = field(min_length=1, max_length=120)
    price: Attr[Decimal] = field(gt=0)
    stock: Attr[int] = field(default=0, ge=0)
    tags: Attr[set[str]] = field(default_factory=set, max_length=20)
    slug: Attr[str] = field(validators=(str.lower,))  # validators may normalize

    @dynamoxy.validator
    def check_name(self) -> None:
        if self.name.lower() in self.tags:
            raise ValueError("the name cannot be a tag")
```

Defaults apply to new instances and to stored items without the attribute (written before the
field existed). A mutable default (`field(default=[])`, a document) is copied for each instance,
as in pydantic; `default_factory=` works too.

Invalid values raise `dynamoxy.ValidationError`; `.errors` lists every `FieldError` (`path`,
`message`, `value`). Validation runs on construction, on attribute assignment, and before every
write, so nothing invalid reaches DynamoDB. Values set by `update()` are validated the same way
(`User.age.set(-1)` fails before I/O); arithmetic updates need a condition instead. Reading never
fails on constraints (stored data may predate them), only on values of the wrong type
(`ItemDecodeError`).

## Automatic fields

| Specifier | Behaviour |
| --- | --- |
| `created_at()` | Set on the first write (`create`, `put` of a new item, upserts) |
| `updated_at()` | Set on every write, including `update()` |
| `version()` | 1 on the first write, +1 on every write; enables optimistic locking |
| `ttl(default=None)` | When DynamoDB may delete the item (stored as epoch seconds); `ensure_tables()` enables TTL |

They are not constructor arguments (type checkers know this too) and are `None` until the
instance is written. Timestamps use the model's `datetime_format` unless they pass `format=`.

## Timestamp formats

`datetime` values are stored in UTC in one of four formats:

| Format | Stored as | Example |
| --- | --- | --- |
| `"iso"` (default) | `S` | `2026-10-13T08:48:47.966937+00:00` |
| `"iso_z"` | `S` | `2026-10-13T08:48:47.966937Z` |
| `"epoch"` | `N` | `1791881327` |
| `"epoch_ms"` | `N` | `1791881327966` |

Both ISO formats always have microseconds, so they are fixed width and text order is time order
(sort keys, `between()`). `"iso_z"` is the RFC 3339 style with a `Z` suffix that most other
languages and services write for UTC. Choose the format once for a model and its subclasses with
the `datetime_format` class keyword. It applies to every `datetime` field (also inside lists,
sets and maps) and to `created_at()`/`updated_at()`:

```python
class Base(dynamoxy.Model, abstract=True, datetime_format="iso_z"):
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()


class Reading(Base, table="readings"):
    sensor_id: Attr[str] = partition_key()
    taken_at: Attr[datetime] = sort_key()  # stored as "2026-10-13T08:48:47.966937Z"
```

- A field can still choose for itself: `created_at(format="epoch_ms")`,
  `field(codec=DateTimeCodec("iso"))` or
  `Attr[Annotated[datetime, dynamoxy.UseCodec(DateTimeCodec("iso_z"))]]` (`DateTimeCodec` is in
  `dynamoxy.codecs`). TTL attributes are always epoch seconds.
- A nested `Document` has its own format: declare it there too
  (`class Address(dynamoxy.Document, datetime_format="iso_z")`). Plain pydantic models nested in a
  model use the `Annotated` form.
- Reading accepts ISO text in either style whatever the format: `Z` or an offset, with or without
  fractional seconds. Items written by other services, or before a format change, decode.
- Epoch formats are exact: a datetime is stored as the whole seconds or milliseconds since 1970
  before it (rounded down, also before 1970), and read back unchanged.
- Keys, key templates, conditions and unique values compare the stored text: a sort key stored as
  `...+00:00` is not found with a key built as `...Z`. Keep one format per attribute, and change
  the format of a key or templated attribute like a key (see
  [evolving data](patterns.md#evolving-data)).

## Keys

Every model has a `partition_key()` and optionally a `sort_key()`. Key values are strings,
numbers or bytes. Keys are immutable: changing a key means writing a new item and deleting the
old one (in a transaction if both must happen). A write of a loaded instance whose key changed
(also through a field its key template uses) raises `ValueError`.

Keys can be generated (`partition_key(default_factory=lambda: uuid4().hex)`) or built from other
fields with templates (`partition_key(template="USER#{user_id}")`, see
[single-table design](single-table-design.md)).

## Secondary indexes

```python
class Order(dynamoxy.Model, table="orders"):
    order_id: Attr[str] = partition_key()
    customer_id: Attr[str]
    status: Attr[str] = field(default="new")
    placed_at: Attr[datetime]

    by_customer = dynamoxy.GlobalIndex(partition_key=customer_id, sort_key=placed_at)
    by_status = dynamoxy.GlobalIndex(partition_key=status, projection=dynamoxy.Projection.keys_only())
```

- `GlobalIndex`: any partition and sort key; eventually consistent; can be added later.
- `LocalIndex`: same partition key, another sort key; strongly consistent reads possible; only at
  table creation; limits item collections to 10 GB.
- Projections: `Projection.all()` (default), `keys_only()`, `include(...)`. Model queries of a
  global index that does not hold every field need `.hydrate()` (full items) or `.project(...)`
  (dicts), and raise `ValueError` otherwise; local indexes read the missing attributes from the
  table (see [indexes and projections](reading-data.md#indexes-and-projections)).
- Indexes are sparse: items without the index key attributes are not indexed.
- Fields declared without a specifier have no class attribute yet while the class body runs:
  name them as strings (`GlobalIndex(partition_key="kind")`).

## Lifecycle hooks

Decorated model methods run around persistence, sync or async (`after_load` hooks are sync):

```python
class Customer(dynamoxy.Model, table="customers"):
    customer_id: Attr[str] = partition_key()
    email: Attr[str]
    visits: Attr[int] = field(default=0)

    @dynamoxy.before_save
    def normalize(self) -> None:  # may change the instance; raising aborts the write
        self.email = self.email.strip().lower()

    @dynamoxy.after_save
    async def announce(self) -> None:
        await outbox.publish("customer.saved", self.customer_id)

    @dynamoxy.after_update
    def forget(self) -> None:  # the refreshed instance, after update(), add(), increment(), ...
        cache.pop(self.customer_id, None)
```

| Hooks | Run for | Notes |
| --- | --- | --- |
| `before_save`, `after_save` | `create`, `put`, `save` (`customer.save()` too), batch puts, transactions | `before_save` may change the instance, and runs before validation |
| `before_update`, `after_update` | `update()` of an instance, in repositories and transactions, and the instance's `update()`, `add()`, `increment()`, `decrement()` | the update writes its actions only, so changes `before_update` makes to the instance are not stored; `after_update` sees the refreshed instance |
| `before_delete`, `after_delete` | `delete()` of an instance (`customer.delete()` too), batch deletes, transactions | |
| `after_load` | every item a repository decodes | migrate old shapes in memory |

Raising in a `before_*` hook aborts the write (in a transaction, the whole transaction). Writes
by key (`customers.update(Customer.key("c1"), ...)`, `customers.delete("c1")`) have no instance
and run no hooks. Client-level hooks (`@client.hook("after_update", Customer)`, or without a
model for every model) receive `(instance, HookContext)` (`operation`, `model`, `client`), run
after the model's own and suit cross-cutting concerns such as auditing.

## Names to avoid

Field names cannot shadow the model API: `key`, `primary_key`, `is_loaded`, `to_item`,
`from_item`, `create`, `get`, `query` and the instance methods `save`, `delete`, `update`, `add`,
`increment`, `decrement`, `refresh`, in native and [pydantic](pydantic.md#existing-models)
models alike. Native models also reserve `validate`, `copy` and `to_dict`; in pydantic models,
pydantic's own names (`copy`, `validate`, ...) follow pydantic's rules. Use another Python name
and keep the stored name with `attribute_name=`:

```python
class Search(dynamoxy.Model, table="searches"):
    search_id: Attr[str] = partition_key()
    search_query: Attr[str] = field(attribute_name="query")  # stored as `query`
```

Pydantic models use `Annotated[str, dynamoxy.AttributeName("query")]`. Queries, conditions and
updates name the field (`Search.search_query == "x"`), and the dict-level API sees `query`.
Documents only reserve `validate`, `copy`, `to_dict`, `to_item` and `from_item`. DynamoDB
reserved words (`name`, `status`, `data`, ...) are fine.
