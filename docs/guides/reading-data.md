# Reading data

## From the model or its repository

Models read their own items, with the client as the first argument:

```python
user = await User.get(client, "u1")                           # None when missing
order = await Order.get(client, customer_id="c1", order_id="o1", consistent=True)
recent = await Order.query(client, customer_id="c1").descending().limit(10).all()
ada = await User.query(client, index=User.by_email, email="ada@example.com").first()
```

`User.get(client, ...)` is `client.model(User).get(...)` and `User.query(client, ...)` is
`client.model(User).query(...)`: the same arguments, the same results, and instances bound to
`client.model(User)`, so their `save()`, `update()` and `delete()` work. Type checkers know the
results: `User | None` and `Query[User]`. Everything else on this page (`get_or_raise`,
`batch_get`, `scan`, `count`, ...) is a method of the repository, `users = client.model(User)`,
or of your own repository subclass. `client.model(User)` returns the same repository every time,
so calling it where you need it costs a dictionary lookup.

## Single items

```python
user = await users.get("u1")                       # None when missing
user = await users.get_or_raise("u1")              # ItemNotFoundError
user = await users.get("u1", consistent=True)      # strongly consistent (2x read cost)
order = await orders.get(customer_id="c1", order_id="o1")
found = await users.batch_get(["u1", "u2"])        # any number of keys, order kept, missing skipped
await users.refresh(user)                          # reload in place (consistent by default)
```

## Consistency

Reads are eventually consistent by default: a read right after a write may (rarely) miss it.
`consistent=True` makes a read strongly consistent (`get`, `batch_get`, `query`, `scan`, `count`):
it reflects every write that succeeded before it, at twice the read cost. Use it when you must
read your own write, or refresh after writing (writes already refresh instances from DynamoDB's
response).

```python
latest = await entries.query(account_id="a1", consistent=True).all()  # same as .consistent()
everyone = await users.scan(consistent=True).all()
total = await orders.count(customer_id="c1", consistent=True)
```

Global secondary indexes are only eventually consistent: `consistent=True` on one raises
`ValueError` before any request. The table and local secondary indexes support it, and
`.hydrate()` reads the full items consistently when its query is consistent.

## Queries

A query targets one partition key value of the table or an index, optionally narrowed by a sort
key condition:

```python
query = (
    entries.query(account_id="a1")         # partition key (by field name or template fields)
    .where(Entry.seq.between(100, 200))    # sort key: =, <, <=, >, >=, between, begins_with
    .filter(Entry.amount > 0)              # any condition on other attributes
    .descending()                          # highest sort keys first
    .limit(50)                             # at most 50 items in total
)
```

Ways to consume it:

| Call | Returns |
| --- | --- |
| `async for item in query` | every item, page by page |
| `await query.all()` | a list of every item |
| `await query.first()` | the first item or `None` |
| `await query.one()` | exactly one item (`ItemNotFoundError` / `ValueError` otherwise) |
| `await query.count()` | the number of matching items (`Select=COUNT`, no items transferred) |
| `await query.exists()` | whether any item matches |
| `await query.page()` | one `Page` (`items`, `cursor`, `has_more`, `count`, `scanned_count`, ...) |
| `query.pages()` | an async iterator of pages |

Queries are immutable: every builder method returns a new query.

`where()` takes the one sort key condition DynamoDB allows per query: combine bounds with
`between()` (two `where()` calls, or a sort key value given to `query()` and a `where()`, raise
`ValueError`). For a templated sort key with a constant prefix (`"ORDER#{order_id}"`), the
`where()` condition replaces the `begins_with` dynamoxy adds otherwise.

### Filters cost reads

A filter is applied after DynamoDB reads items: filtered items still consume read capacity, and a
page can come back empty with a cursor. Design keys and indexes so that the key condition does
most of the selection.

### Sort order

Items come back in sort key order: strings by UTF-8 bytes, numbers numerically, bytes
bytewise. `datetime` fields store sortable text, so time ranges work with `between()`.

## Pagination with cursors

```python
page = await query.page_size(25).page()
send_to_client(page.items, page.cursor)          # cursor: str | None

next_page = await query.page_size(25).after(cursor_from_client).page()
```

- `page_size(n)` is DynamoDB's `Limit`: the number of items *evaluated* per request (before
  filters). A request also stops after reading 1 MB.
- `limit(n)` caps the items returned in total. `page()` evaluates at most `n` items (like
  `page_size(n)`), so a page never holds more than the limit. Iteration, `all()`, `first()` and
  `pages()` request what the limit still needs; with your own filters, which may remove most items,
  they read DynamoDB's pages instead. The page `pages()` cuts at the limit gets a cursor right
  after its last item, so continuing with it skips nothing.
- `cursor` is `None` when DynamoDB says there is nothing more. A cursor can be set even when the
  next page turns out empty.
- Cursors are base64url JSON of the last evaluated key: opaque and URL-safe, but not encrypted or
  signed. Never derive authorization from a cursor: the partition key of the query must come from
  your own code. If keys contain sensitive data, encrypt or sign cursors before handing them out.

## Indexes and projections

```python
await orders.query(index=Order.by_customer, customer_id="c1").descending().first()
await orders.query(index="by_status", status="paid").hydrate().all()   # fetch full items
await orders.query(customer_id="c1").project(Order.order_id, Order.status).all()  # dicts
```

Global secondary indexes are eventually consistent and cannot be read with `consistent=True`
(see [Consistency](#consistency)).

Entries of an index with a `KEYS_ONLY` or `INCLUDE` projection hold only some attributes:

- Model reads of such a *global* index raise `ValueError` before any request, unless they add
  `.hydrate()` (the full items, with `BatchGetItem`) or `.project(...)` (dicts of the attributes
  the index holds): an instance made from an entry would show defaults instead of the stored
  values, and writing it would store them. `count()` and `exists()` need neither.
- Model reads of such a *local* index ask DynamoDB for every attribute (`Select=ALL_ATTRIBUTES`),
  and its `.project(...)` may name attributes the index does not hold: DynamoDB reads the rest
  from the table, at the cost of a read of each item besides the index entries (a 1 MB page
  holds fewer items too).
- A filter on an attribute a global index does not hold raises `ValueError`: DynamoDB filters the
  index entries, so it would never match. Filter the hydrated items in Python instead, or project
  the attribute into the index.
- For models with an entity type (single-table design), dynamoxy projects the type attribute
  into such indexes when it creates them. Indexes created before (dynamoxy 0.2) do not hold it:
  their entries pass the entity filter, and `.hydrate()` checks the entity of the full items.

## Scans

```python
async for user in users.scan().filter(User.visits > 100):
    ...
total = await users.scan().count()
everything = await users.scan().segments(8).all()        # parallel segments
mine = await users.scan().segment(worker_index, 8).all() # one segment per worker process
```

Scans read the whole table (or index) and cost accordingly. Use them for maintenance jobs,
exports and migrations, not request paths. Repository scans only return the model's items (entity
filter in shared tables, guard items excluded). `segments(n)` reads every segment from its start:
continue a scan with one segment's cursor, `.segment(index, total).after(cursor)`.

## Counting

`await users.count()` scans the model's items; `await orders.count(customer_id="c1")` counts a
partition with a query. Both read every matching item (without transferring them). For counters
that must be cheap, maintain them in an item with `update(..., X.count.add(1))`.

## Collections

`client.collection(Customer, Order)` reads several entity types of one table at once and decodes
each item into its model by the type attribute:

```python
items = await client.collection(Customer, Order).query(customer_id="c1").all()
customer = next(item for item in items if isinstance(item, Customer))
```
