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

All dynamoxy errors derive from `DynamoxyError` and say whether retrying may help
(`.retryable`). Service errors keep DynamoDB's `.code`, `.message` and `.request_id` (quote it in
support cases).

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

A timeout or connection loss during a write leaves its outcome unknown. dynamoxy retries it (by
default), which is safe for:

- conditional writes (`create`, `save`, unique guards, version checks): a duplicate fails its
  condition, and dynamoxy reports what it found. If the first attempt was applied, the retry
  fails on the item it wrote itself: `create()` raises `ItemAlreadyExistsError` and `save()`
  `VersionConflictError` for a write that happened. When `error.attempts > 1`, compare
  `error.item` with what you wrote (its `updated_at`, or a request id stored with the item);
- transactions: retries reuse the idempotency token;
- `put` of a complete item and `SET` updates: applying them twice has the same result.

Arithmetic updates (`add`, `increment`) are not idempotent: guard them with a condition on a
request id, or use `retry_timeouts=False` and decide yourself.

## Batches

Managed batches retry unprocessed items for `batch_max_rounds` rounds. If items remain,
`UnprocessedItemsError.unprocessed` lists `(kind, item_or_key)` pairs; nothing is silently
dropped.
