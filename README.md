# dynamoxy

**A typed, async DynamoDB ORM for Python, with a Rust core.** Declare models with type hints (or
pydantic), and get validated, atomic, observable DynamoDB access: repositories, queries with
cursors, transactions with readable failures, unique constraints, optimistic locking,
single-table design and an in-memory DynamoDB for tests.

```python
import asyncio
from datetime import datetime

import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    name: Attr[str] = field(min_length=1)
    visits: Attr[int] = field(default=0)
    created_at: Attr[datetime] = dynamoxy.created_at()
    version: Attr[int] = dynamoxy.version()

    by_email = dynamoxy.GlobalIndex(partition_key=email)


async def main() -> None:
    async with dynamoxy.Client(region="eu-west-1") as client:  # or dynamoxy.testing.memory_client()
        await client.ensure_tables(User)
        users = client.repository(User)

        ada = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
        await ada.increment(User.visits)  # atomic; `ada` is refreshed in place
        ada.name = "Ada Lovelace"
        await ada.save()  # only if nobody changed it meanwhile (version)

        try:
            await users.create(User(user_id="u2", email="ada@example.com", name="Imposter"))
        except dynamoxy.UniqueConstraintError as error:
            print(f"{error.field} is taken")

        found = await users.query(index=User.by_email, email="ada@example.com").first()
        print(found)


asyncio.run(main())
```

## Highlights

- **Typed end to end.** `Attr[T]` fields are values on instances and typed expression paths on
  the class (`User.visits > 3`, `User.visits.add(1)`). pyright, mypy and pyrefly check
  constructors (a missing field, a `str` for an `int`, an automatic field passed in), expression
  operands and update actions (`User.visits > "3"`, `user.increment(User.name)`), and
  `await users.get("u1")` is a `User | None`.
- **Mistakes fail early and explain themselves.** Values are validated on construction and on
  assignment, and DynamoDB's rules (one sort key condition, 100 operations per transaction, no
  consistent reads of global indexes, ...) are checked before a request is sent, with messages that
  say what to change:

  ```text
  ValueError: User index 'by_email' is a global secondary index, which does not support consistent reads: remove consistent=True, or read the table or a local index
  ValidationError: the transaction has 120 operations (guard items of unique attributes included); DynamoDB allows 100
  ```

  A write that DynamoDB refuses raises an error that names the reason (`ItemAlreadyExistsError`,
  `VersionConflictError`, `UniqueConstraintError` with the `.field`), not a bare
  `ConditionalCheckFailedException`. A canceled transaction maps each cancellation reason back to
  the operation that caused it, and service errors keep DynamoDB's code and request id, the
  attempts made and whether a retry may help.
- **Writes that say what they mean.** `create()` never overwrites, `save()` checks the version
  (optimistic locking), `update()` applies atomic actions without reading first, and every write
  takes a typed `condition=`. Instances write themselves through the repository that loaded them
  (`await user.save()`, `await user.increment(User.visits)`). Unique values, timestamps and
  versions are maintained for you (`unique=True`, `created_at()`, `updated_at()`, `version()`).
- **Retries built in.** Throttling, server errors, transaction conflicts and timeouts are retried
  with exponential backoff and full jitter, batches resend unprocessed items, and whatever still
  fails is raised with its attempts or reported, never dropped.
- **A DynamoDB for your tests, without Docker or an AWS account.**
  `dynamoxy.testing.memory_client()` is an in-memory DynamoDB written in Rust, with DynamoDB's
  semantics, limits, error messages and capacity accounting, held to parity with an emulator by
  dynamoxy's own test suite. It refuses what DynamoDB refuses (a query filter on a key attribute,
  an item over 400 KB), simulates faults and answers in microseconds. `dynamoxy.collect()` lets a
  test pin down retries, calls and capacity units (`assert usage.calls <= 3` keeps an N+1 query
  from shipping):

  ```python
  dynamoxy.testing.inject_fault(client, operation="GetItem", error="throttling", times=2)
  with dynamoxy.collect() as usage:
      user = await users.get("u1")  # throttled twice, retried with backoff
  assert usage.retries == 2
  ```

- **Observable without setup.** Structured events go to `dynamoxy.*` loggers and event hooks,
  middleware can wrap every call, and with an OpenTelemetry SDK configured, every call is a span
  following the database semantic conventions, with metrics. An event kind nobody listens to costs
  one atomic load, and dynamoxy never logs item contents.
- **Single-table design without bookkeeping.** Key templates
  (`partition_key(template="CUSTOMER#{customer_id}")`), index keys derived from other fields and
  re-rendered on every write, entity filters added to queries, and collections that decode mixed
  entities by type.
