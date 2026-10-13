# Modeling

## Models and documents

- `dynamoxy.Model` subclasses with `table="..."` are stored as items.
- `dynamoxy.Document` subclasses are stored as maps inside items (addresses, line items).
- Both declare fields the same way and validate on construction and on assignment.
- A model without `table=` (or with `abstract=True`) is a base class: its fields are inherited.

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
- `datetime` values must be timezone-aware; they are stored in UTC as fixed-width ISO 8601 text
  (`2026-01-02T03:04:05.000000+00:00`, or `2026-01-02T03:04:05.000000Z` with
  `datetime_format="iso_z"`), so string order is time order and sort keys and `BETWEEN` work.
  Epoch seconds or milliseconds are possible too (see [timestamp formats](#timestamp-formats)).
- Empty sets are not stored (DynamoDB has no empty sets) and read back as empty sets.
- `X | None` fields store nothing for `None`; `REMOVE` and `None` are the same.
- Enums store their value; `Literal["a", "b"]` restricts strings (or numbers).
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
- Keys, key templates, conditions and unique values compare the stored text: a sort key stored as
  `...+00:00` is not found with a key built as `...Z`. Keep one format per attribute, and change
  the format of a key or templated attribute like a key (see
  [evolving data](patterns.md#evolving-data)).

## Keys

Every model has a `partition_key()` and optionally a `sort_key()`. Key values are strings,
numbers or bytes. Keys are immutable: changing a key means writing a new item and deleting the
old one (in a transaction if both must happen).

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
- Projections: `Projection.all()` (default), `keys_only()`, `include(...)`. Queries on partial
  projections return partially filled instances unless you call `.hydrate()` or `.project(...)`.
- Indexes are sparse: items without the index key attributes are not indexed.
- Fields declared without a specifier have no class attribute yet while the class body runs:
  name them as strings (`GlobalIndex(partition_key="kind")`).

## Lifecycle hooks

Model methods decorated with `@dynamoxy.before_save`, `after_save`, `before_delete`,
`after_delete` (sync or async) and `after_load` (sync) run around persistence. Client-level hooks
(`@client.hook("before_save", User)`) receive `(instance, HookContext)` and suit cross-cutting
concerns such as auditing. `before_*` hooks may change the instance or raise to abort the write.

## Names to avoid

Field names cannot shadow the model API: `key`, `primary_key`, `is_loaded`, `validate`, `copy`,
`to_dict`, `to_item`, `from_item`. Use another Python name and `attribute_name="key"` to keep
the stored name. DynamoDB reserved words (`name`, `status`, `data`, ...) are fine.
