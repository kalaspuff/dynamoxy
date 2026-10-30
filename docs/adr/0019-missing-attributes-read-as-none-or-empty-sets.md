# 0019: A missing attribute reads as `None` or an empty set where the field can hold one

Status: Accepted (amends [0008](0008-stored-formats.md))

## Context

dynamoxy stores `None` and empty sets as no attribute ([ADR 0008](0008-stored-formats.md)):
DynamoDB has no empty sets, and `set(None)` of a model field removes the attribute. Reads filled
a missing attribute with the field's default first, so that items written before a field existed
decode. For a field whose default is neither `None` nor empty, a missing attribute meant two
things, and writes did not read back as written:

* `nickname: Attr[str | None] = field(default="anon")` written as `None` read back as `"anon"`.
* `roles: Attr[set[str]] = field(default_factory=lambda: {"member"})` written as an empty set,
  or emptied by `update(user, User.roles.delete({"member"}))` (DynamoDB removes a set whose last
  element is deleted), read back as `{"member"}`: a revoked role was granted again, and the next
  `save()` stored it. Pydantic models and documents behaved the same.

Storing DynamoDB's `NULL` for such values, so that a missing attribute could keep meaning the
default, does not work: `ADD` and `DELETE` fail on a `NULL` (`add()` and `increment()` after
it), a `DELETE` that empties a set removes the attribute anyway, and DynamoDB refuses a `NULL`
in an index key attribute. Refusing such declarations would take away defaults for new instances
of these fields, and documenting the behaviour would leave revoked roles coming back.

## Decision

* A missing attribute reads as `None` in a field that can be `None` (`X | None`, and automatic
  and derived fields), and as an empty set in a set field (`set[X]`, `frozenset[X]`): the values
  dynamoxy stores as no attribute. Other fields read it as their default, or raise
  `ItemDecodeError` when they have none, as before. Defaults fill new instances as before.
* A set field that can be `None` (`set[X] | None`) stores `None` and an empty set alike, and reads
  both back as `None`.
* Updates of model fields follow: `set(set())` removes the attribute, like `set(None)` (it stored
  a `NULL`); `remove()` and `set(set())` of a set field empty it, checked against the field's
  constraints (they raised "is required and cannot be removed"); `set_if_not_exists(set())`
  raises like `set_if_not_exists(None)`; and `== set()` and `!= set()` compare like `== None` and
  `!= None`.
* The stored format does not change.

## Consequences

* What a write stores reads back the same, also through `save()`, updates, transactions,
  pydantic models and documents.
* Items without the attribute of an `X | None` field whose default is not `None`, or of a set
  field whose default is not empty (typically written before the field existed), read `None` or
  an empty set instead of the default: a breaking change for such fields. To give those items
  the default, backfill it (`update(item, Model.field.set_if_not_exists(default))` for each item
  of a scan that has no such attribute), or make the field required (`str` instead of
  `str | None`) when it never needs to be `None`.
* A `set[X] | None` field cannot tell `None` from an empty set; declare `set[X]` when the
  difference does not matter.
