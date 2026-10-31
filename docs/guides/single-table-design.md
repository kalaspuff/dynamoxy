# Single-table design

Single-table design stores several entity types in one table, so that related items share a
partition and one query returns them together. dynamoxy supports it with entities, key
templates, derived index keys and collections.

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
    status: Attr[OrderStatus] = field(default=OrderStatus.NEW)
    placed_at: Attr[datetime]
    gsi1pk: Attr[str] = field(template="STATUS#{status}")
    gsi1sk: Attr[str] = field(template="{placed_at}")

    by_status = GlobalIndex("gsi1", partition_key=gsi1pk, sort_key=gsi1sk)
```

## Entities

`entity="Order"` stores the entity name in a type attribute (`_type` by default;
`type_attribute=` changes it). Repository reads filter on it, so `orders.scan()` returns orders
only, and collections use it to decode mixed results. Entity names are part of the stored format:
renaming one needs a data migration.

## Key templates

`template="ORDER#{order_id}"` renders the key from other fields with their stored form (enums as
values, datetimes as ISO text, numbers as text). Numbers are rendered as DynamoDB stores them
(`Decimal("1.50")` as `1.5`, also in union and `Any` fields) and datetimes formatted with a spec (`{placed_at:%Y-%m}`) are
formatted in UTC, so an item read back renders the same key. Templated fields:

- are never passed to the constructor and are recomputed on every write;
- are recomputed by `update()` when a field they use changes, including `updated_at` and
  `version` (dynamoxy reads the item first if the update does not set every field the template
  needs). Changes by arithmetic or functions
  (`add`, `increment`, `set(path + 1)`, `set_if_not_exists`) are evaluated on the item as
  dynamoxy last read it. That is exact for versioned models (a stale instance fails the version
  check); for other models, a concurrent change between that read and the write can leave the
  templated attribute behind, so give models whose keys follow counters a `version()` field;
- accept the fields they use in reads: `orders.get(customer_id="c1", order_id="o1")`,
  `orders.query(customer_id="c1")`, `Order.key(customer_id=..., order_id=...)`. A sort key may
  repeat fields of the partition key (`"ORG#{org_id}"` and `"MEMBER#{org_id}#{user_id}"`):
  `memberships.query(org_id="o1", user_id="u1")` selects the sort key too.

When a model's sort key template starts with a constant (`"ORDER#"`) and a query gives only the
partition key, dynamoxy adds `begins_with(sk, "ORDER#")`, so `orders.query(customer_id="c1")`
does not return the customer profile. A `where()` condition replaces it, since DynamoDB takes one
sort key condition per query:
`orders.query(customer_id="c1").where(Order.sk.begins_with("ORDER#2026-05"))`. The entity type
still keeps other models out (`Order.sk >= "ORDER#2026"` would reach the profile otherwise).

Template values must not contain the separators you rely on: validate them (`pattern=`) when
they come from users.

## Generic indexes

Overloaded index keys (`gsi1pk`, `gsi1sk`) let one index serve several access patterns. Declare
them as templated fields and the index on them; query by the template's fields:

```python
paid = await orders.query(index=Order.by_status, status=OrderStatus.PAID).all()
```

Models that should not appear in an index simply have no such fields (sparse index).

An index with a `KEYS_ONLY` or `INCLUDE` projection of a model with an entity type also holds
the type attribute when dynamoxy creates it, so that queries can filter on it. Read the full items
with `.hydrate()`, or dicts with `.project(...)` (see
[indexes and projections](reading-data.md#indexes-and-projections)).

## Collections

```python
items = await client.collection(Customer, Order).query(customer_id="c1").all()
for item in items:
    match item:
        case Customer():
            ...
        case Order():
            ...
```

A collection reads a partition (or scans the table) once and decodes each item by its entity.
Items of entities not in the collection are skipped.

## Creating the table

`client.create_tables(Customer, Order)` merges the models of a table: key schema, attribute
definitions and every index. Models sharing a table must agree on key attribute names and types
(dynamoxy raises `TypeError` otherwise).

## Tips

- Start from access patterns: list every query the application needs, then design keys.
- Keep partition keys high-cardinality; avoid one hot partition for all writes.
- Use `begins_with` on sort keys for hierarchies (`ORDER#2026-05#o1`).
- Keep item collections (all items of one partition key) below 10 GB when using local indexes.
