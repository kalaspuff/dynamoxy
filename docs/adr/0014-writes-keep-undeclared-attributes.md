# 0014: Writes of loaded instances keep the attributes their model does not declare

Status: Accepted

## Context

`put()` and `save()` write a whole item: the encoded fields of the model. Attributes of the
stored item that no field declares were dropped. During a rolling deploy, an instance of the
old version of an application loads an item that the new version wrote with a new field, saves
it, and erases the new field; the version check does not help, since the version matched. The
same happens to attributes other services add to shared items.

## Decision

* A write of an instance that was loaded (read, or written by dynamoxy) keeps the attributes of
  its stored item that neither a field of the model nor the type attribute declare: `save()`,
  `put()`, batch puts and their transaction forms.
* Writes of instances that were not loaded replace the item as before, and `create()` never
  merges anything.
* An attribute is removed with an update (`update(item, F("legacy").remove())`).

## Consequences

* Applications can add fields with rolling deploys and share items with other services without
  losing data.
* Retired attributes stay in items until an update removes them; migrations that rewrite items
  with `save()` must remove what they retire explicitly.
* The item size of such writes includes the kept attributes.
