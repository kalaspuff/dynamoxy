# Writing data

## Choosing a write

| Method | Stored item | Fails when | Typical use |
| --- | --- | --- | --- |
| `create(item)` | new item | the key exists (`ItemAlreadyExistsError`) | inserts |
| `put(item)` | replaces whatever is stored | only on `condition=` | idempotent upserts of whole items |
| `save(item)` | replaces | the stored version differs (`VersionConflictError`) | read-modify-write of versioned models |
| `update(item_or_key, *actions)` | changes attributes in place | item missing (`ItemNotFoundError`, unless `upsert=True`) | counters, partial changes |
| `delete(item_or_key)` | removes it | version (instances) or `condition=` | deletes |

All writes validate first, run hooks, maintain automatic and derived fields and unique guards,
and refresh the instance from DynamoDB's response.

A write of an instance that was loaded (read or written by dynamoxy) keeps what it was loaded
with:

- Its key cannot change (nor a field that a key template uses): `put()`, `save()` and batch
  puts raise `ValueError` before any I/O. Write a copy as a new item
  (`await orders.create(order.copy(customer_id="c2"))`) and delete the old one.
- `immutable=True` fields keep their stored value: a changed one raises `ValueError`. A field
  that the stored item does not have yet can get its first value.

## Writing through an instance

An instance that a repository read or wrote remembers that repository, so it can write itself:

```python
user = await users.get_or_raise("u1")
user.name = "Ada"
await user.save()                                     # users.save(user)
await user.save(condition=User.status == "active")

await user.increment(User.visits)                     # +1, atomic; a missing number counts as 0
await user.decrement(User.credits, 10, condition=User.credits >= 10)
await user.add(User.tags, "vip")                      # sets: one element, or a set of elements
await user.add(User.history, "signed-in")             # lists: appended, created when missing
await user.add(User.score, 5)                         # numbers
await user.increment(User.stats["logins"])            # nested paths
await user.update(User.name.set("Ada"), User.nickname.remove())  # any update actions
await user.refresh()                                  # reload in place
await user.delete()                                   # users.delete(user)
```

| Instance method | Does |
| --- | --- |
| `save(condition=)` | `repository.save(item, ...)`: creates the item or replaces it, with the version check for versioned models |
| `delete(condition=)` | `repository.delete(item, ...)`: returns the item as it was stored, or `None` |
| `update(*actions, condition=, upsert=)` | `repository.update(item, *actions, ...)` |
| `add(path, value, condition=)` | an update that adds to a number, adds set elements or appends to a list |
| `increment(path, amount=1, condition=)`, `decrement(...)` | an update of a number: `ADD` for attributes, `SET p = if_not_exists(p, 0) + n` for nested paths |
| `refresh(consistent=True)` | `repository.refresh(item)` |

- They call the repository's own methods, so everything on this page applies (validation,
  hooks, versions, unique guards, automatic and derived fields), and so do the overrides of your
  repository subclasses: an instance read by a `UserRepository` saves through it.
- Every read binds instances (`get`, queries, scans, `batch_get`, `transact_get`, collections,
  `refresh`), and so does every write (`create`, `put`, `save`, `update`, batch writes,
  transactions). An instance stays with the repository, and client, that last read or wrote it;
  transactions and batch writers keep that binding.
- New instances are not bound until a repository writes them: `await users.create(user)` (or
  `save`/`put`) first. `copy()` results and unpickled instances are not bound either. Their
  methods raise `RuntimeError` before any I/O.
- `add`, `increment` and `decrement` check the value against the attribute before any I/O, and
  type checkers check it too: `increment(User.name)` raises `TypeError`,
  `increment(User.visits, 0.5)` a `ValidationError`.
- Like every update of an instance, they check the version of versioned models. For hot
  counters that should not conflict with other writers, update by key:
  `await users.update(User.key("u1"), User.visits.increment())`.
- The instance is refreshed from DynamoDB's result after each of them.

## Update actions

```python
await users.update(
    user,
    User.visits.add(1),                       # ADD (numbers and sets; missing counts as 0 / empty)
    User.name.set("Ada"),                     # SET a value
    User.score.set(User.score + 5),           # arithmetic: + and -
    User.nickname.remove(),                   # REMOVE
    User.tags.delete({"old"}),                # DELETE elements from a set
    User.history.append_or_create(["login"]), # list_append, creating the list if missing
    User.address["city"].set("Lund"),         # nested paths
)
```

| Action | Expression |
| --- | --- |
| `path.set(value)` | `SET path = :v` (value, another path, `path + n`, `path - n`) |
| `path.set_if_not_exists(value)` | `SET path = if_not_exists(path, :v)` |
| `path.if_not_exists(default)` | an operand: `User.n.set(User.n.if_not_exists(0) + 1)` |
| `path.add(value)`, `increment(n)`, `decrement(n)` | `ADD path :v` |
| `path.remove()` | `REMOVE path` |
| `path.delete(values)`, `discard(values)` | `DELETE path :set` |
| `path.append(list)`, `prepend(list)` | `SET path = list_append(...)` (the list must exist) |
| `path.append_or_create(list)` | `SET path = list_append(if_not_exists(path, :empty), :v)` |

