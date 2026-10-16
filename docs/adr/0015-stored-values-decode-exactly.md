# 0015: Stored values decode exactly, or not at all

Status: Accepted

## Context

Codecs decoded stored values with Python's conversions (`int()`, `bool()`, `str()`,
`float()`), which convert instead of checking. Values that another writer stored with another
type, or that the declared type cannot hold, read as something else:

* A stored `2.5` read as `2` in an `int` field (and `2.9` as member `2` of an `IntEnum`), and
  the next `save()`, even one that changed another field, stored the `2`.
* An `int` sort key stored as `1700000000.5` read as `1700000000`: the instance pointed at
  another item, and `delete(instance)` or `update(instance, ...)` changed that item.
* `"false"` read as `True` in a `bool` field, a stored number as text in a `str` field, a stored
  string as a list or set of its characters, and a list as a set. Unions (`int | str`) returned
  stored values unchecked, also types they do not name.

Such values come from other services, from older versions of a model with another type, and
from bugs (`increment(0.5)` of an `int` field in 0.2 and earlier).

## Decision

A codec decodes a stored value only to a value of its declared type that holds it exactly;
anything else raises `ValueError`, which the schema reports as `ItemDecodeError` naming the
field and the stored type (`credits: cannot decode a stored Decimal as int`).

* `int` (and integer enums and literals) take whole numbers; `float` and `Decimal` take numbers;
  `bool` takes booleans; `str`, `UUID`, `date` and ISO datetimes take text; epoch datetimes take
  numbers and ISO text; `bytes` take binary; lists, sets and maps take their own DynamoDB type.
  Enums and literals check membership. Unions take the types they name: a number keeps its type
  when the union names it, and otherwise becomes an `int` (when whole), a `Decimal` or a
  `float`, the first of these that the union names.
* Items built in Python (`from_item()`) may hold equivalent values: a whole `Decimal` or `float`
  for an `int`, a `float` for a `Decimal`, `str` and `int` subclasses.
* Python keeps microseconds: finer fractions of epoch datetimes and `timedelta` seconds are
  rounded, and `datetime.fromisoformat` truncates ISO text. `timedelta` decodes with decimal
  arithmetic, like the other numbers.
* `update()` and `delete()` decode the item DynamoDB returns after the write: their
  `ItemDecodeError` says that the write was applied, so that callers do not repeat it.

## Consequences

* Reads never change a value: what a read returns, a write stores unchanged, and keys of loaded
  instances are the stored keys.
* Valid items cost nothing more: values of the field's exact type skip the codec as before; the
  checks run only for values that used to be converted.
* Items that decoded only by conversion now raise `ItemDecodeError` (a breaking change). Find
  them with a scan of `repository.table` (dicts, no decoding) and rewrite their attributes, or,
  while both forms are stored, declare the field as a union of the old and new types
  (`int | str`), or give it a `codec=` that converts, and write the new form back on save.
