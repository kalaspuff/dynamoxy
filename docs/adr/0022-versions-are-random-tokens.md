# 0022: Versions are random tokens

Status: Accepted

## Context

A `version()` field held a counter: 1 on the first write, +1 on every write, and `save()`,
`update()` and `delete()` of a loaded instance required the stored version to be the one it was
loaded with. A counter repeats: `put()` of an instance that was not loaded wrote version 1 over
any stored item, and so did creating an item again after it was deleted. An instance loaded
before at version 1 then passed its check again and overwrote both writes (#84). Reading the
stored version before a put would cost a read per write and stay racy without a condition, and
keeping the count in `put()` would not cover delete-and-create.

## Decision

* A version is a random token (64 bits as 16 hex digits, from the operating system's random
  source), new on every write; version fields are declared `Attr[str]`
  (`Annotated[str | None, Version()]` in pydantic models), and other types are refused with a
  `TypeError` when the model is used (its schema is built).
* Updates set the new token (`SET version = :token`) instead of adding 1, so the new version is
  known without reading the item, also to templates that render it.
* The operations of a transaction with an idempotency token derive their tokens from the token
  and their position, so that the same transaction planned again (an application's retry) sends
  the same request, which DynamoDB requires of a repeated token.
* Carl Oscar decided it, accepting the breaking change: dynamoxy has no users to migrate yet.
  Stored numeric versions are not read (`ItemDecodeError`).

## Consequences

* An instance loaded before an item was replaced, or deleted and created again, never matches the
  stored version: its `save()`, `update()` and `delete()` raise `VersionConflictError`.
* Versions say nothing about how many writes an item had; a counter of its own does that.
* Breaking: `version: Attr[int]` fields must become `Attr[str]`, and code that compared versions as
  numbers compares them for equality only.
* A token reused for another transaction after DynamoDB's 10 minutes derives the same version
  tokens again; tokens are meant to be unique per logical operation anyway.
