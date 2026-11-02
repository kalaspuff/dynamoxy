# Architecture decision records

An ADR records a decision that shapes dynamoxy, with its context and consequences. Write one
when a change alters a layer boundary, a stored or wire format, a default with behavioural
impact, or a guarantee (an invariant in [architecture.md](../architecture.md)). Number them in
order (`NNNN-short-title.md`) and list them below; `scripts/check_consistency.py` checks the list.

Status values: **Accepted** (or **Accepted, amended by NNNN** when a later ADR adjusts it),
**Superseded by NNNN**, **Deprecated**.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-rust-core-python-api.md) | Rust core for protocol, timing and I/O; Python for models and application objects | Accepted |
| [0002](0002-aws-sdk-transport-own-retries.md) | The AWS SDK for Rust as transport, with dynamoxy's own retries | Accepted |
| [0003](0003-in-memory-dynamodb.md) | An in-memory DynamoDB in the core, held to parity with the emulator | Accepted |
| [0004](0004-expressions-built-in-python.md) | Expressions are built in Python and parsed, validated and evaluated in Rust | Accepted |
| [0005](0005-unique-constraints-guard-items.md) | Unique attributes with guard items written in transactions | Accepted, amended by 0013 |
| [0006](0006-attr-descriptors-dataclass-transform.md) | Fields are `Attr[T]` descriptors declared with `dataclass_transform` | Accepted |
| [0007](0007-repositories-over-active-record.md) | Repositories (data mapper) instead of active record | Accepted, amended by 0011 |
| [0008](0008-stored-formats.md) | Stored formats: codecs, sortable timestamps, entity types, cursors | Accepted, amended by 0013 and 0019 |
| [0009](0009-timestamp-formats.md) | Timestamps with a `Z` suffix (`"iso_z"`), chosen per model with `datetime_format` | Accepted |
| [0010](0010-asyncio-bridge.md) | An own asyncio bridge: results delivered through per-loop ports, in-memory calls polled inline | Accepted |
| [0011](0011-instances-remember-their-repository.md) | Model instances remember the repository that read or wrote them and persist themselves through it | Accepted, amended by 0017 and 0024 |
| [0012](0012-reads-of-partial-indexes.md) | Model reads of indexes that hold only some attributes: the type attribute is projected, entries are never decoded into instances | Accepted, amended by 0021 |
| [0013](0013-stored-forms-read-back-as-written.md) | Stored numbers and timestamps read back as written: canonical numbers in guards and templates, UTC for formatted datetimes, exact epochs | Accepted |
| [0014](0014-writes-keep-undeclared-attributes.md) | Writes of loaded instances keep the attributes their model does not declare | Accepted |
| [0015](0015-stored-values-decode-exactly.md) | Stored values decode exactly to their declared type, or raise `ItemDecodeError` | Accepted |
| [0016](0016-writes-store-a-write-id.md) | Model writes store a random write id (`_wid`), so a retry recognises its own earlier attempt | Accepted |
| [0017](0017-models-create-items-with-an-explicit-client.md) | Models create items with an explicit client: `Model.create(client, ...)`, typed like the constructor | Accepted, amended by 0018 and 0023 |
| [0018](0018-models-read-items-with-an-explicit-client.md) | Models read items with an explicit client: `Model.get(client, ...)` and `Model.query(client, ...)` | Accepted, amended by 0023 |
| [0019](0019-missing-attributes-read-as-none-or-empty-sets.md) | A missing attribute reads as `None` or an empty set where the field can hold one, not as the field's default | Accepted |
| [0020](0020-documents-keep-undeclared-keys.md) | Documents read from an item keep the map keys their class does not declare | Accepted |
| [0021](0021-index-reads-follow-dynamodbs-refusals.md) | Filters and projections of indexes follow DynamoDB's refusals, measured against it; queries of an index without the type attribute fail | Accepted |
| [0022](0022-versions-are-random-tokens.md) | Versions are random tokens, new on every write, so a stale instance never matches again | Accepted |
| [0023](0023-models-scan-and-instances-create-themselves.md) | `Model.scan(client)`, and instances create themselves with `instance.create(client)` | Accepted |
| [0024](0024-stream-records-decode-into-model-changes.md) | DynamoDB Streams records decode into changes of models (`dynamoxy.streams`); `from_item()` refuses guard items and other models' items | Accepted |
