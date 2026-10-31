# Configuration reference

Every option of the public configuration surface. `scripts/check_consistency.py` verifies that
each parameter listed here exists in the code and that every parameter in the code is listed.
Durations accept seconds (`float`) or `timedelta`; durations longer than about 31 years
(1e9 seconds) mean no limit and are capped there. Counts are `int`s (not `float`s or `bool`s) of
at most 4,294,967,295; other types raise `TypeError` when the object is created.

## `Client(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `region` | `None` | AWS region; default from the environment or profile (`AWS_REGION`, `~/.aws/config`) |
| `endpoint_url` | `None` | A DynamoDB-compatible endpoint (DynamoDB Local, MiniStack, VPC endpoint); must start with `http://` or `https://` |
| `profile` | `None` | Named AWS profile |
| `credentials` | `None` | Static `Credentials`; default: the AWS credential chain (environment, profiles, SSO, web identity, container and instance roles) |
| `timeout` | `10.0` | Time limit of one attempt of a request |
| `connect_timeout` | `3.1` | Time limit to establish a connection |
| `retry` | `RetryPolicy()` | How throttling, server errors, transaction conflicts and timeouts are retried |
| `table_prefix` | `""` | Prefix added to every logical table name (`"prod-"`) |
| `table_names` | `None` | Explicit physical names for logical table names (overrides the prefix) |
| `consumed_capacity` | `"TOTAL"` | Capacity reporting requested from DynamoDB: `"TOTAL"`, `"INDEXES"` or `"NONE"` |
| `batch_concurrency` | `4` | Requests of one managed batch sent in parallel |
| `batch_max_rounds` | `8` | Rounds for unprocessed batch items before they are reported (`UnprocessedItemsError`) |
| `slow_operation_threshold` | `None` | Calls slower than this emit `request.slow` |
| `telemetry` | `Telemetry()` | OpenTelemetry settings; `None` disables spans and OpenTelemetry metrics |
| `logging` | `None` | `LoggingConfig`; default: every event to the `dynamoxy.*` loggers at its default level |
| `event_buffer` | `10000` | Core events buffered for hooks before new ones are dropped (`events.dropped`) |
| `store` | `None` | A `MemoryStore`: use the in-memory DynamoDB instead of AWS (see `dynamoxy.testing`) |

