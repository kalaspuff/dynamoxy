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
AWS_PROFILE=<test-account-profile> AWS_REGION=eu-north-1 bash scripts/probe_dynamodb.sh 1   # or 2
```

`DYNAMODB_PROBE_ENDPOINT=<url>` sends the same requests to an emulator. Round 1 asks the
questions issues #84 to #106 left open; round 2 asks what round 1 left open; round 3 asks where
LocalStack and dynamoxy's tests disagree. Write new rounds for new questions, and keep their logs
here. The emulator logs were taken with AWS CLI 1.46.2.

| Log | Target |
| --- | --- |
| [aws-eu-north-1-2026-10-31-round1.log](aws-eu-north-1-2026-10-31-round1.log) | DynamoDB, eu-north-1, 2026-10-31 (AWS CLI 2.37.6) |
| [ministack-1.5.33-round1.log](ministack-1.5.33-round1.log), [round 2](ministack-1.5.33-round2.log), [round 3](ministack-1.5.33-round3.log) | MiniStack 1.5.33 (the emulator of dynamoxy's e2e tests and CI) |
| [localstack-2026.3.0-round1.log](localstack-2026.3.0-round1.log), [round 2](localstack-2026.3.0-round2.log), [round 3](localstack-2026.3.0-round3.log) | LocalStack 2026.3.0, whose DynamoDB is DynamoDB Local 3.3.2 |

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
| the same, the scan fetching the items (`ALL_ATTRIBUTES`, or a projection of `secret`) | not measured yet (round 2 asks); PartiQL `SELECT`s filter the entries (dynoxide-rs) | returns the item | returns the item |

The in-memory backend answers like DynamoDB (and, until round 2 says otherwise, filters the entries
in scans that fetch the items too), and model reads refuse these filters and projections before
any request ([ADR 0021](../adr/0021-index-reads-follow-dynamodbs-refusals.md)).

### Expression limits (#97)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `#name` / `:value` placeholder of 255 bytes (with `#`/`:`) | accepted | accepted | accepted |
| placeholder of 256 bytes or more | `ExpressionAttributeNames contains invalid key: The expression attribute map contains a key that is too long; size of key: 256` (`ExpressionAttributeValues` alike) | accepted | same; for values the message ends after `too long;` |
| placeholder `#a-b` or `:a-b`, used or not | `ExpressionAttributeNames contains invalid key: Syntax error; key: "#a-b"` (values alike) | `#a` undefined, or `#a-b` unused | same as DynamoDB |
| attribute name of 65,535 bytes, in an item or through a placeholder | accepted | accepted | `InternalFailure` in an item |
| attribute name of 65,536 bytes in a `PutItem` item | `1 validation error detected: Attribute name is too large, must be less than 65536 bytes` | accepted | `InternalFailure` |
| the same through a placeholder of a `GetItem` | `Attribute name is too large, must be less than 65536 bytes for key #n` | accepted | prefixed `ExpressionAttributeNames contains invalid value: ` |
| `attribute_type(a, b)`, a path as the type, in a filter | `Invalid FilterExpression: Incorrect operand type for operator or function; operator or function: attribute_type, operand type: {S,SS,N,NS,B,BS,BOOL,NULL,L,M,HD,DOUBLE,FLOAT,HDS,FS,DOUBLESET,DICT,DECIMAL,INT,DECIMALSET,INTSET}` | evaluated (matches) | refused, the types in another order |
| the same in an `UpdateItem` condition | prefixed `1 validation error detected: ` (`Invalid ConditionExpression: ...`) | evaluated | refused without the prefix |
| `ExecuteTransaction` of `SELECT`s, one of a missing table | `TransactionCanceledException`, reasons `[None, ResourceNotFound]` (`Requested resource not found` on the missing one, no message on the others) | `ResourceNotFoundException` | `ResourceNotFoundException` |
| `TransactGetItems` of a missing table | `ResourceNotFoundException: Requested resource not found` | same | `Cannot do operations on a non-existent table` |

The in-memory backend answers like DynamoDB. It also puts the expression and placeholder errors of
`PutItem`, `UpdateItem` and `DeleteItem`, and the attribute names they refuse in an item, in the
`1 validation error detected: ` envelope (captured by dynoxide-rs in us-east-1 and eu-west-2; an
update of a key attribute stays bare), and refuses a map key of 64 KB or more like a top-level
name, which round 2 asks DynamoDB about.

### Size limits (#93)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `PutItem` of a 190 KB item that an `ALL` local index holds (the item and its entry: 380 KB) | accepted, 191 write units for the table and 191 for the index | same | same |
| the same at 210 KB or 300 KB (over 400 KB together) | `Item size has exceeded the maximum allowed size` | accepted | same as DynamoDB |
| 300 KB without the index key, or held by a `KEYS_ONLY` local index | accepted | accepted | accepted |
| `UpdateItem` growing that item from 150 KB to 210 KB | `Item size to update has exceeded the maximum allowed size` | accepted | accepted |
| `TransactWriteItems` `Put` of the 210 KB item | `TransactionCanceledException`, reason `ValidationError`: `Item size has exceeded the maximum allowed size` | accepted | top-level `ValidationException` |
| `TransactGetItems` of ten 390 KB items (3.9 MB) | accepted | accepted | accepted |
| `TransactGetItems` of eleven (4.3 MB) | `Payload in response is too large.` | accepted | the same message with the size appended (`too large.4393159`) |
| `TransactWriteItems` of ten `Update`s writing 390 KB each | accepted | accepted | accepted |
| the same with eleven | `Transaction payload size cannot exceed 4MB. Payload Size: 4393445` | `Transaction request exceeds the 4MB transaction size limit` | `... Payload Size: 4393214` |

The in-memory backend refuses these with DynamoDB's messages and kinds. Until round 2 measures
them, it counts a local index entry as the attributes the index holds (without the 100 bytes of
overhead the documentation mentions for storage; DynamoDB Local does the same), counts the
4 MB of a read as the items returned, and reports its own count of the items written as the
payload size (4,393,081 for the request above, where DynamoDB says 4,393,445).

### Table updates (#87)

| Request | DynamoDB | MiniStack | LocalStack |
| --- | --- | --- | --- |
| `UpdateTimeToLive` of a table still `CREATING` | `ResourceNotFoundException: Requested resource not found: Table: <name> not found` | accepted (tables are active at once) | accepted (active at once) |
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
  `DYNAMOXY_TEST_ENDPOINT_URL=http://127.0.0.1:4566 uv run pytest` runs the suite against it.
