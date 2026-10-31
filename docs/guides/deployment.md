# Deployment

## Processes and clients

- Create one `Client` per process at startup and share it; it is safe to use from many tasks
  concurrently.
- Create clients *after* forking. Servers that fork workers (gunicorn, uvicorn `--workers`,
  multiprocessing, Celery prefork) must create the client in each worker. A client created before
  a fork refuses to work in the child process.
- Close the client at shutdown (`async with` or `await client.close()`) so buffered events reach
  hooks. dynamoxy also stops its runtime cleanly when the interpreter exits.
- AWS Lambda: create the client at module level and reuse it across invocations; dynamoxy
  connects lazily on first use.

## Credentials

Without `credentials=`, dynamoxy uses the AWS SDK's default credential chain, the same one the AWS
CLI and the other AWS SDKs use. The first source that provides credentials wins:

1. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and, for temporary
   credentials, `AWS_SESSION_TOKEN`.
2. The shared files `~/.aws/credentials` and `~/.aws/config`, for the profile given as
   `profile=`, else `AWS_PROFILE`, else `default`. Profiles can use SSO, `credential_process` or
   `role_arn` (assume role).
3. A web identity token: `AWS_WEB_IDENTITY_TOKEN_FILE` with `AWS_ROLE_ARN` (EKS IRSA).
4. Container credentials: ECS task roles and EKS Pod Identity
   (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, `AWS_CONTAINER_CREDENTIALS_FULL_URI`).
5. The EC2 instance metadata service (instance profiles).

AWS Lambda provides the function role's credentials through the environment variables of step 1.

```python
dynamoxy.Client(region="eu-west-1")  # the default chain
dynamoxy.Client(profile="dev")       # the "dev" profile in step 2
dynamoxy.Client(credentials=dynamoxy.Credentials(access_key_id, secret_access_key))  # no chain
```

- `credentials=` replaces the chain: no other source is consulted.
- `profile=` only selects the profile of step 2. Environment variables still come first: with
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` set, `profile="dev"` does not change the
  credentials.
- Credentials are resolved on the first request, not when the client is created or connects, and
  the SDK refreshes temporary credentials before they expire. When no source provides any, the
  first request raises `CredentialsError` (never retried).

Use roles (instance profiles, ECS task roles, IRSA, Lambda roles) rather than static keys.

## Region

The region comes from `region=`, else `AWS_REGION`, else `AWS_DEFAULT_REGION`, else the `region`
of the selected profile in `~/.aws/config`, else the EC2 instance metadata service. When none is
found, connecting (the first call, or `async with`) raises `ConfigurationError`, unless
`endpoint_url` is set: then `us-east-1` is used, which local endpoints such as DynamoDB Local and
MiniStack ignore.

## Environments sharing an account

Several environments (developers, QA, staging) can share one AWS account and region with
`table_prefix=`: models keep their logical names (`table="users"`) and the client adds the
prefix, so `Client(table_prefix="qa-1-")` uses `qa-1-users`. Read the prefix from your settings:

```python
dynamoxy.Client(table_prefix=settings.table_prefix)  # e.g. APP_TABLE_PREFIX="dev-alice-"
```

- The prefix applies to repositories, `client.table()`, batches, transactions and
  `create_tables()` / `ensure_tables()`, so each environment creates and uses its own tables.
- `table_names={"users": "shared-users"}` maps single logical names; these names are used as
  given, without the prefix.
- Separate the environments in IAM too: grant each one only `arn:...:table/qa-1-*` (indexes
  included).

## IAM permissions

Grant the actions you use on the table ARNs and `arn:...:table/<name>/index/*`:

| Feature | Actions |
| --- | --- |
| Reads | `dynamodb:GetItem`, `Query`, `Scan`, `BatchGetItem`, `ConditionCheckItem` |
| Writes | `dynamodb:PutItem`, `UpdateItem`, `DeleteItem`, `BatchWriteItem` |
| Transactions and unique attributes | `dynamodb:TransactWriteItems` is not a separate IAM action: grant the item actions above (and `ConditionCheckItem`) |
| PartiQL | `dynamodb:PartiQLSelect`, `PartiQLInsert`, `PartiQLUpdate`, `PartiQLDelete` |
| `ensure_tables()` and admin | `dynamodb:DescribeTable`, `CreateTable`, `UpdateTable`, `UpdateTimeToLive`, `DescribeTimeToLive`, `ListTables`, `TagResource` |
| Backups | `dynamodb:CreateBackup`, `ListBackups`, `DeleteBackup`, `RestoreTableFromBackup`, `UpdateContinuousBackups`, `DescribeContinuousBackups`, `RestoreTableToPointInTime` |

Fine-grained access control (`dynamodb:LeadingKeys`) works with dynamoxy's key templates:
note that unique guard items live under `_unique#...` partition keys.

## Limits to design for

| Limit | Value |
| --- | --- |
| Item size | 400 KB (attribute names count), together with its entries in the table's local indexes |
| Partition key / sort key | 2048 / 1024 bytes |
| Query/Scan page | 1 MB read before filtering |
| Transaction | 100 operations, 4 MB written (or read), each item once |
| BatchWriteItem / BatchGetItem | 25 / 100 per request (dynamoxy chunks) |
| Numbers | 38 significant digits, magnitude 1E-130 to 1E+126 |
| Nested values (lists and maps) | 32 levels (deeper values raise `ValidationError` before any request) |
| Expressions | 4 KB per expression, 300 operators/functions |
| Local index item collection | 10 GB per partition key value |
| Partition throughput | about 3000 RCU and 1000 WCU per second |

## Costs

- On-demand tables bill per request unit; provisioned tables per capacity unit-hour.
- Writes: 1 WCU per KB; transactional writes and unique-attribute writes cost 2x on the table;
  every index that receives the item costs its own write, not doubled in a transaction.
- Reads: 0.5 RCU per 4 KB eventually consistent, 1 RCU consistent, 2 RCU transactional.
- Filters and projections do not reduce read costs; key design does.
- Measure with `dynamoxy.collect()` in tests and staging, and `dynamoxy.consumed_capacity` in
  production metrics.

## Production checklist

- [ ] Tables managed by infrastructure as code (or `ensure_tables()` in a deploy step), with
      point-in-time recovery and deletion protection enabled.
- [ ] One client per process, created after fork, closed at shutdown.
- [ ] Retry policy and timeouts fit your latency budget.
- [ ] Logging of `dynamoxy.*` at `INFO` (or `WARNING`) with structured output; alerts on
      `request.gave_up` and `operation.failed`.
- [ ] OpenTelemetry configured, if you use tracing.
- [ ] Every access pattern served by a key or index (no scans on request paths).
- [ ] Hot keys spread (write sharding) where traffic concentrates.
- [ ] Unique values and versions used where invariants matter.
