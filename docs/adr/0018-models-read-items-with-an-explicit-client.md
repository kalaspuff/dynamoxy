# 0018: Models read items with an explicit client: `Model.get` and `Model.query`

Status: Accepted (amends [0017](0017-models-create-items-with-an-explicit-client.md))

## Context

[ADR 0017](0017-models-create-items-with-an-explicit-client.md) added
`Model.create(client, ...)` and left reads on `client.model(User)`, to keep `get` and `query`
free as field names. Applications then wrote `User.create(client, ...)` next to
`client.model(User).get("u1")`: two ways into one model for its three most common calls. Fields
named `get` or `query` are rare, and a stored attribute can keep either name under another
Python name. dynamoxy chooses the shorter API for everyone over those names.

## Decision

* `Model` and `PydanticModel` get the classmethods
  `get(client, /, *key, consistent=False, **key_values)` and
  `query(client, /, *, index=None, consistent=False, **key_values)`. They call
  `client.model(Model).get(...)` and `.query(...)`: the same arguments, results and errors, and
  instances bound to `client.model(Model)`. `query` returns the immutable `Query` and reads
  nothing until it is iterated or awaited.
* As for `create`, the client comes first and positional-only, and a call without a client
  raises `TypeError` before any I/O.
* `get` and `query` become part of the model API: fields cannot use the names. Pydantic models
  now refuse every name of the model API too (`key`, `to_item`, `save`, ...), with the
  `TypeError` of native models: pydantic only warned, and the field's path replaced the method,
  so `Model.query(...)` failed later with `'FieldPath' object is not callable`.
  `attribute_name="query"` (`AttributeName("query")` in pydantic models) keeps such a stored
  name under another Python name, and a pydantic alias keeps it in pydantic's input and output;
  expressions name the field, and the dict-level API sees the attribute unchanged.
* `client.model(Model)` builds a plain repository on first use and returns the same one
  afterwards (a closed client keeps none). The classmethods then cost about 0.3 µs more than a
  held repository on the in-memory backend, instead of 1.4 µs for building one per call.
* The other reads (`get_or_raise`, `exists`, `scan`, `count`, `batch_get`) stay on the
  repository.

## Consequences

* `await User.get(client, "u1")` and `User.query(client, index=User.by_email, email=...)` read
  with the types checkers expect: `User | None` and `Query[User]`.
* Fields named `get` or `query`, and pydantic fields named like any part of the model API, need
  another Python name (a breaking change, acceptable in 0.x).
* The physical table name of `client.model(Model)` is fixed when the client first builds it:
  changing `table_prefix` or `table_names` of a client in use does not move it, just as it never
  moved repositories that were already built.
