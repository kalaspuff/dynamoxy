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
`update(user, User.handle.set(None))` removes the value and its guard.

**Costs and requirements.**

- Writes of unique models are transactional: 2x write capacity, plus a guard write per changed
  value. A delete or change of an instance that was not loaded first reads the stored item to
  find the old values (one consistent read).
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

`field(immutable=True)` fields cannot be updated once stored: `update()` actions on them raise
`ValueError`. Keys are always immutable.

## Idempotency

- Conditional creates are naturally idempotent: create an item keyed by the request or message id
  (`ProcessedMessage(message_id=...)`) in the same transaction as the effect.
- Transactions take `token=`: an identical retry within 10 minutes succeeds without being
  applied twice (see [transactions](transactions.md)).
- dynamoxy's own retries reuse the transaction token and never retry conditional failures.
