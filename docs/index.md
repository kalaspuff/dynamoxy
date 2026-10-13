# dynamoxy documentation

dynamoxy is a typed, async DynamoDB ORM for Python with a Rust core. Start with
[Getting started](guides/getting-started.md); the compact all-in-one reference is the
[usage guide](../python/dynamoxy/AGENT_GUIDE.md) (also printed by `dynamoxy guide`).

## Guides

| Guide | Covers |
| --- | --- |
| [Getting started](guides/getting-started.md) | Install, a first model, connecting to AWS, an emulator or the in-memory DynamoDB |
| [Modeling](guides/modeling.md) | Fields, codecs, validation, automatic fields, keys, indexes, documents, hooks |
| [Reading data](guides/reading-data.md) | Gets, queries, sort key conditions, filters, pagination and cursors, scans, projections |
| [Writing data](guides/writing-data.md) | create/put/save/update/delete, update actions, conditions, upserts, batches |
| [Constraints and concurrency](guides/constraints-and-concurrency.md) | Unique values, optimistic locking, immutable fields, idempotency |
| [Transactions](guides/transactions.md) | Atomic writes, checks, cancellation reasons, tokens, transactional reads |
| [Single-table design](guides/single-table-design.md) | Entities, key templates, generic indexes, collections |
| [Pydantic models](guides/pydantic.md) | Using pydantic models as tables |
| [Tables](guides/tables.md) | Creating and evolving tables, TTL, streams, backups, point-in-time recovery, tags |
| [Low-level access](guides/low-level-access.md) | The dict-level table API, PartiQL, DynamoDB's typed JSON |
| [Errors and retries](guides/errors-and-retries.md) | The error tree, retry policy, timeouts, what to catch |
| [Observability](guides/observability.md) | Events, logging, hooks, middleware, OpenTelemetry, cost accounting, metrics |
| [Testing](guides/testing.md) | The in-memory DynamoDB, fault injection, emulators, testing patterns |
| [Application patterns](guides/patterns.md) | Repositories, services, unit of work, web apps, consumers, migrations |
| [Deployment](guides/deployment.md) | Processes, IAM permissions, limits, costs, production checklist |

## Reference

| Page | Contents |
| --- | --- |
| [Configuration](reference/configuration.md) | Every option of `Client`, policies, field specifiers, indexes and table creation (checked against the code) |
| [Errors](reference/errors.md) | Every exception and when it is raised (checked) |
| [Events](reference/events.md) | Every event kind with logger and level (generated) |

## Design

- [Architecture](architecture.md): layers, pipelines, invariants, concurrency, extension points.
- [Architecture decision records](adr/README.md): why things are the way they are.
- [Examples](../examples/README.md): runnable programs, executed by the test suite.
