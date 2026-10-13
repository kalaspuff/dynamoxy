# 0008: Stored formats: codecs, sortable timestamps, entity types, cursors

Status: Accepted

## Context

What dynamoxy writes is read later by other versions of dynamoxy, other services and analytics.
Formats must be stable, interoperable and useful in keys.

## Decision

* Values are stored as native DynamoDB types where one exists (numbers as `N`, sets as sets,
  maps as `M`); numbers keep exact decimal precision.
* `datetime` is stored as fixed-width ISO 8601 text in UTC with microseconds
  (`2026-01-02T03:04:05.000000+00:00`), so lexical order is time order in sort keys; naive
  datetimes are rejected. Epoch formats are opt-in, and TTL attributes are epoch seconds. ADR 0009
  adds the opt-in `Z` style (`2026-01-02T03:04:05.000000Z`).
* Enums store their values; `None` and empty sets store nothing.
* The entity type attribute is `_type` (configurable) holding the entity name.
* Unique guard keys follow ADR 0005.
* Cursors are base64url-encoded JSON of the typed `LastEvaluatedKey`; they are opaque to users and
  not signed.

## Consequences

* Items written by dynamoxy are readable by any DynamoDB client.
* Changing any of these formats is a breaking change: keep reading the old format, add an ADR,
  and call it out in the changelog.
