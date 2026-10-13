# 0005: Unique attributes with guard items written in transactions

Status: Accepted

## Context

Applications need unique values besides the primary key (emails, handles). DynamoDB only
guarantees key uniqueness. Global secondary indexes are eventually consistent and cannot enforce
uniqueness.

## Decision

* For each value of a `unique=True` attribute, store a guard item in the same table with
  partition key `_unique#<Entity>#<attribute>#<value>` (and sort key `_unique` when the table has
  one).
* Every write that sets, changes or removes a unique value, and every delete, runs as a
  transaction containing the item and the guard puts (`attribute_not_exists`) and deletes.
* Batch writes of such models are refused; `update()` requires `set()`/`remove()` for unique
  fields so the new value is known.
* Repository reads exclude guard items.

## Consequences

* Uniqueness holds as long as all writers use dynamoxy (or the same protocol).
* Such writes cost twice the write capacity and an extra write per changed value, and need the
  old value (from the loaded instance or one consistent read).
* The guard key format is a stored format (ADR 0008): changing it needs a migration.
