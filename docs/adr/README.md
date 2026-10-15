# Architecture decision records

An ADR records a decision that shapes dynamoxy, with its context and consequences. Write one
when a change alters a layer boundary, a stored or wire format, a default with behavioural
impact, or a guarantee (an invariant in [architecture.md](../architecture.md)). Number them in
order (`NNNN-short-title.md`) and list them below; `scripts/check_consistency.py` checks the list.

Status values: **Accepted**, **Superseded by NNNN**, **Deprecated**.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-rust-core-python-api.md) | Rust core for protocol, timing and I/O; Python for models and application objects | Accepted |
| [0002](0002-aws-sdk-transport-own-retries.md) | The AWS SDK for Rust as transport, with dynamoxy's own retries | Accepted |
| [0003](0003-in-memory-dynamodb.md) | An in-memory DynamoDB in the core, held to parity with the emulator | Accepted |
| [0004](0004-expressions-built-in-python.md) | Expressions are built in Python and parsed, validated and evaluated in Rust | Accepted |
| [0005](0005-unique-constraints-guard-items.md) | Unique attributes with guard items written in transactions | Accepted |
| [0006](0006-attr-descriptors-dataclass-transform.md) | Fields are `Attr[T]` descriptors declared with `dataclass_transform` | Accepted |
| [0007](0007-repositories-over-active-record.md) | Repositories (data mapper) instead of active record | Accepted |
| [0008](0008-stored-formats.md) | Stored formats: codecs, sortable timestamps, entity types, cursors | Accepted |
| [0009](0009-timestamp-formats.md) | Timestamps with a `Z` suffix (`"iso_z"`), chosen per model with `datetime_format` | Accepted |
| [0010](0010-asyncio-bridge.md) | An own asyncio bridge: results delivered through per-loop ports, in-memory calls polled inline | Accepted |
