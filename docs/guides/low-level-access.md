# Low-level access

Models are the main API, but some tasks need raw items: migrations, admin tools, tables owned by
other services, or data whose shape varies.

## The dict-level table API

```python
from dynamoxy import F

table = client.table("legacy")   # logical name (prefix and table_names apply)

await table.put({"pk": "a", "sk": 1, "data": {"x": 1}}, condition=F("pk").not_exists())
item = await table.get({"pk": "a", "sk": 1}, consistent=True, projection=["data"])
new = await table.update({"pk": "a", "sk": 1}, F("data", "x").add(1), return_values="ALL_NEW")
old = await table.delete({"pk": "a", "sk": 1}, return_old=True)

rows = await table.query(F("pk") == "a", F("sk") >= 10, index=None).filter(F("data").exists()).all()
everything = await table.scan().segments(4).all()
await table.batch_write(puts=new_items, deletes=old_keys)
found = await table.batch_get(keys)
```

Items are dicts of plain Python values: `str`, `int`/`Decimal` (numbers read back as `int` when
whole, else `Decimal`), `float` (written), `bytes`, `bool`, `None`, `list`, `dict`, `set`
(non-empty, of one type). Paths are untyped (`F("a", "b")`, `F("list")[0]`), but conditions and
update actions work exactly like typed paths.

`query()` and `scan()` take `index=` and `consistent=True` like repositories. A table does not know
which of its indexes are global, so DynamoDB itself refuses a consistent read of a global secondary
index (`ValidationError`).

## PartiQL

```python
rows = await client.execute('SELECT * FROM "prod-users" WHERE user_id = ?', "u1")
await client.execute('UPDATE "prod-users" SET visits = visits + ? WHERE user_id = ?', 1, "u1")
responses = await client.execute_transaction([(statement, [param, ...]), ...])
results = await client.batch_execute([(statement, [param, ...]), ...])
```

Statements use physical table names (`client.table_name("users")`) and `?` parameters (never
format values into statements). `execute()` follows `NextToken` and returns every row.

`ORDER BY` takes the partition and sort key of the table or index (`ASC` or `DESC`). DynamoDB
then runs one query per partition key value, so the `WHERE` clause must name them
(`pk = ?`, `pk IN [?, ?]`), and with several the partition key must be ordered too:
`... WHERE pk IN [?, ?] ORDER BY pk, sk DESC`. The in-memory backend applies the same rules
(MiniStack does not support `ORDER BY`).
`execute_transaction()` is all-or-nothing, and like every transaction it names each item at most
once (`ValidationError` otherwise); `batch_execute()` reports an error per statement.

## DynamoDB's typed JSON

```python
wire = dynamoxy.to_wire({"n": Decimal("1.5"), "tags": {"a"}})   # {"n": {"N": "1.5"}, "tags": {"SS": ["a"]}}
item = dynamoxy.from_wire(record["dynamodb"]["NewImage"])       # DynamoDB Streams, Lambda, exports
json_safe = dynamoxy.to_wire(item, base64_binary=True)          # binary as base64 text
```

Use these to exchange items with boto3's low-level client, DynamoDB Streams and S3 exports.
To turn a stream image into a model: `dynamoxy.schema_of(User).decode(dynamoxy.from_wire(image))`.

## Calls without a wrapper

`client.call(method, operation, **params)` performs any native operation through the middleware,
telemetry and event pipeline; repositories and tables are built on it. Parameters mirror the
DynamoDB API in snake_case (see `python/dynamoxy/_core.pyi`). Prefer the typed APIs; this is the
escape hatch.
