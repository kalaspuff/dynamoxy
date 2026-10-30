# Constraints and concurrency

DynamoDB guarantees two things natively: primary keys are unique, and a write can be conditional
on the item's current state. dynamoxy builds the common invariants of applications on those.

## Unique values

```python
class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    handle: Attr[str | None] = field(default=None, unique=True)
```

**How it works.** For each unique value, dynamoxy stores a *guard item* in the same table, keyed
by the value (`pk = "_unique#User#email#ada@example.com"`; `sk = "_unique"` when the table has a
sort key). A write that sets or changes a unique value is a transaction:

| Operation | Transaction |
| --- | --- |
| `create`/`put`/`save` with a new value | the item + `Put guard if not exists` |
| changing a value | the item + `Put new guard if not exists` + `Delete old guard` |
| removing a value (`None`) | the item + `Delete old guard` |
| `delete` | `Delete item` + `Delete guards` |

A taken value cancels the transaction and raises `UniqueConstraintError` (`.field`, `.value`);
nothing is written. `None` values have no guard (several items may have no handle), and
`update(user, User.handle.set(None))` removes the value and its guard. Numbers are guarded as
DynamoDB stores them: `5`, `5.0` and `Decimal("5.00")` are the same value.

A `delete()` of an item that is not stored has no guards to delete and writes nothing, unless it
has a `condition=`: then it checks the condition on the missing item, as DynamoDB does for any
delete. `users.delete("u1", condition=User.name == "Ada")` raises `ConditionalCheckFailedError`
when there is no item `u1`, and the same `tx.delete(...)` cancels its transaction.

A write of an instance whose unique values another writer changed since it was loaded reads the
stored item and writes again when nothing else of the write can have failed: `put()`, and
`save()`, `update()` and `delete()` of unversioned models, without a `condition=`. `put()`
replaces what is stored and moves the guards with it, as it promises, and `update()` and
`delete()` change or delete the item with the guards of its values as stored now. When another
writer deleted the item, they behave as for other models: `put()` and `save()` store it again,
`delete()` returns `None` and `update()` raises `ItemNotFoundError`. A transaction cannot read
again: such a write cancels it with a `ConditionalCheckFailedError` that says to `refresh()` the
instance and retry. Upserts (`update(..., upsert=True)`) raise that error too instead of writing
again: had another writer deleted the item, an upsert planned again would create it without the
unique values that its actions do not set.

**Costs and requirements.**

- Writes of unique models are transactional: 2x write capacity, plus a guard write per changed
  value. A delete or change of an instance that was not loaded first reads the stored item to
  find the old values (one consistent read); a write of a loaded instance whose unique values
  changed meanwhile costs that read and a second transaction.
- The table's partition key must be a string attribute (guards share it).
- Batch writes are refused (they cannot be transactional).
- `update()` must set unique fields with `set()` or `remove()`.
- Guards are skipped by repository reads; dict-level scans and exports see them (`pk` starts with
  `_unique#`).
- Only writes made by dynamoxy maintain guards. If other services write the table, they must
  follow the same protocol, or uniqueness is not guaranteed.

**Case-insensitive uniqueness.** Normalize the value before it is stored, with a validator:
`email: Attr[str] = field(unique=True, validators=(str.lower,))`.

## Optimistic locking

Add `version: Attr[int] = dynamoxy.version()`. The version starts at 1 and increases on every
write. When an instance was loaded (read, or written by dynamoxy), `save()`, `update()` and
`delete()` of that instance add the condition `version = <loaded version>`. If someone else
wrote the item meanwhile, `VersionConflictError` is raised and nothing changes:

```python
async def add_tag(users: dynamoxy.Repository[User], user_id: str, tag: str) -> User:
    for attempt in range(5):
        user = await users.get_or_raise(user_id, consistent=True)
        user.tags = user.tags | {tag}
        try:
            return await users.save(user)
        except dynamoxy.VersionConflictError:
            await asyncio.sleep(0.01 * 2**attempt)
    raise RuntimeError("too much contention")
```

The instance methods check the version the same way: `await user.save()`, `user.update(...)`,
`user.add(...)`, `user.increment(...)` and `user.delete()` raise `VersionConflictError` for a
stale instance, and `await user.refresh()` reloads it before you re-apply the change.

Prefer atomic updates when the change does not depend on the current value:
`await users.update(User.key(user_id), User.tags.add({tag}))` needs no read and no retry.

`save()` of a *new* instance (never loaded) behaves like `create()` for versioned models: it fails
if the key exists. Use `put()` to overwrite unconditionally.

Items stored before the model had a `version()` field load with `version` `None`. `save()` of
such an instance requires that the item still exists without a version, and stores version 1;
`VersionConflictError` says that another writer saved it first, or deleted it.

## Conditions as business rules

Any write takes `condition=`. Typical rules:

```python
# Never overdraw.
await accounts.update(Account.key(account_id), Account.balance.add(-amount), condition=Account.balance >= amount)

# State machines: only move forward.
await orders.update(order, Order.status.set("shipped"), condition=Order.status == "paid")

# Rate limiting with a counter per window.
await counters.update(
    Counter.key(f"{user_id}#{window}"),
    Counter.hits.add(1),
    condition=Counter.hits.not_exists() | (Counter.hits < 100),
    upsert=True,
)
```

`ConditionalCheckFailedError.item` contains the stored item, so you can tell the user why without
another read.

## Immutable fields

`field(immutable=True)` fields cannot change once stored: `update()` actions on them raise
`ValueError`, and so do `put()`, `save()` and batch puts of a loaded instance whose value changed.
Keys are always immutable, and so are the fields a key template uses: `update()` actions on
them raise `ValueError`, and so does every write through a loaded instance whose key changed.

## Idempotency

- Conditional creates are naturally idempotent: create an item keyed by the request or message id
  (`ProcessedMessage(message_id=...)`) in the same transaction as the effect.
- Transactions take `token=`: an identical retry within 10 minutes succeeds without being
  applied twice (see [transactions](transactions.md)).
- dynamoxy's own retries reuse the transaction token and never retry conditional failures.
