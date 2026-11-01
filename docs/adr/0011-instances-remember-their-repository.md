# 0011: Model instances remember the repository that read or wrote them

Status: Accepted (amends [0007](0007-repositories-over-active-record.md)), amended by
[0017](0017-models-create-items-with-an-explicit-client.md) (`Model.create(client, ...)`),
and [0024](0024-stream-records-decode-into-model-changes.md) (`Repository.from_item()`)

## Context

[ADR 0007](0007-repositories-over-active-record.md) put persistence in repositories:
`await users.save(user)`. Code that loads an instance, changes it and writes it back has to
carry the repository through every step, and applications asked for the shorter active-record
form: `await user.save()`, `await user.delete()`, `await user.increment(User.visits)`. Active
record usually binds a model class to one global connection. That would bring back what 0007
avoided: global state, one client per model, and persistence logic outside repositories.

## Decision

* A repository that reads or writes an instance records itself in the instance's persistence
  state: `ModelState.repository`, next to `ModelState.stored` (the item as last read or
  written). The two always change together, so an instance knows what it last saw and where.
* Instances of `Model` and `PydanticModel` get `save()`, `delete()`, `update(*actions)`,
  `add()`, `increment()`, `decrement()` and `refresh()`. Each calls a public method of that
  repository (`save`, `delete`, `update`, `refresh`), so validation, hooks, versions, unique
  guards, automatic and derived fields and error mapping are the repository's, and so are the
  overrides of repository subclasses. `add()`, `increment()` and `decrement()` only build
  update actions, validated against the attribute's type before any I/O.
* Model classes still hold no client. Instances no repository read or wrote (new ones,
  `Model.from_item()`, `Model.copy()`, unpickled ones) are unbound: their methods raise
  `RuntimeError` before any I/O and name the repository call to use instead.
* Transactions and batch writers write an instance through the repository it is bound to when
  that repository belongs to their client, and through their own otherwise, so the binding and
  the stored copy stay together.
* `copy.copy()` and `copy.deepcopy()` (and pydantic's `model_copy()`) keep the repository, each
  in a state of its own; pickles leave it out, because it holds the client.
* The method names become part of the model API: `Model` fields cannot use them
  (`attribute_name=` keeps such stored names). Documents (nested maps) have no persistence
  methods, and their fields may now use the names of model methods.

## Consequences

* `user.name = "Ada"; await user.save()` and `await page.increment(Page.views)` work without
  global state. Several clients (accounts, regions, test stores) still share the models, and an
  instance follows the client that last wrote it.
* Repositories stay the place for queries and domain operations; the instance methods are
  shortcuts to them, not a second write path.
* A bound instance keeps its repository and client alive; after `client.close()` its methods
  raise `ShutdownError`.
* Fields named `save`, `delete`, `update`, `add`, `increment`, `decrement` or `refresh` need
  another Python name (a breaking change, acceptable in 0.x).
