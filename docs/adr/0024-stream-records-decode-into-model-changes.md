# 0024: Stream records decode into changes of models; `from_item()` refuses items that are not the model's

Status: Accepted (amends [0011](0011-instances-remember-their-repository.md))

## Context

DynamoDB Streams and Lambda are the usual way to react to writes, but turning a stream record
into a model took hand-written plumbing that failed on records dynamoxy itself produces (issue
#183): writes of models with unique values also write guard items to the same table, which the
stream carries; single-table designs carry every entity of the table; `KEYS_ONLY` streams, and
some records of the other view types, hold no image at all; and `Model.from_item()` neither
looked at the type attribute nor recognised guard items, so it failed on missing fields, or
decoded another entity's item as its own model.

## Decision

* `dynamoxy.streams.changes(event, *models, client=None)` takes a Lambda event, `GetRecords`
  records or one record, and yields a typed `Change` per record of the given models, in the
  records' order. It is a pure-Python module with no dependency.
* Which model a record is, decided per image:
  * guard items (partition key starting with `_unique#`) are skipped;
  * an image whose type attribute names the entity of a model given is that model's; another
    entity is skipped, so a handler lists only the entities it handles;
  * an image without a type attribute is the model's without an entity type, at most one per
    table, and is skipped when there is none. This is the rule of `client.collection()` and of
    typed repository reads, which filter on the type attribute: entity models only ever read
    items that carry their type.
  * a record without images is the model's whose key it fits (key attributes, their types, the
    prefixes and constants of key templates). A key that fits several models raises: skipping
    it would drop a change silently, and picking one would guess.
* A record that cannot become a change raises `StreamRecordError`, a subclass of
  `ItemDecodeError` with the record's `sequence_number` and `event_id`, so a Lambda function can
  report it as its batch item failure. The models are checked when `changes()` is called; each
  record when the iterator reaches it, so the changes before a failing record are processed
  first and Lambda retries from that record.
* Without `client=`, the physical table names are unknown, so every record is read as one of
  the models: models whose items cannot be told apart (two without an entity type, or two with
  the same entity) are refused with `TypeError`. With `client=`, records are routed by the
  table of `eventSourceARN` (the client's `table_prefix` and `table_names` apply), records of
  other tables are skipped, and the instances are bound to the client's repositories.
* `Repository.from_item(item)` decodes an item from elsewhere (a stream image, an export) into
  an instance bound to the repository, as if read through it: `after_load` hooks run, and its
  writes check the item's version. The name mirrors `Model.from_item()`, which keeps returning
  unbound instances (ADR 0011).
* `Model.from_item()`, `PydanticModel.from_item()` and `Repository.from_item()` refuse guard
  items, and items whose type attribute names another model registered for the table, with an
  `ItemDecodeError` that says what the item is. Entity names no model declares (an entity
  renamed since) still decode, as in repository reads.

## Consequences

* A Lambda handler is a loop over `streams.changes(event, ...)` that reports
  `StreamRecordError.sequence_number`; guard items and entities it does not handle never reach
  it.
* Items without a type attribute in a table of entity models (written by other tools) are not
  changes of those models; a model without an entity type declared for the table receives them.
* A `KEYS_ONLY` stream of a single-table design needs key templates with distinct prefixes per
  entity, or it fails on the first ambiguous key.
* Records of Kinesis Data Streams for DynamoDB are refused (#226), and the in-memory backend
  keeps no stream (#227).
