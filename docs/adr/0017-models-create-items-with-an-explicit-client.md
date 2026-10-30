# 0017: Models create items with an explicit client: `Model.create(client, ...)`

Status: Accepted (amends [0011](0011-instances-remember-their-repository.md))

## Context

Since [ADR 0011](0011-instances-remember-their-repository.md), instances persist themselves
(`await user.save()`), but a new item still takes two steps:
`await client.model(User).create(User(user_id="u1", ...))`. Applications asked for
`await User.create(...)`. Active-record ORMs answer this with a connection bound to the model
class, or a global or context-wide default connection. [ADR 0007](0007-repositories-over-active-record.md)
rules out the first two: models hold no client, so several clients (accounts, regions, test
stores) use the same models.

## Decision

* `Model` and `PydanticModel` get a classmethod `create(client, /, **fields)`. It builds the
  instance with the constructor (the same validation, before any I/O) and calls
  `client.model(Model).create(instance)`, so it fails, hooks and binds exactly like
  `Repository.create`.
* The client is the first argument, positional-only. It never collides with a field (a field
  may be named `client`), and a later version can make it optional without changing any call.
* `cls` is typed as the model's constructor (`cls: Callable[P, M]`), so type checkers check
  the fields of `create` like those of the constructor: names, types and required fields
  (verified with mypy, pyright, pyrefly and ty).
* `create` becomes part of the model API: `Model` fields cannot use the name
  (`attribute_name="create"` keeps such a stored name). Documents do not reserve it.
* No condition other than "the key is new" and no repository subclass: those stay with
  `client.model(Model).create(...)` and `UserRepository(client).create(...)`.

## Alternatives considered

* **The client from a context variable** (`with dynamoxy.use_client(client): await
  User.create(...)`), so that calls need no client. Not now: a write would go to whichever
  client the context holds, an account or region the calling code does not name; and ASGI
  servers such as Uvicorn run an application's lifespan, where web applications create their
  client, in a task of its own, so a value set there never reaches request handlers (they would
  need a middleware anyway). The signature leaves room for it: an opt-in scope could fill the
  optional client later, never set implicitly by `async with client`, with an error naming
  both ways when there is none.
* **`User(...).create(client)`**, an instance method: `create` would read like a second
  `save()`, and the classmethod reads as what it does, building and creating.
* **`User.create(..., client=client)`**: collides with fields named `client`.
* **More classmethods** (`User.get(client, ...)`, `User.query(client, ...)`): not now. Reads
  stay on `client.model(User)`, and each name taken by the model API is a name fields cannot
  use.

## Consequences

* `await User.create(client, user_id="u1", name="Ada")` writes a new item and returns it,
  stored and bound, in one line that type checkers check.
* Fields named `create` need another Python name (a breaking change, acceptable in 0.x).
* Code that relies on the overrides of a repository subclass calls the subclass, as before.
