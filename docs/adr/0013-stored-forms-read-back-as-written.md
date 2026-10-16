# 0013: Stored numbers and timestamps read back as they were written

Status: Accepted (amends [0005](0005-unique-constraints-guard-items.md) and
[0008](0008-stored-formats.md))

## Context

Several stored forms were derived from a Python value as the application gave it, while
DynamoDB stores, and returns, a canonical form. An item read back then produced another form
than the one stored, so writing it did something else than writing it the first time:

* Unique guard keys held `str()` of a number: `Decimal("5.0")` wrote the guard `…#5.0`, but the
  stored item reads back as `5`. Changing or deleting the value deleted `…#5` (someone else's
  guard, or nothing) and left `…#5.0` behind, so the value stayed taken forever; and `5` and
  `5.0` were different values for uniqueness.
* Key templates rendered `Decimal` values with their exponent (`PRICE#1.50`, read back as
  `1.5`), and datetimes formatted with a spec (`{at:%Y-%m-%d}`) in their own time zone (read
  back in UTC). Saving an instance read back wrote a second item under another key.
* `"epoch_ms"` multiplied a float timestamp: about one value in 160 was stored a millisecond
  early, and every load and save moved it again (also in sort keys). `"epoch"` rounded toward
  zero before 1970.

## Decision

* Guard keys write numbers as DynamoDB stores them: no exponent, no trailing fractional zeros,
  `0` for zero; floats as the core converts them (`dynamoxy._utils.number_text`).
* Key templates render `Decimal` values the same way, and convert aware datetimes to UTC before
  formatting them with a spec. Other values render as before (floats, timedeltas and enums
  already read back the same).
* Epoch formats are computed with integers: whole seconds or milliseconds since 1970, rounded
  down; decoding is exact too.

## Consequences

* Values written from now on read back as written: guards follow changes and deletes, saved
  instances keep their keys, epoch timestamps stay put.
* Data written before keeps its old forms:
  * guard items of non-canonical numbers (`…#5.0`, written from `Decimal("5.0")` or `5.0`) no
    longer protect the canonical value; rewrite the items of unique number fields (or their
    guard items) to move them to the new form;
  * items whose templated key was rendered from a non-canonical `Decimal` or a non-UTC datetime
    have a key that their instances no longer render. Move them with `create(item.copy())` and
    a delete of the old key;
  * epoch milliseconds stored a millisecond early stay so: an exact lookup with the original
    datetime misses them by 1 ms (queries by range find them).
