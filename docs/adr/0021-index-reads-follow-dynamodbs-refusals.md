# 0021: Filters and projections of indexes follow DynamoDB's refusals

Status: Accepted (amends [0012](0012-reads-of-partial-indexes.md))

## Context

ADR 0012 assumed that DynamoDB filters the entries of an index and returns what matches: a
filter on an attribute a global index does not hold would never match, a filter of a local index
read with `Select=ALL_ATTRIBUTES` would see the items fetched from the table, and indexes without
the type attribute would let entries pass `attribute_not_exists(_type) OR _type = :entity`. The
in-memory backend did the same, so tests passed.

Measured against DynamoDB in eu-north-1 on 2026-10-31 ([docs/parity](../parity/README.md)),
DynamoDB refuses all of these in a `Query` with `ValidationException`, for global and local
indexes, whatever the query returns (`ALL_ATTRIBUTES`, `COUNT`, a projection fetched from the
table): "One or more parameter values were invalid: Secondary index lsi does not project one or
more filter attributes: [secret]", naming each place the filter reads such an attribute
(`[_type, _type]` for the entity filter). A `ProjectionExpression` of a global index naming them
is refused in queries and scans ("Global secondary index gsi does not project [secret]"). A
`Scan` filter on them matches nothing, and a local index fetches the attributes of a projection
from the table, and a scan that fetches the items (`ALL_ATTRIBUTES`, or a projection of what a
local index does not hold) filters the fetched items.

## Decision

* The in-memory backend refuses and evaluates these reads like DynamoDB, with its messages: query
  filters on attributes an index does not hold, projections of a global index naming them, and
  scan filters that see the index entries, or the items when the scan fetches them.
* Model reads raise `ValueError` before any request for a filter on an attribute a partial index
  does not hold, local indexes included, and for `.project(...)` of a global index naming one,
  unless the query adds `.hydrate()`: then the index is read for the keys alone and the
  attributes come from the full items.
* Model reads keep filtering partial indexes on `attribute_not_exists(_type) OR _type = :entity`.
  dynamoxy creates such indexes with the type attribute, so the filter works there; queries of an
  index without it (created by other tools or by dynamoxy 0.2) fail with DynamoDB's
  `ValidationError`, and its scans let the entries pass, as before.

## Consequences

* What fails against DynamoDB fails in tests on the in-memory backend too.
* Filters of partial local indexes, which ADR 0012 sent with `Select=ALL_ATTRIBUTES`, raise
  `ValueError` now; filter the hydrated items in Python, or project the attribute into the index.
* An index without the type attribute has to be created again with it before models with an
  entity type can query it. Dropping the entity filter would have made those queries work, but
  every query of every partial index would then return other entities' entries, counted by
  `count()` and cut by `limit()`.
