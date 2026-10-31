# 0020: Documents read from an item keep the map keys their class does not declare

Status: Accepted (extends [0014](0014-writes-keep-undeclared-attributes.md))

## Context

Writes of loaded instances keep the item's top-level attributes that no field declares
([ADR 0014](0014-writes-keep-undeclared-attributes.md)), so that an older version of an
application, during a rolling deploy, does not erase a field a newer one added. Fields added to
a document (`address.zip_code`) were still erased: a document decoded from a map held its
declared fields only, and the next `save()` of the item wrote the map without the new key.

Merging the stored map into the encoded one at the item level would keep keys of a document the
application replaced (`user.address = Address(...)`), which belong to the old address.

## Decision

* A document decoded from a stored map (native documents, models used as documents, and
  pydantic models) remembers the map's keys that no field declares, and writes them back
  whenever it is encoded: `save()` and `put()` of its item, and update values
  (`User.history.set([...])`), also in lists and maps of documents and nested documents.
* A document built by the application writes its fields only, so replacing a document replaces
  its map.
* A key is removed with an update of its path (`update(user, F("address")["zip_code"].remove())`)
  or by assigning a new document.

## Consequences

* Applications can add fields to documents with rolling deploys, as they can to models.
* The remembered keys live in the instance's `__dict__`, outside its fields: they do not take
  part in `==`, `repr()` or pydantic's serialization, and exact copies (`copy.copy()`,
  `copy.deepcopy()`, pydantic's `model_copy()`) keep them.
