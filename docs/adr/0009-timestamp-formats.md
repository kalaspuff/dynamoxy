# 0009: Timestamps with a `Z` suffix, chosen per model

Status: Accepted

## Context

ADR 0008 stores `datetime` values as fixed-width ISO 8601 text ending in `+00:00`. Many services
and libraries write UTC timestamps in the RFC 3339 style with a `Z` suffix
(`2026-10-13T08:48:47.966937Z`). Applications that share tables with them, or that compare and
sort such values, need dynamoxy to write that style and to read both. Choosing a codec on every
`datetime` field is repetitive and easy to forget on one of them.

## Decision

* `DateTimeCodec` gets a fourth format, `"iso_z"`: UTC with microseconds and a `Z` suffix, fixed
  width like `"iso"`, so text order stays time order.
* The `datetime_format` class keyword of `Model`, `PydanticModel` and `Document` sets the format
  of every `datetime` field of the class and its subclasses (also inside lists, sets and maps)
  and of `created_at()`/`updated_at()`. Fields that choose a codec (`codec=`, `UseCodec`,
  `created_at(format=...)`) keep theirs; TTL attributes stay epoch seconds.
* Each class decides for its own fields: a nested document does not take the format of the model
  it is nested in, because a schema is built once per class.
* Every format reads ISO text in either style: `Z` (or `z`) or an offset, with any number of
  fractional digits.
* The default stays `"iso"`, so nothing stored changes unless a model opts in.

## Consequences

* Items written by other services, or before a model changed its format, decode.
* Keys, key templates, conditions and unique guard keys compare stored text. Changing the format
  of such an attribute on existing data is a migration, like a key change.
* Both ISO styles share their first 26 characters, so mixed values in one attribute still sort by
  time; only equal instants differ (`+00:00` sorts before `Z`).
