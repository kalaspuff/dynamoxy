# Errors and retries

## What to catch

Most code needs only a few errors; the [errors reference](../reference/errors.md) lists them
all.

```python
async def register(user_id: str, email: str, name: str) -> Response:
    try:
        user = await users.create(User(user_id=user_id, email=email, name=name))
    except dynamoxy.UniqueConstraintError:
        return conflict("email already registered")
    except dynamoxy.ItemAlreadyExistsError:
        return conflict("user exists")
    except dynamoxy.ValidationError as error:
        return bad_request({field_error.path: field_error.message for field_error in error.errors})
    return created(user)
```

| You see | It means | Usually |
| --- | --- | --- |
| `ValidationError` | invalid input (before I/O) or a request DynamoDB rejected | 400 |
| `ItemNotFoundError` | `get_or_raise`/`one`/`update` found nothing | 404 |
| `ItemAlreadyExistsError`, `UniqueConstraintError` | conflicts with stored data | 409 |
| `VersionConflictError` | concurrent modification | reload and retry, or 409 |
| `ConditionalCheckFailedError` | your business condition failed | depends |
| `TransactionCanceledError` | see `.cancellation_reasons` / `.errors` | depends |
| `ThrottlingError`, `TransportError`, `InternalServerError` | still failing after retries | 503, retry later |

All dynamoxy errors are classes of `dynamoxy.errors` (also exported by `dynamoxy`), the module
tracebacks name, and derive from `DynamoxyError`. They say whether retrying may help
(`.retryable`). Service errors keep DynamoDB's `.code`, `.message` and `.request_id` (quote it in
support cases). Errors pickle with their attributes, so they reach the caller of a worker process
or task queue as themselves. Unpickling does not call the error's constructor, so your own
subclasses of dynamoxy's errors may take more arguments than the message: their attributes come
back from the pickle. A value that does not pickle, or does not unpickle, is left out (`None` in
the unpickled error), at the finest level that works: a `FieldError.value` such as a function, the
`.model` of a model class defined inside a function, or one value in a `.key` or `.item` dict
(the rest of the dict stays). The check uses the standard `pickle` module, also when the error is
sent by cloudpickle (Ray, Dask, PySpark), which could send such values: an error never fails to
pickle because of an attribute, whichever pickler sends it. Copies (`copy.copy`,
`copy.deepcopy`) keep every attribute.

Messages are safe to log: they name models, fields and types, never keys or values, which may be
personal data. The errors carry those as attributes, and the messages say where: `User does not
exist (see .key)`, `User.email: the value is already used by another item (see .value)`. Log
`error.key` yourself only when your keys are not personal data. `FieldError.value` holds the value
that failed validation or decoding (`str()` and `repr()` leave it out).

## Automatic retries

The core retries retryable failures with exponential backoff and full jitter:

- throttling (`ProvisionedThroughputExceededException`, `ThrottlingException`,
  `RequestLimitExceeded`),
- server errors (HTTP 5xx, `InternalServerError`, `ServiceUnavailable`),
- `TransactionConflictException` and transactions canceled only by conflicts or throttling,
- timeouts and connection failures (unless `RetryPolicy(retry_timeouts=False)`).

Conditional failures, validation errors and access errors are never retried. Each retry emits
`request.retry` (and `request.throttled`); exhaustion emits `request.gave_up`.

```python
dynamoxy.Client(retry=dynamoxy.RetryPolicy(max_attempts=8, base_delay=0.1, max_delay=10))  # batch jobs
dynamoxy.Client(retry=dynamoxy.RetryPolicy.none(), timeout=0.5)                          # tight latency budgets
```

The delay before retry *r* (the first retry is 1) is random between 0 and
`min(max_delay, base_delay * 2^(r-1))`.

## Timeouts

`timeout` limits each attempt (default 10 s), `connect_timeout` the connection (default 3.1 s).
The total time of a call is at most about `max_attempts * timeout` plus delays. For an overall
deadline, wrap the call: `async with asyncio.timeout(2): ...`. Cancelling a call cancels its
remaining attempts; a write already sent may still be applied by DynamoDB.

## Writes with unknown outcome

An attempt that times out, loses its connection or gets a 5xx may have been applied (DynamoDB:
"the operation may have succeeded or failed"). A throttled or refused attempt applies nothing.
After an attempt that may have been applied, dynamoxy never reports a conflict that the write
may have caused itself, and never applies a write twice:

- Transactions are sent again: their idempotency token makes the second attempt return the
  first one's result.
- Writes that apply once even when sent twice are sent again: `put()`, `delete()`, and updates
  that only `SET` values, `REMOVE` attributes or add to and remove from sets.
- Updates that would apply twice are not sent again (`add()` and `increment()` of numbers,
  `append()`, `SET n = n + 1` in PartiQL): the call raises `WriteOutcomeUnknownError`.
- When the next attempt fails its condition (`create()`, version checks, `condition=`, a
  PartiQL `INSERT`), the earlier attempt may have caused it: the call raises
  `WriteOutcomeUnknownError`, whose `.item` is the item DynamoDB returned, instead of
  `ItemAlreadyExistsError` or `VersionConflictError`. Old values that the next attempt returns
  (`return_old=True`) may be the earlier attempt's own write, or the absence of the item it
  deleted, and raise it too.
- When the attempts run out without an answer that says whether the write was applied (the
  later attempts were throttled, or the last attempt itself timed out or got a 5xx), the call
  raises `WriteOutcomeUnknownError` too, not the last attempt's `ThrottlingError` or
  `InternalServerError`: those say that nothing was applied, and invite a retry that could
  apply the write twice. This holds for one attempt as well (`RetryPolicy.none()`), and for
  transactions, whose later attempts would only return the earlier one's result.

Model writes settle most of these themselves. Each write that runs alone stores a random id in
the item (`_wid`, [ADR 0016](../adr/0016-writes-store-a-write-id.md)); when the outcome is
unknown, the repository looks at the item DynamoDB returned with the failed condition, or reads
it when an update was not sent again. If the item holds this write's id, the write was applied:
`create()`, `save()`, `update()` and `increment()` succeed, run their hooks once and emit
`request.outcome_settled`. The outcome stays unknown when another writer wrote in between, for
deletes, for the dict-level API, and for models declared with `write_ids=False`.

`WriteOutcomeUnknownError` is a `TransportError` and never a `ConditionalCheckFailedError`, so
code that handles lost races does not mistake a write that happened for one. It is not
`.retryable`: read the item to find out what happened, and repeat only writes that apply once.
Each one emits `request.outcome_unknown`. Reads have no outcome to lose: they raise the last
attempt's error.

A write that must apply exactly once even when the application repeats it (a payment, a stock
count) can go alone into a transaction with a token of your own, such as the idempotency key of
the request that asked for it. Within 10 minutes, DynamoDB applies the same token once:

```python
async with client.transaction(token=request_id) as tx:
    tx.update(account, Account.balance.add(-amount))
```

The repeated transaction must be identical: automatic timestamps such as `updated_at()` change
between calls, so for such models also make it refuse a repeat, for example with `tx.create()` of
an item keyed by the request id ([idempotency tokens](transactions.md#idempotency-tokens)). It
costs twice the write units of a single write. `RetryPolicy(retry_timeouts=False)` retries no
timeouts at all: they raise `TransportError`.

## Batches

Managed batches retry unprocessed items for `batch_max_rounds` rounds. If items remain,
`UnprocessedItemsError.unprocessed` lists `(kind, item_or_key)` pairs; nothing is silently
dropped.
