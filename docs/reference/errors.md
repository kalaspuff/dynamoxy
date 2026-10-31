# Errors reference

Every exception dynamoxy raises derives from `dynamoxy.DynamoxyError` and is exported from
`dynamoxy` (and `dynamoxy.errors`). `scripts/check_consistency.py` verifies that every name in
`dynamoxy.errors.__all__` is documented here.

Every error has `.retryable`: whether the same call may succeed later. dynamoxy already retried
retryable errors according to the client's `RetryPolicy` before raising them. Errors returned by
DynamoDB also carry `.code` (`"ConditionalCheckFailedException"`), `.message`, `.operation`
(`"PutItem"`), `.request_id` and `.status_code`; errors of a call carry `.attempts`,
`.throttles` (attempts DynamoDB throttled), `.requests` (requests sent: 1 for a single call,
several for a managed batch) and `.duration` (seconds, retries included).

Messages name models, fields, attributes and types, but never keys, unique values or stored
values: those may be personal data, and messages end up in logs. The errors carry them as
attributes (`.key`, `.item`, `.value`, `FieldError.value`), and the messages say where to look
(`User does not exist (see .key)`). DynamoDB's own messages (`.message` of service errors) are
passed on as DynamoDB words them; a few quote values of the request, such as a `BETWEEN` whose
bounds are in the wrong order.

```text
DynamoxyError
├── ConfigurationError
├── ValidationError                         .errors: tuple[FieldError, ...]
│   └── ItemDecodeError
├── ItemNotFoundError
├── ServiceError
│   ├── ConditionalCheckFailedError         .item
│   │   ├── ItemAlreadyExistsError
│   │   ├── VersionConflictError
│   │   └── UniqueConstraintError
│   ├── TransactionCanceledError            .cancellation_reasons, .errors
│   ├── TransactionConflictError, TransactionInProgressError, IdempotentParameterMismatchError
│   ├── ThrottlingError, ResourceNotFoundError, ResourceInUseError, LimitExceededError
│   ├── ItemCollectionSizeLimitExceededError, DuplicateItemError, BackupError
│   └── AccessDeniedError, InternalServerError
├── UnprocessedItemsError                   .unprocessed, .error
├── TransportError
│   └── WriteOutcomeUnknownError            .item
├── CredentialsError
└── ShutdownError
```

## Base classes

| Error | Retryable | Raised when |
| --- | --- | --- |
| `DynamoxyError` | — | Base class of every dynamoxy error |
| `ServiceError` | depends | DynamoDB returned an error; subclasses name the common ones, others keep the `.code` |

## Validation and data

| Error | Retryable | Raised when |
| --- | --- | --- |
| `ConfigurationError` | no | The client configuration is invalid (the native layer refused it) |
| `ValidationError` | no | A value, key, expression or request is invalid: model validation (before any I/O; `.errors` lists every `FieldError`), local checks (limits, sizes, names) or DynamoDB's `ValidationException` |
| `FieldError` | — | Not an exception: one problem of a `ValidationError` (`.path`, `.message`, `.value`; `str()` and `repr()` leave the value out) |
| `ItemDecodeError` | no | A stored item does not match its model (missing required attributes, wrong types); `.model`, `.key`, and `.errors` with each stored value in `FieldError.value` |
| `ItemNotFoundError` | no | `get_or_raise()`, `Query.one()`, or `update()` of an item that does not exist (without `upsert`); `.model`, `.key` |

## Conditions and concurrency

| Error | Retryable | Raised when |
| --- | --- | --- |
| `ConditionalCheckFailedError` | no | A `condition=` did not hold; `.item` is the stored item when DynamoDB returned it, `.key` the key of the item a model write targeted |
| `ItemAlreadyExistsError` | no | `create()` found an item with the same key; `.model`, `.key`, `.item` |
| `VersionConflictError` | no | Optimistic locking failed: the stored version is not the one the instance was loaded with, or the item was deleted; `.model`, `.key`, `.expected_version`, `.item`. Reload, re-apply, save again |
| `UniqueConstraintError` | no | A value of a `unique=True` attribute is used by another item; `.model`, `.field`, `.value` |
| `TransactionCanceledError` | only when every failed operation hit a conflict or throttling (retried first) | DynamoDB canceled a transaction. `.cancellation_reasons` (or `dynamoxy.cancellation_reasons(error)`) lists a `CancellationReason` per operation; `.errors` holds the specific errors of model operations |
| `CancellationReason` | — | Not an exception: `.index`, `.code` (`"None"` when that operation did not fail, `"ConditionalCheckFailed"`, `"TransactionConflict"`, `"DuplicateItem"`, ...), `.message`, `.item`, `.error`, `.operation`, `.failed` |
| `TransactionConflictError` | yes | Another transaction is updating one of the items |
| `TransactionInProgressError` | yes | A transaction with the same idempotency token is still running |
| `IdempotentParameterMismatchError` | no | An idempotency token was reused for a different transaction |
| `DuplicateItemError` | no | A PartiQL `INSERT` targets an existing item |

## Capacity and resources

| Error | Retryable | Raised when |
| --- | --- | --- |
| `ThrottlingError` | yes | DynamoDB throttled the request (`ProvisionedThroughputExceededException`, `ThrottlingException`, `RequestLimitExceeded`) on every attempt |
| `UnprocessedItemsError` | yes, unless `.error` is not | A managed batch still had unprocessed items after `batch_max_rounds` rounds, or a request of it failed (`.error`: a retryable error in every round, or one that is not retried, which stops the batch); `.unprocessed` lists the `(kind, item_or_key)` pairs that were not done |
| `ResourceNotFoundError` | no | The table, index or backup does not exist |
| `ResourceInUseError` | no | The table exists already or is being created, updated or deleted |
| `LimitExceededError` | no | Too many tables or concurrent control-plane operations; wait for other table changes to finish |
| `ItemCollectionSizeLimitExceededError` | no | An item collection of a table with local indexes exceeds 10 GB |
| `BackupError` | no | A backup or point-in-time recovery operation failed (backup missing, PITR disabled, restore target exists) |

## Access, transport and lifecycle

| Error | Retryable | Raised when |
| --- | --- | --- |
| `AccessDeniedError` | no | The credentials are not allowed to perform the request (IAM) |
| `CredentialsError` | no | AWS credentials are missing or could not be loaded |
| `InternalServerError` | yes | DynamoDB failed internally (HTTP 5xx) on every attempt |
| `TransportError` | yes | A network failure or a timeout (`timeout`, `connect_timeout`) on every attempt |
| `WriteOutcomeUnknownError` | no | A write may have been applied, and no answer said whether it was: an attempt timed out, lost its connection or got a 5xx, and the write was not sent again (it would apply twice: `ADD` of a number, `list_append`), or the next attempt failed its condition, or returned old values, which the earlier attempt may have caused, or the attempts ran out without an answer (throttled, or the last one timed out or got a 5xx). `.item` is the item DynamoDB returned to that next attempt. A `TransportError`, not a `ConditionalCheckFailedError` |
| `ShutdownError` | no | The client was closed, or the interpreter is exiting; create a new client |
