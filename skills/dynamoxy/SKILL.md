---
name: dynamoxy
description: Build DynamoDB data access in Python with dynamoxy (typed async ORM with a Rust core) - models, repositories, queries, transactions, unique constraints, single-table design, tests with the in-memory DynamoDB. Use when code imports dynamoxy or a Python service that uses dynamoxy needs to read or write DynamoDB.
---

# Using dynamoxy

Before writing code, read the full usage guide that ships with the installed version:

```bash
uv run dynamoxy guide        # or: python -m dynamoxy guide
```

It is authoritative for the installed version; this skill only summarizes it.

## Non-negotiables

1. One `dynamoxy.Client` per process, created after `fork()`, shared, and closed at shutdown
   (`async with client:`). Tests use `dynamoxy.testing.memory_client()`. Environments sharing
   an AWS account use `Client(table_prefix="dev-")` (`users` → `dev-users`), not per-environment
   table names in models.
2. Models subclass `dynamoxy.Model` (or `dynamoxy.PydanticModel`) with `table="..."`; fields are
   `Attr[T]` with `field()`, `partition_key()`, `sort_key()`; automatic fields use `created_at()`,
   `updated_at()`, `version()`, `ttl(default=None)`.
3. All access goes through the model's classmethods (`Model.create(client, ...)`,
   `Model.get(client, ...)`, `Model.query(client, ...)`), the client's typed model operations
   (`client.model(Model)`, a `Repository[Model]`) or a `Repository[Model]` subclass with domain
   methods. No raw DynamoDB calls in services. Instances a repository read or wrote write
   themselves through it: `await item.save()`, `await item.delete()`,
   `await item.increment(Model.counter)`, `await item.add(Model.tags, "x")`,
   `await item.update(*actions)`, `await item.refresh()`.
4. Pick the write that states the intent: `create` (new only; `await Model.create(client,
   field=...)` builds and writes in one call), `save` (optimistic locking), `update` with atomic
   actions (`Model.counter.add(1)`), `put` (overwrite), `delete`.
5. Lifecycle hooks are decorated model methods: `@dynamoxy.before_save`/`after_save`,
   `before_update`/`after_update` (also for `add`/`increment`/`decrement`),
   `before_delete`/`after_delete`, `after_load`; writes by key run none.
6. Every query needs a partition key value; add a `GlobalIndex` per access pattern. No `scan()` on
   request paths. Paginate with `page()` / `cursor` / `after(cursor)`; never trust cursors for
   authorization. Reads are eventually consistent: pass `consistent=True` (`get`, `query`,
   `scan`; not on a `GlobalIndex`) to read your own writes. Reads of a `KEYS_ONLY`/`INCLUDE`
   global index need `.hydrate()` (full items) or `.project(...)` (dicts of what it holds), and
   filters may only name what an index holds. `where()` takes one sort key condition (use
   `between()` for ranges).
7. Use `field(unique=True)` for unique values, `version()` for concurrent edits, conditions for
   business rules, and `client.transaction()` for multi-item changes. Keys and
   `immutable=True` fields of loaded instances cannot change: write a copy as a new item.
   `Model.field.set(None)` removes an attribute. `None` and empty sets are stored as no
   attribute, which reads back as `None` or an empty set whatever the field's default (a
   `set[X] | None` field reads an empty set back as `None`).
8. Catch specific errors: `ItemNotFoundError`, `ItemAlreadyExistsError`, `UniqueConstraintError`,
   `VersionConflictError`, `ConditionalCheckFailedError`, `TransactionCanceledError`
   (`.cancellation_reasons`), `ValidationError` (`.errors`), and `WriteOutcomeUnknownError` for
   a write that may have been applied (never repeat an increment blindly after it).
9. Never log item contents; use `dynamoxy.collect()` and events for observability. Error
   messages leave out keys and values (they are in `error.key`, `error.value`,
   `FieldError.value`), so logging `str(error)` is safe.

## Skeleton

```python
import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    name: Attr[str]
    version: Attr[int] = dynamoxy.version()

    by_email = dynamoxy.GlobalIndex(partition_key=email)


class UserRepository(dynamoxy.Repository[User]):
    async def by_email(self, email: str) -> User | None:
        return await self.query(index=User.by_email, email=email).first()


async def main() -> None:
    async with dynamoxy.Client() as client:
        users = UserRepository(client)
        user = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
        user.name = "Ada Lovelace"
        await user.save()  # through UserRepository, with the version check
```
