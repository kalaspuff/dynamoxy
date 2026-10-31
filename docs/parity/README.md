# Parity with DynamoDB

dynamoxy's in-memory backend models DynamoDB's semantics and error messages, so that tests on it
fail where production would. Where the DynamoDB documentation, moto's `aws_verified` tests and
dynoxide-rs's captures say nothing, the answers come from DynamoDB itself: this directory keeps
what was asked, what DynamoDB answered, and what the emulators answer, for the next change that
needs them.

## The probes

[`scripts/probe_dynamodb.sh`](../../scripts/probe_dynamodb.sh) asks DynamoDB with the AWS CLI. It
creates tables named `dynamoxy-probe-<random>-*`, logs every request and answer (account ids
masked), and deletes the tables at the end. A maintainer with a test account runs it:

```bash
AWS_PROFILE=<test-account-profile> AWS_REGION=eu-north-1 bash scripts/probe_dynamodb.sh 1   # or 2, 3
```

`DYNAMODB_PROBE_ENDPOINT=<url>` sends the same requests to an emulator. Round 1 asks the
questions issues #84 to #106 left open; round 2 asks what round 1 left open; round 3 asks where
LocalStack and dynamoxy's tests disagree. Write new rounds for new questions, and keep their logs
here. The emulator logs were taken with AWS CLI 1.46.2.

| Log | Target |
| --- | --- |
| [aws-eu-north-1-2026-10-31-round1.log](aws-eu-north-1-2026-10-31-round1.log), [round 2](aws-eu-north-1-2026-10-31-round2.log), [round 3](aws-eu-north-1-2026-10-31-round3.log) | DynamoDB, eu-north-1, 2026-10-31 (AWS CLI 2.37.6) |
| [ministack-1.5.33-round1.log](ministack-1.5.33-round1.log), [round 2](ministack-1.5.33-round2.log), [round 3](ministack-1.5.33-round3.log) | MiniStack 1.5.33 (the emulator of dynamoxy's e2e tests and CI) |
| [localstack-2026.3.0-round1.log](localstack-2026.3.0-round1.log), [round 2](localstack-2026.3.0-round2.log), [round 3](localstack-2026.3.0-round3.log) | LocalStack 2026.3.0, whose DynamoDB is DynamoDB Local 3.3.2 |

## Probing dynamoxy itself

[`scripts/probe_backends.py`](../../scripts/probe_backends.py) asks the same questions through
dynamoxy (models, tables, transactions, PartiQL, native calls) on any target, the in-memory
backend included, and writes the facts to JSON: outcomes, exception classes, codes, messages,
cancellation reasons, sizes and counts. `compare` lists what differs between two runs.

```bash
uv run python scripts/probe_backends.py run --target memory                # probe-memory.json
uv run python scripts/probe_backends.py run --target ministack --endpoint http://127.0.0.1:4599
uv run python scripts/probe_backends.py run --target localstack            # http://127.0.0.1:4566
AWS_PROFILE=<test-account-profile> uv run python scripts/probe_backends.py run --target aws --region eu-north-1
uv run python scripts/probe_backends.py compare probe-aws.json probe-memory.json
uv run python scripts/probe_backends.py iam-policy --account <account-id>  # what a run on AWS needs
```

A run on AWS takes about 15 minutes and costs cents; its tables are named `dynamoxy-probe-*` and
deleted at the end (`cleanup --target aws` deletes what an interrupted run left). The runs are
kept here: [DynamoDB](probe-aws-eu-north-1-2026-10-31.json) (eu-north-1, 2026-10-31),
[the in-memory backend](probe-memory.json), [MiniStack](probe-ministack-1.5.33.json) and
[LocalStack](probe-localstack-2026.3.0.json).

Every fact the DynamoDB run holds, the in-memory backend's run holds alike. That run stopped two
scenarios early: on-demand throttling of eleven 390 KB writes to a new table ended `sizes` before
its `TransactGetItems` and `UpdateItem` facts, and a backfill longer than the 300-second wait ended
`tables` before its billing facts (the probe now retries and waits longer). It also showed that
DynamoDB refuses `DescribeTimeToLive` of a table it is still creating, and how conditions, deletes
and checks count toward a transaction's payload. Later variants of the `sizes` scenario (the other
condition forms) have no DynamoDB answer yet. MiniStack differs from DynamoDB in 63 facts and
LocalStack in 58, each an emulator difference listed below.

## What DynamoDB answered

"Refused" means a `ValidationException` with the message given, unless another error is named.
"memory" is dynamoxy's in-memory backend.