Rules from DynamoDB: one action per path, no overlapping paths (`a` and `a.b`), key attributes
cannot change. dynamoxy also refuses updates of derived (templated) and `immutable` fields and of
unique fields with anything but `set()`/`remove()`.

`None` means "no attribute" for model fields, as in `put()` and `save()`: `User.nickname.set(None)`
removes the attribute (it never stores DynamoDB's `NULL`). A `NULL` that another writer stored
reads as `None` too, so `User.nickname == None` matches items without the attribute or with a
`NULL`, and `!= None` matches the others. Untyped paths (`F("x")`) keep DynamoDB's `NULL`.

`increment()`, `decrement()` and arithmetic (`User.score + 5`) check their operand against the
field: an `int` field takes integers (`User.visits.increment(0.5)` raises `ValidationError`), a
`timedelta` field durations.

Templated attributes are rendered again when an update changes a field they use, including the
automatic ones: a template of `updated_at` or `version` (`"{status}#{updated_at}"`) follows every
update.

Values you `set()` are validated like constructor arguments (type, constraints such as `ge` or
`pattern`, validators), and removing a required field is refused, all before any I/O. Arithmetic
(`add`, `increment`, `set(path + n)`) happens in DynamoDB and cannot be checked locally: guard it
with a condition (`condition=Account.balance >= amount`).

`update()` of an instance checks its version (for versioned models) and refreshes it; `update()`
of a key (`User.key("u1")`) only needs the key, so it suits hot counters. Keys can also be given
as field values: `devices.update({"device_id": device_uuid}, ...)`.

## Conditions

```python
await users.put(user, condition=User.email.not_exists() | (User.email == user.email))
await users.delete("u1", condition=User.visits == 0)
await users.update(User.key("u1"), User.credits.add(-10), condition=User.credits >= 10)
```

When a condition fails, `ConditionalCheckFailedError.item` holds the stored item (DynamoDB returns
it on failure; no extra read).

## Upserts

`update(target, *actions, upsert=True)` updates the item, or creates it when it is missing. The
created item must be a complete model item, so dynamoxy:

- writes the entity type (single-table models) and `created_at`, `updated_at` and `version`;
- gives every field the actions leave alone its value with `if_not_exists`: the field's default
  for key targets, the instance's value for instance targets (existing items keep theirs);
- renders derived (templated) attributes from those values, also only where missing;
- refuses, before any I/O, an upsert that leaves a required field without a value. Provide it in
  the actions, usually with `set_if_not_exists(...)`, which never overwrites an existing value:

```python
await stats.update(
    PageStats.key("home"),
    PageStats.views.add(1),
    PageStats.title.set_if_not_exists("Home"),
    upsert=True,
)
```

Models with templated keys need the values the templates use: pass the key as field values
(`orders.update({"customer_id": "c1", "order_id": "o1"}, ..., upsert=True)`) instead of a stored key.
Unique attributes must be set by the actions of an upsert that may create the item (so the guard
item is written), unless the target is a loaded instance.

## Batches

```python
await events.batch_put(items)        # BatchWriteItem in chunks of 25, in parallel
await events.batch_delete(keys)
found = await events.batch_get(keys) # BatchGetItem in chunks of 100
```

- Not atomic and unconditional: each item succeeds or is retried on its own.
- Unprocessed items (throttling) are sent again with backoff for `batch_max_rounds` rounds; what
  remains raises `UnprocessedItemsError` with `.unprocessed`. Only the writes that were made
  update their instances and run `after_save`/`after_delete` hooks.
- Two writes of the same key in one request are invalid in DynamoDB: `batch_put()`,
  `batch_delete()` and `client.batch_writer()` write each key once (the last write wins). The
  batch writer flushes every `flush_size` operations; a flush that fails before its request (a
  `before_save` hook or validation raised) or whose request fails keeps the other writes queued.
- Refused for models with unique attributes (use transactions or single writes).
- `before_save` hooks run for batch puts; conditions and versions do not apply.

## Deleting

`delete()` (`await users.delete(user)` or `await user.delete()`) returns the deleted item (or
`None` when nothing was stored), runs delete hooks and removes unique guards in the same
transaction. Deleting a loaded versioned instance checks its version. A deleted instance is no
longer loaded but stays bound to its repository.

## Time to Live

Set a `ttl(default=None)` field to a `datetime`; DynamoDB deletes expired items within a few
days. Until then they are still returned: filter them out in reads that must not see them
(`.filter(Session.expires_at > now)`).