- **Fast where it counts.** Per call, dynamoxy needs 2.5 to nearly 5 times less client CPU than
  boto3 while returning validated models, and it starts in less than half the time (see
  [Performance](#performance)).
- **Docs for you and your coding agent.** The usage guide ships in the wheel and always matches the
  installed version (`dynamoxy guide`), and application repositories can copy a ready-made agent
  skill.
- **Nothing else to install.** No required dependencies: pydantic and OpenTelemetry are optional
  extras, imported lazily. One abi3 wheel per platform covers CPython 3.12+ on Linux (x86_64 and
  aarch64, glibc and musl), macOS and Windows.

It covers the rest of DynamoDB too: queries and scans with cursors, global and local indexes,
batches with unprocessed-item handling, transactions, TTL, streams, backups and point-in-time
recovery, PartiQL and table management.

## Already using pydantic?

Inherit from `dynamoxy.PydanticModel` instead of `BaseModel` and mark the key. Pydantic keeps
validating, and the class stays a pydantic model, so the same `Customer` can be a FastAPI request or
response model and a DynamoDB table (`uv add 'dynamoxy[pydantic]'`):

```python
from typing import Annotated

from pydantic import BaseModel, Field

import dynamoxy


class Address(BaseModel):  # plain pydantic models nest as maps
    street: str
    city: str


class Customer(dynamoxy.PydanticModel, table="customers"):  # instead of BaseModel
    customer_id: Annotated[str, dynamoxy.PartitionKey()]
    email: Annotated[str, dynamoxy.Unique()]
    name: str = Field(min_length=1)
    address: Address | None = None


await client.ensure_tables(Customer)
customers = client.repository(Customer)
await customers.create(Customer(customer_id="c1", email="ada@example.com", name="Ada"))
customer = await customers.get("c1")  # a validated Customer
```

A model you would rather not touch can stay as it is: subclass it together with
`dynamoxy.PydanticModel` and re-declare only the fields that need a marker, such as the key (see
the [pydantic guide](https://github.com/kalaspuff/dynamoxy/blob/main/docs/guides/pydantic.md#existing-models)).

## Install

```bash
uv add dynamoxy               # or: pip install dynamoxy
uv add 'dynamoxy[pydantic]'   # pydantic models
uv add 'dynamoxy[otel]'       # OpenTelemetry
```

## Documentation

| | |
| --- | --- |
| [Getting started](https://github.com/kalaspuff/dynamoxy/blob/main/docs/guides/getting-started.md) | First model, connecting, first queries |
| [Usage guide](https://github.com/kalaspuff/dynamoxy/blob/main/python/dynamoxy/AGENT_GUIDE.md) | The compact reference (`dynamoxy guide`) |
| [Guides](https://github.com/kalaspuff/dynamoxy/blob/main/docs/index.md) | Modeling, reading, writing, transactions, single-table design, testing, ... |
| [Examples](https://github.com/kalaspuff/dynamoxy/blob/main/examples/README.md) | Runnable programs |
| [Reference](https://github.com/kalaspuff/dynamoxy/blob/main/docs/reference/configuration.md) | Configuration, [errors](https://github.com/kalaspuff/dynamoxy/blob/main/docs/reference/errors.md), [events](https://github.com/kalaspuff/dynamoxy/blob/main/docs/reference/events.md) |
| [Architecture](https://github.com/kalaspuff/dynamoxy/blob/main/docs/architecture.md) | How it works, invariants, [decisions](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/README.md) |
| [Changelog](https://github.com/kalaspuff/dynamoxy/blob/main/CHANGELOG.md) | Release notes |

**AI agents**: run `dynamoxy guide` (or read [AGENT_GUIDE.md](https://github.com/kalaspuff/dynamoxy/blob/main/python/dynamoxy/AGENT_GUIDE.md))
before writing code with dynamoxy; application repositories can copy the skill in
[skills/dynamoxy](https://github.com/kalaspuff/dynamoxy/blob/main/skills/dynamoxy/SKILL.md). Agents changing dynamoxy itself start with
[AGENTS.md](https://github.com/kalaspuff/dynamoxy/blob/main/AGENTS.md).

## Design

dynamoxy has three layers: a Rust core that speaks DynamoDB (`crates/dynamoxy-core`), a thin PyO3
binding, and the Python package. The notable decisions:

- **Rust for protocol, timing and I/O; Python for what you touch.** The core talks to DynamoDB
  through the AWS SDK for Rust and owns retries, timeouts, batching, events, metrics and the
  in-memory DynamoDB. Python owns models, validation, codecs, repositories, hooks, middleware and
  OpenTelemetry. The binding only converts values and bridges to asyncio, and the core never
  calls into Python: Python pulls events from it
  ([ADR 0001](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0001-rust-core-python-api.md)).
- **One backend port, two adapters, held to parity.** Every DynamoDB operation is a method of one
  `Backend` trait, implemented by the AWS adapter and by the in-memory DynamoDB. The integration
  tests run on the in-memory backend and on an emulator, so the test double stays faithful to
  DynamoDB
  ([ADR 0003](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0003-in-memory-dynamodb.md)).
- **The SDK for transport, dynamoxy for retries.** The AWS SDK signs requests, resolves
  credentials and endpoints and pools connections; dynamoxy retries with exponential backoff, full
  jitter and a timeout per attempt, so every call knows its attempts, throttles and duration, and
  every retry is an event
  ([ADR 0002](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0002-aws-sdk-transport-own-retries.md)).
- **No values in expressions.** Typed paths render to expressions with placeholders for every name
  and value, so reserved words and injection are non-issues. The core parses and validates them
  (property tests check the builder against its parser), and its evaluator refreshes instances
  after a transaction without reading them back
  ([ADR 0004](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0004-expressions-built-in-python.md)).
- **Repositories, and instances that remember theirs.** Models hold no client, so several clients
  (accounts, regions, test stores) can share them. Repositories do the persistence, and an
  instance a repository read or wrote saves itself through that repository, without global state
  ([ADR 0007](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0007-repositories-over-active-record.md),
  [ADR 0011](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0011-instances-remember-their-repository.md)).
- **Uniqueness with guard items.** Each unique value is a guard item in the same table, written
  in the same transaction as the item that holds the value; an index could not enforce it, since
  global indexes are eventually consistent
  ([ADR 0005](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0005-unique-constraints-guard-items.md)).
- **Stored formats are API.** Native DynamoDB types, exact decimals, fixed-width UTC timestamps
  that sort as text, canonical numbers in keys and opaque cursors. Writes of loaded instances keep
  the attributes their model does not declare, so rolling deploys lose nothing. Changing a format
  takes an ADR and a migration path
  ([ADR 0008](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0008-stored-formats.md),
  [ADR 0013](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0013-stored-forms-read-back-as-written.md),
  [ADR 0014](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0014-writes-keep-undeclared-attributes.md)).
- **Invariants, each covered by tests**: failing fast before I/O, no I/O on import, everything
  bounded and nothing dropped silently, fork safety, a clean interpreter exit, and no payload
  logging ([architecture](https://github.com/kalaspuff/dynamoxy/blob/main/docs/architecture.md#invariants)).

## Performance

Network round trips dominate DynamoDB latency, and they cost every client the same. What a client
library adds is CPU time on every call, which a pure-Python client spends holding the GIL: it
limits how many requests a process can serve and shows up on Lambda bills. dynamoxy does the
protocol work in Rust and keeps the Python side of a call short.

Client CPU time per call against a local DynamoDB emulator. dynamoxy returns model instances; the
botocore clients that boto3, aioboto3 and PynamoDB build on return DynamoDB JSON:

| | dynamoxy | boto3 | aiobotocore |
| --- | ---: | ---: | ---: |
| `GetItem`, one item | 0.14 ms | 0.66 ms | 0.60 ms |
| `PutItem`, one item | 0.19 ms | 0.67 ms | 0.61 ms |
| `Query`, 100 items | 0.65 ms | 1.68 ms | 1.71 ms |

In a fresh process, as in a Lambda cold start, dynamoxy imports and reads its first item in
51 ms, where boto3 takes 115 ms and aiobotocore 159 ms.

On the in-memory backend, which measures dynamoxy alone, a `get()` that returns a model takes
about 13 µs, a `create()` about 27 µs and a query that decodes 100 models about 0.25 ms, so test
suites with thousands of DynamoDB calls run in seconds.

Where it comes from:

- **The protocol runs in Rust, outside the GIL.** Request signing, HTTP with pooled connections,
  JSON, retries and timeouts run on a Tokio runtime. Results become Python values in one pass that
  shares attribute names and decimals between the items of a page.
- **An asyncio bridge without detours.** Rust threads never take the GIL: finished calls queue up
  on their event loop, which is woken once per batch through a socket it watches and builds the
  results on its own thread. In-memory calls run inline
  ([ADR 0010](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/0010-asyncio-bridge.md)).
- **Plans made once.** Each model plans its validation, encoding, decoding and key rendering when
  the class is created, not on every call.
- **Only what you use.** An event kind nobody listens to costs one atomic load; there are no spans
  without an OpenTelemetry SDK and no request objects without middleware.
- **Requests that ask for less.** `first()` and `limit()` request only what they still need,
  `count()` counts on the server, `exists()` reads keys only, batches are chunked and sent
  concurrently, and scans can run in parallel segments.

How this was measured: dynamoxy 0.2.0, boto3 1.43 and aiobotocore 3.9 on Python 3.14, aarch64
Linux in a VM on an Apple Silicon Mac, against [MiniStack](https://pypi.org/project/ministack/)
over local HTTP, in October 2026. CPU figures are the CPU time of the client process (all
threads) for sequential calls, medians of repeated runs of 3000 calls (400 for queries); cold
starts are medians of 31 fresh processes, interpreter startup excluded; in-memory figures are
wall-clock times. Numbers vary with hardware.

## Contributing

See [CONTRIBUTING.md](https://github.com/kalaspuff/dynamoxy/blob/main/CONTRIBUTING.md). The project is young (0.x): breaking changes are possible
in minor versions and are called out in the changelog.

## License

[MIT](https://github.com/kalaspuff/dynamoxy/blob/main/LICENSE)