### Reads of attributes an index does not hold (#106)

| Request (index `KEYS_ONLY`, `secret` not projected) | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `Query` of a local or global index, `FilterExpression` on `secret`, with any `Select` (`ALL_ATTRIBUTES`, `COUNT`) or a `ProjectionExpression` the local index fetches | refused: `One or more parameter values were invalid: Secondary index lsi does not project one or more filter attributes: [secret]` | returns the item | same as DynamoDB |
| the same, `attribute_not_exists(#t) OR #t = :e` on `_type` | refused, each occurrence named: `[_type, _type]` | returns the item | refused, `[_type]` |
| `Query` or `Scan` of a global index, `ProjectionExpression` naming `secret` | refused: `Global secondary index gsi does not project [secret]` | returns what the index holds | same as DynamoDB |
| `Query` or `Scan` of a local index, `ProjectionExpression` naming `secret` | returns `secret`, read from the table | returns what the index holds | same as DynamoDB |
| `Scan` of a local or global index, `FilterExpression` on `secret` | matches nothing (filters the entries) | returns the item | returns the item |
| `Scan` of a local index that fetches the items (`ALL_ATTRIBUTES`, or a projection of `secret`), `FilterExpression` on `secret` | matches: the filter sees the fetched item | returns the item | returns the item |
| `Query` filter on several attributes the index does not hold (`secret = :s AND note = :o`, a nested `m.x`, `size(secret)`, the global index's key `gk` next to `secret`) | each named by its top-level name, in the order of the expression: `[secret, note]`, `[m]`, `[secret]`, `[gk, secret]` (a filter that also names a held attribute was not probed) | returns the item | sorted: `[note, secret]` |
| `ProjectionExpression` of a global index naming `note, m.x, pk, secret` | `Global secondary index gsi does not project [note, m, secret]` (in order, held ones left out) | returns `pk` | `[note, secret, m]` |
| a `Scan` of a global index with both | the projection is refused first | returns an empty item | same as DynamoDB |

The in-memory backend answers like DynamoDB, and model reads refuse these filters and projections
before any request ([ADR 0021](../adr/0021-index-reads-follow-dynamodbs-refusals.md)).

### Expression limits (#97)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `#name` / `:value` placeholder of 255 bytes (with `#`/`:`) | accepted | accepted | accepted |
| placeholder of 256 bytes or more | `ExpressionAttributeNames contains invalid key: The expression attribute map contains a key that is too long; size of key: 256` (`ExpressionAttributeValues` alike) | accepted | same; for values the message ends after `too long;` |
| placeholder `#a-b` or `:a-b`, used or not | `ExpressionAttributeNames contains invalid key: Syntax error; key: "#a-b"` (values alike) | `#a` undefined, or `#a-b` unused | same as DynamoDB |
| attribute name of 65,535 bytes, in an item or through a placeholder | accepted | accepted | `InternalFailure` in an item |
| attribute name of 65,536 bytes in a `PutItem` item | `1 validation error detected: Attribute name is too large, must be less than 65536 bytes` | accepted | `InternalFailure` |
| the same through a placeholder of a `GetItem` | `Attribute name is too large, must be less than 65536 bytes for key #n` | accepted | prefixed `ExpressionAttributeNames contains invalid value: ` |
| the same as a map key in a `PutItem` item, or through a placeholder of an `UpdateItem` `SET` | `1 validation error detected: Attribute name is too large, must be less than 65536 bytes` (`... for key #n`) | accepted | `InternalFailure` for the map key; the placeholder prefixed `ExpressionAttributeNames contains invalid value: `, without the envelope |
| a 256-byte or `#a-b` placeholder, or `attribute_type(a, b)`, in a `PutItem` condition | the same messages as in reads, inside `1 validation error detected: ` | the 256-byte one accepted; `#a` undefined (in the envelope); `attribute_type` evaluated | the same messages, without the envelope |
| `attribute_type(a, b)`, a path as the type, in a filter | `Invalid FilterExpression: Incorrect operand type for operator or function; operator or function: attribute_type, operand type: {S,SS,N,NS,B,BS,BOOL,NULL,L,M,HD,DOUBLE,FLOAT,HDS,FS,DOUBLESET,DICT,DECIMAL,INT,DECIMALSET,INTSET}` | evaluated (matches) | refused, the types in another order |
| the same in an `UpdateItem` condition | prefixed `1 validation error detected: ` (`Invalid ConditionExpression: ...`) | evaluated | refused without the prefix |
| `ExecuteTransaction` of `SELECT`s, one of a missing table | `TransactionCanceledException`, reasons `[None, ResourceNotFound]` (`Requested resource not found` on the missing one, no message on the others) | `ResourceNotFoundException` | `ResourceNotFoundException` |
| `TransactGetItems` of a missing table | `ResourceNotFoundException: Requested resource not found` | same | `Cannot do operations on a non-existent table` |

The in-memory backend answers like DynamoDB. It also puts the expression and placeholder errors of
`PutItem`, `UpdateItem` and `DeleteItem`, and the attribute names they refuse in an item, in the
`1 validation error detected: ` envelope (captured by dynoxide-rs in us-east-1 and eu-west-2, and
measured in rounds 1 and 2; an update of a key attribute stays bare), and refuses a map key of
64 KB or more like a top-level name (round 2).

### Size limits (#93)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `PutItem` of a 190 KB item that an `ALL` local index holds (the item and its entry: 380 KB) | accepted, 191 write units for the table and 191 for the index | same | same |
| the same at 210 KB or 300 KB (over 400 KB together) | `Item size has exceeded the maximum allowed size` | accepted | same as DynamoDB |
| 300 KB without the index key, or held by a `KEYS_ONLY` local index | accepted | accepted | accepted |
| `UpdateItem` growing that item from 150 KB to 210 KB | `Item size to update has exceeded the maximum allowed size` | accepted | accepted |
| `TransactWriteItems` `Put` of the 210 KB item | `TransactionCanceledException`, reason `ValidationError`: `Item size has exceeded the maximum allowed size` | accepted | top-level `ValidationException` |
| `PutItem` of an item an `ALL` local index holds, at the byte | 204,800 bytes taken, 204,801 refused: the entry counts as the item, without overhead | accepted | same as DynamoDB |
| the same for a `KEYS_ONLY` local index (18 bytes of keys) | 409,580 bytes taken, 409,588 refused: the entry counts as the keys it holds | accepted | 409,589 taken, 409,600 refused |
| `TransactGetItems` of ten 390 KB items (3.9 MB) | accepted | accepted | accepted |
| `TransactGetItems` of eleven (4.3 MB) | `Payload in response is too large.` | accepted | the same message with the size appended (`too large.4393159`) |
| `TransactGetItems` of eleven 300 KB binary items (4.4 MB as base64), or of 4,000,900 bytes of items | accepted: the limit counts the items, and lies above 4,000,900 bytes and at most at the 4,393,159 refused in round 1 (presumably 4 MiB, 4,194,304, which was not probed) | accepted | accepted |
| `TransactWriteItems` of ten `Update`s writing 390 KB each | accepted | accepted | accepted |
| the same with eleven | `Transaction payload size cannot exceed 4MB. Payload Size: 4393445` | `Transaction request exceeds the 4MB transaction size limit` | `... Payload Size: 4393214` |

The in-memory backend refuses these with DynamoDB's messages and kinds, counts a local index entry
as the attributes it holds, the 4 MB of a read as the items returned (4 MiB), and the payload
size of a write transaction as DynamoDB does, from the request: a put's item, an update's key,
request charge and 8 bytes, a delete's and a check's key, and each one's condition, by the rule
below (measured for every condition form by dynamoxy's probe on AWS). It holds a standalone
`UpdateItem` to 400 KB by the same update charge before it reads the item (dynoxide-rs), and
reports item collection sizes as whole gigabytes, `[0.0, 1.0]` for a small collection.

How DynamoDB counts that payload size (round 2, one change at a time to eleven 390 KB updates
that read 4,393,455): a `Put` counts its item's size (eleven puts of the same items: 4,393,125). An
`Update` counts its key, the update's charge as dynoxide-rs measured it for `UpdateItem` (3 bytes,
19 for each value it sets plus the target's name and the value's size, 1 for each attribute it
removes), and 8 bytes more: a second `SET note = :w` of `"x"` added 24 bytes per update, three more
bytes of key 3. The table's name and placeholder names count nothing, and an
`attribute_not_exists(absent)` condition added 73 bytes per update. Eleven updates of 363,700
bytes were taken, so the limit lies above about 4,001,200 bytes (presumably 4 MiB; not probed).

| Eleven 390 KB updates (`SET filler = :v`, keys `upd`/`00`..`10`), changed in one way | Payload Size | Source |
| --- | --- | --- |
| none | 4,393,455 | round 2, `93-tx-base`; probe `sizes.payload_updates_11` |
| a second `SET note = :w` of `"x"` | 4,393,719 | round 2, `93-tx-expression` |
| a longer table name, or `#f` for `filler` | 4,393,455 | round 2, `93-tx-long-table`, `93-tx-names` |
| a key `updupd` | 4,393,488 | round 2, `93-tx-long-key`; probe `sizes.payload_updates_longer_key` |
| eleven puts of the same items instead | 4,393,125 | round 2, `93-tx-puts`; probe `sizes.payload_puts_11` |
| `attribute_not_exists(absent)` on each | 4,394,258 | round 2, `93-tx-condition` |
| `attribute_exists(pk)` on each | 4,394,170 | probe `sizes.payload_updates_condition_exists_pk` |
| `attribute_exists(abcdefghij)` on each | 4,394,258 | probe `sizes.payload_updates_condition_exists_long` |
| `note = :w` on each | 4,394,137 | probe `sizes.payload_updates_condition_compare` |
| a delete of a 11-byte key added | 4,393,466 | probe `sizes.payload_updates_plus_delete` |
| a check `attribute_not_exists(pk)` of that key added | 4,393,535 | probe `sizes.payload_updates_plus_check` |
| a put of that key with `attribute_not_exists(pk)` added | 4,393,535 | probe `sizes.payload_updates_plus_put_with_condition` |
| `attribute_exists(ab.cd)` on each | 4,394,346 | probe run 3 `sizes.payload_updates_condition_exists_nested` |
| `note = alt` (two paths) on each | 4,394,324 | probe run 3 `sizes.payload_updates_condition_compare_paths` |
| `size(note) > :w` on each | 4,394,379 | probe run 3 `sizes.payload_updates_condition_compare_size` |
| `note BETWEEN :w AND :w` on each | 4,394,291 | probe run 3 `sizes.payload_updates_condition_between` |
| `note IN (:w, :w)` on each | 4,394,236 | probe run 3 `sizes.payload_updates_condition_in` |
| `begins_with(note, :w)` on each | 4,394,236 | probe run 3 `sizes.payload_updates_condition_begins_with` |
| `contains(note, :w)` on each | 4,394,203 | probe run 3 `sizes.payload_updates_condition_contains` |
| `attribute_type(note, :t)` on each | 4,394,269 | probe run 3 `sizes.payload_updates_condition_attribute_type` |
| `attribute_exists(pk) AND attribute_exists(pk)` on each | 4,395,050 | probe run 3 `sizes.payload_updates_condition_and` |
| `attribute_exists(pk) OR attribute_exists(pk)` on each | 4,395,039 | probe run 3 `sizes.payload_updates_condition_or` |
| `NOT attribute_exists(pk)` on each | 4,394,401 | probe run 3 `sizes.payload_updates_condition_not` |

The probe facts come from dynamoxy's own probe runs on DynamoDB, kept here as
[probe-aws-eu-north-1-2026-10-31.json](probe-aws-eu-north-1-2026-10-31.json) and
[run 3](probe-aws-eu-north-1-2026-10-31-run3.json) (their script, `scripts/probe_backends.py`,
comes with the probe's pull request). They fit one rule, which the in-memory backend follows: a
path in a condition counts as where it is read (1, plus 14 and the element for each element), a
value its size, `size(p)` 22 more than its path, and on top of its operands `attribute_exists`
48, `attribute_not_exists` 52, a comparison 42, `BETWEEN` 55, `IN` 50, `begins_with` 51,
`contains` 48, `attribute_type` 54, `AND` 15, `OR` 14 and `NOT` 21.

### Table updates (#87)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `UpdateTimeToLive` of a table still `CREATING` | `ResourceNotFoundException: Requested resource not found: Table: <name> not found` | accepted (tables are active at once) | accepted (active at once) |
| `DescribeTimeToLive` of a table still `CREATING` (dynamoxy's own probe, `tables.ttl_while_creating`) | `ValidationException: Cannot describe time to live while table is in CREATING state: Current table state is CREATING` | accepted (tables are active at once) | accepted |
| `UpdateTable` of a table still `CREATING` | `ResourceInUseException: Attempt to change a resource which is still in use: Table is being created: <name>` | accepted | accepted |
| switch to `PROVISIONED` with capacity for the table only (one global index) | `One or more parameter values were invalid: ProvisionedThroughput must be specified for index: gsi1` | accepted, the index at 0/0 | `InternalFailure` |
| add a global index without `ProvisionedThroughput` to a provisioned table | `One or more parameter values were invalid: Both ReadCapacityUnits and WriteCapacityUnits must be specified for index: gsi2` | accepted | `InternalFailure` |
| switch back to `PAY_PER_REQUEST` | the indexes' capacity reads 0/0 | 0/0 | keeps 1/1 |
| give an index `ProvisionedThroughput` on an on-demand table | `One or more parameter values were invalid: The only Updates for index: gsi1 when TableThroughputMode is PAY_PER_REQUEST can be to OnDemandThroughput, WarmThroughput` | accepted | `InternalFailure` |
| add a second global index while the first is `CREATING`, the table `UPDATING` or already `ACTIVE` again during the backfill (25 s to 506 s) | `LimitExceededException: Subscriber limit exceeded: Only 1 online index can be created or deleted simultaneously per table` | accepted (indexes are active at once) | same as DynamoDB |

The in-memory backend answers like DynamoDB. It creates tables and indexes at once, but they read
`CREATING` in the first `DescribeTable` after them and `ACTIVE` from the next on, as a caller that
polls until they are active sees them; reads and writes of the items of a table being created are
not refused (DynamoDB's answer to them was not measured).

### Round 3: PartiQL, Time to Live, item collection metrics

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `ExecuteStatement` `INSERT` of an item that exists | `DuplicateItemException: Duplicate primary key exists in table` | same | code `DuplicateItem` |
| the same in `ExecuteTransaction` | cancellation reason `DuplicateItem` | `DuplicateItemException` | `ValidationError` |
| the same in `BatchExecuteStatement` | error code `DuplicateItem` | same | same |
| `ExecuteStatement` `SELECT` with `Limit=2` | `NextToken` and `LastEvaluatedKey` | ignores `Limit` | no `LastEvaluatedKey` |
| `DescribeTimeToLive` of a table deleted and created again | `DISABLED` | same | keeps `ENABLED` |
| `ReturnItemCollectionMetrics=SIZE` of `PutItem`, `BatchWriteItem`, `TransactWriteItems` (a small collection) | each returns `SizeEstimateRangeGB` `[0.0, 1.0]` | `PutItem` only | none |
| `PutItem` of 300 KB of binary | accepted | accepted | accepted |

## The emulators

- **MiniStack** (`uv run ministack`, part of the test dependencies) is what the e2e tests and CI
  use. It accepts most of what DynamoDB refuses above, so tests of those refusals assert on the
  in-memory backend only (`if backend == "memory":`, with a comment saying what MiniStack does).
- **LocalStack** wraps DynamoDB Local, AWS's own emulator, and is closer to DynamoDB. It runs
  without Docker or an auth token from PyPI (Python 3.13; its dependencies pinned to its release
  date):

  ```bash
  uv venv -p 3.13 /tmp/localstack/venv
  uv pip install -p /tmp/localstack/venv/bin/python --exclude-newer 2026-03-31 'localstack-core[runtime]==2026.3.0'
  FILESYSTEM_ROOT=/tmp/localstack/fs GATEWAY_LISTEN=127.0.0.1:4566 SERVICES=dynamodb EAGER_SERVICE_LOADING=1 \
    DYNAMODB_IN_MEMORY=1 DNS_ADDRESS=0 SKIP_SSL_CERT_DOWNLOAD=1 LOCALSTACK_TELEMETRY_DISABLED=1 \
    /tmp/localstack/venv/bin/python -m localstack.runtime.main
  ```

  It downloads a JDK and DynamoDB Local on its first start (about 20 s, then 2-3 s).
  `DYNAMOXY_TEST_ENDPOINT_URL=http://127.0.0.1:4566 uv run pytest -m e2e` runs the suite
  against it, and `DYNAMOXY_TEST_ENDPOINT_URL=http://127.0.0.1:4566 cargo test -p dynamoxy-core
  --test emulator` the Rust parity scenarios; both pass. The tests' `target` fixture
  (`"memory"`, `"ministack"` or `"localstack"`) skips what LocalStack's free edition lacks
  (backups, point-in-time restores, item collection metrics, `LastEvaluatedKey` of PartiQL pages).
  LocalStack also keeps the Time to Live of a table deleted and created again, sends
  `DuplicateItem` where DynamoDB sends `DuplicateItemException` (dynamoxy reads both as
  `DuplicateItemError`), and gives a duplicate `INSERT` in `ExecuteTransaction` the reason
  `ValidationError` where DynamoDB gives `DuplicateItem` (round 3).