## `RetryPolicy(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `max_attempts` | `4` | Attempts per request, the first included; `1` disables retries (`RetryPolicy.none()`) |
| `base_delay` | `0.05` | Seconds before the first retry; doubles with every attempt, with full jitter (a `timedelta` is stored as float seconds) |
| `max_delay` | `5.0` | Longest delay between attempts, in seconds |
| `retry_timeouts` | `True` | Retry requests that timed out or lost their connection. Such a write may have been applied: writes that would apply twice are not sent again, and an answer the earlier attempt may have caused raises `WriteOutcomeUnknownError` (see [writes with unknown outcome](../guides/errors-and-retries.md#writes-with-unknown-outcome)) |

## `Credentials(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `access_key_id` | required | Access key id |
| `secret_access_key` | required | Secret access key (hidden in `repr`) |
| `session_token` | `None` | Session token of temporary credentials (hidden in `repr`) |

## `Telemetry(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `tracing` | `True` | Create a span per DynamoDB call (needs `opentelemetry-api`) |
| `metrics` | `True` | Record OpenTelemetry metrics per call |
| `tracer_provider` | `None` | Tracer provider; default: the global one |
| `meter_provider` | `None` | Meter provider; default: the global one |
| `record_expressions` | `False` | Add expressions (placeholders only, never values) to spans as `db.query.text` |

`Telemetry.disabled()` turns both off.

## `LoggingConfig(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `enabled` | `True` | Log events at all |
| `levels` | `{}` | Level overrides by kind or glob (`{"request.*": "debug"}`): `"debug"`, `"info"`, `"warning"`, `"error"` |
| `disabled` | `()` | Kinds or globs that are not logged |
| `include_fields` | `True` | Append the structured fields to log messages (`key=value`) |

Hooks registered with `client.on()` receive events regardless of logging settings.

## `class Model(..., table=...)`

Class keywords of `dynamoxy.Model` and `dynamoxy.PydanticModel` subclasses. `dynamoxy.Document`
subclasses take `datetime_format` too.

| Parameter | Default | Description |
| --- | --- | --- |
| `table` | inherited | Logical table name; models without one are abstract (fields only, for inheritance) |
| `entity` | `None` | Entity name stored in the type attribute, for several models in one table |
| `type_attribute` | `"_type"` with `entity`, else `None` | Attribute that stores the entity name |
| `abstract` | `False` | A base model that is not stored itself |
| `datetime_format` | inherited, else `"iso"` | How `datetime` fields and automatic timestamps are stored unless a field chooses a codec: `"iso"` (`2026-10-13T08:48:47.966937+00:00`), `"iso_z"` (`2026-10-13T08:48:47.966937Z`), `"epoch"` or `"epoch_ms"` (numbers). Both ISO styles are always read |
| `write_ids` | inherited, else `True` | Each write that runs alone (not in a transaction or a batch) stores a random id in `_wid`, so that a write whose attempt may have been applied recognises it instead of raising `WriteOutcomeUnknownError` ([ADR 0016](../adr/0016-writes-store-a-write-id.md)) |

## `field(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `default` | none (required) | Value used when none is given |
| `default_factory` | `None` | Creates the value when none is given (`list`, `set`, `uuid4`, ...) |
| `template` | `None` | Derive the value from other fields (`"STATUS#{status}"`); not a constructor argument |
| `init` | `True` | For type checkers only: templated fields pass `init=False` implicitly |
| `attribute_name` | the field name | DynamoDB attribute name |
| `codec` | from the annotation | How the value is stored (`dynamoxy.codecs`) |
| `unique` | `False` | No two items may have the same value (guard items, transactions) |
| `min_length` | `None` | Minimum length of strings, lists, sets and maps |
| `max_length` | `None` | Maximum length |
| `pattern` | `None` | Regular expression strings must match entirely |
| `ge` | `None` | Values must be greater than or equal to this |
| `gt` | `None` | Values must be greater than this |
| `le` | `None` | Values must be less than or equal to this |
| `lt` | `None` | Values must be less than this |
| `validators` | `()` | Callables `value -> value` raising `ValueError` |
| `immutable` | `False` | The value cannot change once stored (updates of it are rejected) |
| `description` | `None` | Documentation of the field |
| `repr` | `True` | Show the field in `repr()` |

## `partition_key(...)`

The partition key; `sort_key(...)` takes the same parameters. Key attributes are stored as
strings, numbers or bytes.

| Parameter | Default | Description |
| --- | --- | --- |
| `default` | none (required) | Value used when none is given |
| `default_factory` | `None` | Creates the value (`lambda: uuid4().hex`) |
| `template` | `None` | Build the key from other fields (`"CUSTOMER#{customer_id}"`) |
| `init` | `True` | For type checkers only |
| `attribute_name` | the field name | DynamoDB attribute name |
| `codec` | from the annotation | How the value is stored |
| `min_length` | `None` | Minimum length |
| `max_length` | `None` | Maximum length |
| `pattern` | `None` | Regular expression the value must match entirely |
| `validators` | `()` | Callables `value -> value` raising `ValueError` |
| `description` | `None` | Documentation of the field |

## `sort_key(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `default` | none (required) | Value used when none is given |
| `default_factory` | `None` | Creates the value |
| `template` | `None` | Build the key from other fields (`"ORDER#{order_id}"`) |
| `init` | `True` | For type checkers only |
| `attribute_name` | the field name | DynamoDB attribute name |
| `codec` | from the annotation | How the value is stored |
| `min_length` | `None` | Minimum length |
| `max_length` | `None` | Maximum length |
| `pattern` | `None` | Regular expression the value must match entirely |
| `validators` | `()` | Callables `value -> value` raising `ValueError` |
| `description` | `None` | Documentation of the field |

## `created_at(...)`

A `datetime` set on the first write; `updated_at(...)` takes the same parameters and is set on
every write.

| Parameter | Default | Description |
| --- | --- | --- |
| `init` | `False` | For type checkers only: not a constructor argument |
| `attribute_name` | the field name | DynamoDB attribute name |
| `format` | the model's `datetime_format` | `"iso"` or `"iso_z"` (sortable text ending in `+00:00` or `Z`), `"epoch"` or `"epoch_ms"` (numbers) |
| `description` | `None` | Documentation of the field |

## `updated_at(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `init` | `False` | For type checkers only: not a constructor argument |
| `attribute_name` | the field name | DynamoDB attribute name |
| `format` | the model's `datetime_format` | `"iso"`, `"iso_z"`, `"epoch"` or `"epoch_ms"` |
| `description` | `None` | Documentation of the field |

## `version(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `init` | `False` | For type checkers only: not a constructor argument |
| `attribute_name` | the field name | DynamoDB attribute name |
| `description` | `None` | Documentation of the field |

## `ttl(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `default` | none (required) | Usually `None`: `expires_at: Attr[datetime \| None] = ttl(default=None)` |
| `attribute_name` | the field name | DynamoDB attribute name (stored as epoch seconds) |
| `description` | `None` | Documentation of the field |

## `GlobalIndex(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `name` | the class attribute name | Index name |
| `partition_key` | required | Field (or field name) of the index partition key |
| `sort_key` | `None` | Field (or field name) of the index sort key |
| `projection` | `Projection.all()` | `Projection.all()`, `Projection.keys_only()`, `Projection.include(...)`, `"ALL"`, `"KEYS_ONLY"` |
| `throughput` | `None` | `Throughput` for provisioned tables |

## `LocalIndex(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `name` | the class attribute name | Index name |
| `sort_key` | required | Field (or field name) of the alternative sort key |
| `projection` | `Projection.all()` | Copied attributes |

## `Throughput(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `read` | required | Read capacity units |
| `write` | required | Write capacity units |

## `Billing(...)`

Use `Billing.on_demand()` (the default) or `Billing.provisioned(read, write)`.

| Parameter | Default | Description |
| --- | --- | --- |
| `mode` | `"PAY_PER_REQUEST"` | `"PAY_PER_REQUEST"` or `"PROVISIONED"` |
| `throughput` | `None` | `Throughput` of provisioned tables |

## `IndexDefinition(...)`

An index of `Table.create()` (dict-level tables).

| Parameter | Default | Description |
| --- | --- | --- |
| `name` | required | Index name |
| `kind` | required | `"global"` or `"local"` |
| `partition_key` | required | `(attribute, type)`, type `"S"`, `"N"` or `"B"` |
| `sort_key` | `None` | `(attribute, type)` |
| `projection` | `Projection.all()` | Copied attributes |
| `throughput` | `None` | `Throughput` of provisioned tables |

## `client.create_tables(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `models` | required | Model classes (positional); models sharing a table are merged |
| `billing` | on demand | `Billing` |
| `wait` | `True` | Wait until each table is active |
| `exist_ok` | `False` | Skip tables that exist instead of raising `ResourceInUseError` |
| `stream` | `None` | Stream view type: `"NEW_AND_OLD_IMAGES"`, `"NEW_IMAGE"`, `"OLD_IMAGE"`, `"KEYS_ONLY"` |
| `deletion_protection` | `False` | Turn on deletion protection |
| `tags` | `None` | Tags for every table |

`client.ensure_tables(*models, billing=None, wait=True)` creates missing tables, global indexes
and TTL settings and never deletes anything.

## `Table.create(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `partition_key` | required | `(attribute, type)` |
| `sort_key` | `None` | `(attribute, type)` |
| `indexes` | `()` | `IndexDefinition`s |
| `billing` | on demand | `Billing` |
| `stream` | `None` | Stream view type |
| `ttl_attribute` | `None` | Enable Time to Live on this attribute |
| `table_class` | `None` | `"STANDARD"` or `"STANDARD_INFREQUENT_ACCESS"` |
| `deletion_protection` | `False` | Turn on deletion protection |
| `tags` | `None` | Tags |
| `encryption_key` | `None` | Customer managed KMS key (id, ARN or alias) |
| `wait` | `True` | Wait until the table is active |

## `testing.memory_client(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `store` | a new `MemoryStore` | Share a store between clients to share data |
| `options` | — | Any other `Client` option (keyword arguments); `retry` defaults to short delays |

## `testing.inject_fault(...)`

| Parameter | Default | Description |
| --- | --- | --- |
| `target` | required | An in-memory client or `MemoryStore` |
| `operation` | `None` | DynamoDB operation (`"PutItem"`); every operation when omitted |
| `table` | `None` | Physical table name; every table when omitted |
| `error` | `None` | `"throttling"`, `"request_limit"`, `"internal"`, `"unavailable"`, `"conflict"`, `"access_denied"` or any DynamoDB error code |
| `message` | `None` | Error message |
| `unprocessed` | `False` | Batch operations return every request unprocessed |
| `latency` | `None` | Seconds to wait before the call runs |
| `lose_response` | `False` | The call runs, then its response is lost (a `TransportError`, which the client retries), as when a connection drops after a write was applied |
| `times` | `1` | Number of calls affected |
