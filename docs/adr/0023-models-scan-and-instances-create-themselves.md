# 0023: `Model.scan(client)`, and instances create themselves: `instance.create(client)`

Status: Accepted (amends [0017](0017-models-create-items-with-an-explicit-client.md) and
[0018](0018-models-read-items-with-an-explicit-client.md))

## Context

[ADR 0018](0018-models-read-items-with-an-explicit-client.md) left `scan` on the repository, so
code that reads with `Customer.query(client, ...)` still wrote `client.model(Customer).scan()`
for the rest. [ADR 0017](0017-models-create-items-with-an-explicit-client.md) rejected
`User(...).create(client)`, so an instance built elsewhere (from a request body, a pydantic
`model_validate`, a factory) still needed `client.model(User).create(instance)`. Carl Oscar asked
for both, so that the examples read through the model alone.

## Decision

* `Model` and `PydanticModel` get the classmethod `scan(client, /, *, index=None,
  consistent=False)`, which returns `client.model(Model).scan(...)`: the immutable `Scan`, the
  same errors, instances bound to `client.model(Model)`. `scan` becomes part of the model API:
  fields cannot use the name (`attribute_name="scan"` keeps such a stored name).
* `create` answers both on the class and on an instance. `Model.create(client, **fields)` is
  unchanged; `instance.create(client)` writes the instance as a new item through
  `client.model(type(instance)).create(instance)` and returns the instance itself, stored and
  bound. It takes no field values and no condition (`client.model(User).create(user,
  condition=...)` does). A descriptor picks the form at runtime.
* Type checkers see `create` as an overloaded classmethod: the constructor's parameters after
  the client, or the client alone. mypy cannot infer a constructor's parameters through a
  descriptor's `__get__`, only through a classmethod's `cls: Callable[P, M]`, so the overloads
  keep mypy, pyright and pyrefly checking the field values of `Model.create(client, ...)`. The
  cost: `Model.create(client)` without any field value type-checks (the second form) and fails
  at runtime, before any I/O, when the model has required fields. mypy reports wrong field
  values as `call-overload` instead of `arg-type` or `call-arg`.

## Consequences

* `await Customer(...).create(client)`, `Customer.scan(client).filter(...)` and
  `Customer.query(client, ...)` cover the examples without a repository.
* Fields named `scan` need another Python name (a breaking change, acceptable in 0.x).
* `create` on an instance reads like `save()`: it differs in failing when the key exists
  (`ItemAlreadyExistsError`), also for an instance that was loaded.
