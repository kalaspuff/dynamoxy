# 0012: Model reads of indexes that hold only some attributes

Status: Accepted, amended by [0021](0021-index-reads-follow-dynamodbs-refusals.md)

## Context

Entries of an index with a `KEYS_ONLY` or `INCLUDE` projection hold the keys and the projected
attributes only. Model reads of such indexes went wrong in two ways:

* For models with an entity type (single-table design), every read filters on the type
  attribute (`_type`). DynamoDB filters the index entries, which did not hold it, so queries of
  such indexes matched nothing, also with `.hydrate()`, which only fetched what the query found.
* Entries were decoded into instances: fields the index does not hold got their defaults
  (`Counter(count=0)` for a stored 42), without an error when every such field has a default or
  is optional. Writing such an instance stored the defaults.

## Decision

* When dynamoxy creates a `KEYS_ONLY` or `INCLUDE` index for a model with an entity type, it
  projects the type attribute too (`INCLUDE` of `_type`). Reads of an index declared without it
  filter on `attribute_not_exists(_type) OR _type = :entity`, so that indexes created before keep
  their entries, and the entity of hydrated items is checked on the client.
* Model reads of a global index that does not hold every field of the model raise `ValueError`
  before any request, unless they read full items (`.hydrate()`) or dicts (`.project(...)`).
  `count()` and `exists()` (which reads keys only) need neither.
* Model reads of such a local index ask DynamoDB for every attribute (`Select=ALL_ATTRIBUTES`),
  which DynamoDB reads from the table.
* A filter of such a global index that reads an attribute the index does not hold raises
  `ValueError`: it could never match.

## Consequences

* Model reads never return made-up values, and single-table models work with partial indexes.
* Code that read partial instances from a global index raises until it adds `.hydrate()` or
  `.project(...)`: a breaking change of a behaviour that lost data.
* Indexes created before hold no type attribute: their queries return the entries of every
  entity until they are hydrated.
