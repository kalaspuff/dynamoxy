# Observability

dynamoxy reports what it does in four ways: structured events (logging and hooks), OpenTelemetry
spans and metrics, per-request cost collection, and a metrics snapshot. None of them logs item
contents.

## Events and logging

Every event has a kind (`category.action`), a message, structured fields and a level. Events go
to the `dynamoxy.<category>` loggers and to hooks. The full list is in the
[events reference](../reference/events.md) (`dynamoxy events` prints it).

```python
import logging

logging.getLogger("dynamoxy").setLevel(logging.INFO)

client = dynamoxy.Client(
    logging=dynamoxy.LoggingConfig(
        levels={"operation.succeeded": "debug", "request.retry": "warning"},
        disabled=("client.*",),
        include_fields=True,  # "... operation=PutItem table=users attempts=2"
    )
)
```

`levels` takes kinds and globs. An exact kind wins over globs, and of the globs that match a
kind the longest wins, so `{"request.*": "debug", "request.gave_up": "error"}` logs
`request.gave_up` at ERROR and the other request events at DEBUG.

Log records carry the event as `record.dynamoxy` (kind, message, fields, level, timestamp), so
JSON formatters can emit structured logs.

| Category | Kinds | Why you care |
| --- | --- | --- |
| `request` | `retry`, `throttled`, `gave_up`, `slow` | capacity problems, latency |
| `batch` | `unprocessed`, `incomplete` | throttled batch jobs |
| `table` | `created`, `updated`, `deleted` | schema changes made by the application |
| `operation` | `succeeded` (debug), `failed` | every call, with duration, attempts and capacity |
| `client` | `connected`, `closed` | lifecycle |
| `hook`, `events` | `hook.failed`, `events.dropped` | problems in your hooks or an overloaded event buffer |

## Hooks

```python
unsubscribe = client.on("request.throttled", lambda event: statsd.increment("dynamodb.throttled"))


@client.on("operation.failed")
async def alert(event: dynamoxy.Event) -> None:
    await pager.notify(event.message, event.fields)
```

Hooks may be sync or async and match kinds or globs (a typo raises `ValueError`). A failing hook
never affects the operation; it emits `hook.failed`. Events of a call reach hooks before the call
returns, however many there are; async hooks run as tasks. `close()` waits for those of the current event loop before it
closes the client, so they may still use it (an `operation.failed` hook that writes an audit
item); a hook that calls `close()` itself is not waited for. The core records only
event kinds that logging or a hook wants, so unused kinds cost nothing.

## OpenTelemetry

Install `dynamoxy[otel]` (or any `opentelemetry-api`) and configure an SDK in the application.
dynamoxy then creates one `CLIENT` span per call, named `<Operation> <table>`, as a child of the
current span:

| Attribute | Example |
| --- | --- |
| `db.system.name` | `aws.dynamodb` |
| `db.operation.name`, `rpc.method` | `Query` |
| `db.collection.name`, `aws.dynamodb.table_names` | `users` |
| `aws.dynamodb.index_name`, `limit`, `consistent_read`, `scan_forward`, `projection`, `segment`, `total_segments` | request parameters |
| `aws.dynamodb.count`, `scanned_count`, `consumed_capacity` | response |
| `dynamoxy.attempts`, `dynamoxy.throttles`, `dynamoxy.model` | dynamoxy |
| `error.type`, `aws.request_id` | failures (status `ERROR`, exception event) |
| `db.query.text` | expressions with placeholders, with `Telemetry(record_expressions=True)` |

Metrics:

| Instrument | Type | Unit |
| --- | --- | --- |
| `db.client.operation.duration` | histogram | s |
| `db.client.response.returned_rows` | histogram | `{row}` |
| `dynamoxy.consumed_capacity` (`capacity.type` = read/write) | counter | `{capacity_unit}` |
| `dynamoxy.request.attempts` | counter | `{attempt}` |
| `dynamoxy.request.throttles` | counter | `{attempt}` |

`returned_rows` counts the items that reads return (`GetItem`, `Query`, `Scan`, `BatchGetItem`,
`TransactGetItems`, `ExecuteStatement`); writes record none. Capacity of PartiQL statements counts
as reads for `SELECT` and as writes otherwise, also when DynamoDB reports only a total. A
managed batch that leaves items unprocessed after every round (`UnprocessedItemsError`) is a
failed operation: its span has status `ERROR`, its metrics carry `error.type`, and it emits
`operation.failed`.

```python
client = dynamoxy.Client(telemetry=dynamoxy.Telemetry(tracer_provider=provider, meter_provider=meters))
client = dynamoxy.Client(telemetry=None)  # off
```

## Cost accounting per request

```python
with dynamoxy.collect() as usage:
    response = await handle(request)
logger.info("handled", extra={"dynamodb": usage.summary()})
# {"calls": 4, "requests": 4, "duration_ms": 12.8, "read_capacity_units": 1.5,
#  "write_capacity_units": 2.0, "errors": 0, "retries": 0, "operations": {"Query": 1, ...}}
```

Collectors follow the context into tasks started inside the block and can be nested.
`usage.records` lists every `OperationRecord` (operation, table, model, duration, attempts,
throttles, requests, capacity, items, error), failed calls with their attempts and throttles
too. A call sends one request, however many attempts it takes; a managed batch sends one per
chunk of items and round of unprocessed ones. `retries` counts the attempts beyond the first of
each request, so a 60-item `batch_put()` that is never throttled has three requests and no
retries.

## Metrics snapshot

```python
snapshot = client.metrics()
for name, operation in snapshot.operations.items():
    print(name, operation.calls, operation.errors, operation.throttles, operation.duration)
```

Lock-free counters kept by the core since the client connected (`client.reset_metrics()`
restarts them), including latency buckets (`snapshot.latency_buckets_ms`).

## Middleware

Middleware sees every call (`Request`: operation, table, params, model, context) and its
`Response` (output, stats). Use it for auditing, access control, tenant isolation, caching or
fault injection in staging:

```python
async def audit(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
    response = await call_next(request)
    if request.operation in {"PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems"}:
        audit_log.info("write", extra={"table": request.table, "attempts": response.stats.attempts})
    return response


client.use(audit)
```

Middleware runs in registration order (the first registered is the outermost). A call runs the
middleware registered when it starts: adding or removing one, also from inside a middleware (a
one-shot middleware that removes itself) or another task, changes later calls only.
