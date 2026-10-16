# 0016: Model writes store a write id, so a retry recognises its own earlier attempt

Status: Accepted

## Context

An attempt that times out, loses its connection or gets a 5xx may have been applied. Since the
core no longer reports an answer that such an attempt may have caused as a definite one
(`WriteOutcomeUnknownError`), a model write needs a way to tell whether its earlier attempt was
applied, or `create()` and `save()` fail for every timeout of a write that did happen:

* The stored item compared with the item the write sent is wrong exactly where it matters: two
  workers claiming a job by `create()`-ing identical items (a deduplication table keyed by a
  message id) would both see "their" item and both go ahead.
* The version cannot tell either: `put()` of a versioned instance writes `version + 1` without a
  condition, so a stored version that this write would have written may still be another
  writer's, written after this write.
* Sending every conditional write as a transaction with a client request token would settle it,
  at twice the write units of every such write.

## Decision

* Every model write that runs alone (`create()`, `put()`, `save()`, `update()` and the instance
  methods) stores a new random id in the item's `_wid` attribute: 64 bits from the operating
  system's random source, as 16 hex digits. Every attempt of one write sends the same id.
* When the core reports an unknown outcome, the repository reads the item that DynamoDB returned
  to the next attempt (with its failed condition), or reads the item with strong consistency
  when there is none (an update that was not sent again). If it holds this write's id, the
  write was applied: the call succeeds, runs its hooks once, emits `request.outcome_settled`, and
  `update()` returns that item. Otherwise the outcome stays unknown, and
  `WriteOutcomeUnknownError.item` is the item as stored now.
* Transactions and batch writes store no id: a transaction's token makes its retries exact, and
  the same transaction sent again with the same token must be identical. Their items keep the
  id of an earlier write, which no later write can match.
* `write_ids=False` (a class keyword, inherited) stores no id; such models raise
  `WriteOutcomeUnknownError` for these writes. A field stored as `_wid` requires it.

## Consequences

* A write whose earlier attempt was applied, and that no other write replaced since, succeeds:
  a timed-out `create()`, `save()` or `increment()` is neither a false conflict nor applied
  twice. When another writer wrote in between, or the attempt may still be in flight, the
  outcome stays unknown.
* Items of models gain the `_wid` attribute (about 20 bytes), which other readers, streams and
  exports see. Models do not declare it, it is never part of an instance, and every write that
  stores one replaces the one stored before (ADR 0014 keeps other undeclared attributes).
* The dict-level API (`client.table(...)`) stores no id: its writes raise
  `WriteOutcomeUnknownError` when the outcome is unknown.
