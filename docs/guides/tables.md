# Tables

## Names

Models declare logical table names (`table="users"`). The client maps them to physical names:
`Client(table_prefix="prod-")` makes `users` → `prod-users`; `table_names={"users":
"legacy_users"}` maps single names (without the prefix). `client.table_name("users")` returns the
physical name. See [Deployment](deployment.md#environments-sharing-an-account) for environments
sharing an account.

## Creating and evolving

```python
await client.create_tables(User, Order, billing=dynamoxy.Billing.on_demand(), tags={"team": "core"})
await client.ensure_tables(User, Order)
```

- `create_tables()` creates each table with its key schema, every global and local index from the
  models, and Time to Live on `ttl()` fields; it waits until the tables are active.
  `exist_ok=True` skips existing tables, also a table that another process creates at the same
  time (replicas or test workers starting together).
- `ensure_tables()` creates missing tables and adds missing global indexes (one at a time, as
  DynamoDB requires, waiting for each backfill) and TTL settings. With `wait=False` it waits only
  where DynamoDB needs it: for a table it created before enabling TTL, and for each index before
  adding the next one (also for one an earlier call left backfilling), but not for the last. It never deletes indexes or
  tables, and raises `TypeError` when what exists differs from the models in a way it cannot
  change: the key schema of the table or of an index with the same name (the attributes or their
  types), or the TTL attribute. A table that another process creates at the same time is checked
  like an existing one.
- Indexes with a `KEYS_ONLY` or `INCLUDE` projection of models with an entity type also project
  the type attribute (`_type`), which model queries filter on.

In production, prefer infrastructure as code (CloudFormation, CDK, Terraform). The definitions
dynamoxy would use are available for generating it:

```python
for definition in dynamoxy.table.definitions_for([User, Order]):
    print(definition.name, definition.partition_key, definition.sort_key, definition.indexes, definition.ttl_attribute)
```

## Table operations

```python
table = client.table("events")

description = await table.describe()          # keys, indexes (status, backfilling), billing, stream, counts
await table.exists()
await table.update_settings(billing=dynamoxy.Billing.provisioned(10, 5))  # global indexes get 10/5 too
await table.update_settings(stream="NEW_AND_OLD_IMAGES")   # stream=False disables it
await table.update_settings(deletion_protection=True, table_class="STANDARD_INFREQUENT_ACCESS")
await table.add_index(dynamoxy.IndexDefinition(name="by_kind", kind="global", partition_key=("kind", "S")))
await table.delete_index("by_kind")
await table.delete_table()
names = await client.list_tables()
```

A switch from on-demand to provisioned billing gives every global index the table's capacity,
since DynamoDB needs capacity for each of them; switches between provisioned capacities change
the table's only, and a switch back to on-demand drops the indexes' capacity. `add_index()` gives
an index added to a provisioned table the table's capacity too, unless its definition or
`billing=` gives another.

DynamoDB changes a table one step at a time: it refuses to change a table it is still creating
(`ResourceInUseError`, and `ResourceNotFoundError` for Time to Live) and to add an index while
another is being created (`LimitExceededError`, "Only 1 online index can be created or deleted
simultaneously per table"), also during the minutes of its backfill when the table reads `ACTIVE`
again. The in-memory backend refuses a deletion then too, as the message says (not measured). `create_tables()` and `ensure_tables()` wait for each step
before the next; with `wait=False` on your own calls, call `table.wait_until_active()` before the
next change. On the in-memory backend a new table or index reads `CREATING` in the first
`DescribeTable` and `ACTIVE` from the next on (as when polling until it is active), so tests
catch a missing wait.

## Time to Live

```python
await table.enable_time_to_live("expires_at")   # no-op when already enabled on it
ttl = await table.time_to_live()                 # TimeToLive(status, attribute)
await table.disable_time_to_live()
```

DynamoDB deletes expired items in the background, typically within days; reads still return them
until then. Deletions by TTL appear in streams as service deletions.

## Backups and point-in-time recovery

```python
await table.enable_point_in_time_recovery(recovery_period_days=35)
state = await table.continuous_backups()           # earliest/latest restorable times
await table.restore_to("events-restored", at=datetime(2026, 5, 1, tzinfo=UTC))

backup = await table.create_backup("before-migration")
await table.backups()
await client.table("events-copy").restore_backup(backup.arn)
await table.delete_backup(backup.arn)
```

Restores create new tables; indexes and settings come from the backup, but TTL, tags, streams
and auto scaling must be configured again.

## Tags

```python
await table.tag({"team": "payments", "cost-center": "42"})
await table.untag("cost-center")
tags = await table.tags()
```
