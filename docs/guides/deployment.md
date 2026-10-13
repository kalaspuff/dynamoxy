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

## Credentials and IAM

Use roles (instance profiles, ECS task roles, IRSA, Lambda roles) rather than static keys.
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
| Item size | 400 KB (attribute names count) |
| Partition key / sort key | 2048 / 1024 bytes |
| Query/Scan page | 1 MB read before filtering |
| Transaction | 100 operations, 4 MB, each item once |
| BatchWriteItem / BatchGetItem | 25 / 100 per request (dynamoxy chunks) |
| Numbers | 38 significant digits, magnitude 1E-130 to 1E+126 |
| Expressions | 4 KB per expression, 300 operators/functions |
| Local index item collection | 10 GB per partition key value |
| Partition throughput | about 3000 RCU and 1000 WCU per second |

## Costs

- On-demand tables bill per request unit; provisioned tables per capacity unit-hour.
- Writes: 1 WCU per KB; transactional writes and unique-attribute writes cost 2x; every index
  that receives the item costs its own write.
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
