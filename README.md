<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/kalaspuff/dynamoxy/main/docs/assets/logo/dynamoxy-dark.svg">
    <img src="https://raw.githubusercontent.com/kalaspuff/dynamoxy/main/docs/assets/logo/dynamoxy.svg" alt="" width="44" height="44" align="absmiddle">
  </picture>
  dynamoxy
</h1>

**A typed, async DynamoDB ORM for Python, with a Rust core.** Declare models with type hints (or
pydantic) and get validated, atomic DynamoDB access, plus an in-memory DynamoDB for your tests.

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
    version: Attr[str] = dynamoxy.version()

    by_email = dynamoxy.GlobalIndex(partition_key=email)


async def main() -> None:
    async with dynamoxy.Client(region="eu-west-1") as client:  # or dynamoxy.testing.memory_client()
        await client.ensure_tables(User)

        ada = await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
        await ada.increment(User.visits)  # atomic; `ada` is refreshed in place
        ada.name = "Ada Lovelace"
        await ada.save()  # only if nobody changed it meanwhile (version)

        try:
            await User.create(client, user_id="u2", email="ada@example.com", name="Imposter")
        except dynamoxy.UniqueConstraintError as error:
            print(f"{error.field} is taken")

        found = await User.query(client, index=User.by_email, email="ada@example.com").first()
        print(found)


asyncio.run(main())
```

## Highlights

- **Typed end to end.** Fields are values on instances and typed paths on the class
  (`User.visits > 3`). Your type checker catches `User(visits="3")` and `User.visits > "3"`.
- **Errors that tell you what to fix.** DynamoDB's rules are checked before a request is sent:

  ```text
  ValidationError: the transaction has 120 operations (guard items of unique attributes included); DynamoDB allows 100
  ```

  A refused write raises `ItemAlreadyExistsError`, `VersionConflictError` or
  `UniqueConstraintError`, not a bare `ConditionalCheckFailedException`.
- **Writes that mean what they say.** `create()` never overwrites, `save()` checks the version,
  `update()` is atomic without a read.
- **Unique values, timestamps and versions** are maintained for you.
- **Retries built in**, with backoff and jitter. Nothing fails silently.
- **An in-memory DynamoDB for tests.** No Docker, no AWS account. It refuses what DynamoDB
  refuses, simulates faults and answers in microseconds:

  ```python
  dynamoxy.testing.inject_fault(client, operation="GetItem", error="throttling", times=2)
  with dynamoxy.collect() as usage:
      user = await User.get(client, "u1")  # throttled twice, retried
  assert usage.retries == 2
  ```

- **Observable.** Structured logs and event hooks out of the box; OpenTelemetry spans and metrics
  when you configure it. Item contents are never logged.
- **Single-table design** with key templates such as `"CUSTOMER#{customer_id}"`.
- **Fast.** 2.5 to 5 times less client CPU per call than boto3 ([numbers](#performance)).
- **No required dependencies.** pydantic and OpenTelemetry are optional extras.

Also covered: indexes, cursors, batches, transactions, TTL, streams, backups, PartiQL and table
management.

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
customers = client.model(Customer)
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
| [Parity with DynamoDB](https://github.com/kalaspuff/dynamoxy/blob/main/docs/parity/README.md) | DynamoDB's answers next to the in-memory backend's, MiniStack's and LocalStack's |
| [Changelog](https://github.com/kalaspuff/dynamoxy/blob/main/CHANGELOG.md) | Release notes |

**AI agents**: run `dynamoxy guide` (or read [AGENT_GUIDE.md](https://github.com/kalaspuff/dynamoxy/blob/main/python/dynamoxy/AGENT_GUIDE.md))
before writing code with dynamoxy; application repositories can copy the skill in
[skills/dynamoxy](https://github.com/kalaspuff/dynamoxy/blob/main/skills/dynamoxy/SKILL.md). Agents changing dynamoxy itself start with
[AGENTS.md](https://github.com/kalaspuff/dynamoxy/blob/main/AGENTS.md).

## Tested against DynamoDB

The in-memory backend is checked against DynamoDB itself, not only against emulators. A probe
runs dynamoxy's operations on real DynamoDB and on each backend, and compares what comes back:
results, error codes and messages, size limits, transaction payload sizes and table states.

| | Answers like DynamoDB | Used for |
| --- | --- | --- |
| In-memory backend | Every probed fact | Your tests, and dynamoxy's |
| [MiniStack](https://pypi.org/project/ministack/) | Most results; accepts much of what DynamoDB refuses (filters on attributes an index does not hold, size limits, changes to a table being created) | dynamoxy's end-to-end tests and CI |
| [LocalStack](https://www.localstack.cloud/) | Closer than MiniStack (DynamoDB Local inside); its free edition lacks backups and point-in-time recovery | dynamoxy's end-to-end tests, locally |

So a test that passes on the in-memory backend fails where DynamoDB would, and an emulator that
accepts a request is not proof that DynamoDB will. Every answer, with the probe that asked it, is
in [docs/parity](https://github.com/kalaspuff/dynamoxy/blob/main/docs/parity/README.md).

## How it works

A Rust core speaks DynamoDB: the AWS SDK for transport, plus dynamoxy's own retries, batching,
events and the in-memory backend. Python owns what you touch: models, validation, repositories
and hooks. A few choices worth knowing:

- **Values never go into expression strings**, so reserved words and injection are non-issues.
- **Unique values are guard items** written in the same transaction, since indexes are only
  eventually consistent.
- **Models hold no client**, so one model works with several accounts, regions or test stores.
- **Stored formats are API**: they stay readable across versions.

The [architecture](https://github.com/kalaspuff/dynamoxy/blob/main/docs/architecture.md) and the
[decision records](https://github.com/kalaspuff/dynamoxy/blob/main/docs/adr/README.md) have the
details.

## Performance

Round trips cost every client the same. What a library adds is CPU time per call, spent holding
the GIL. dynamoxy does the protocol work in Rust.

Client CPU time per call against a local emulator (dynamoxy returns validated models; the others
return DynamoDB JSON):

| | dynamoxy | boto3 | aiobotocore |
| --- | ---: | ---: | ---: |
| `GetItem`, one item | 0.14 ms | 0.66 ms | 0.60 ms |
| `PutItem`, one item | 0.19 ms | 0.67 ms | 0.61 ms |
| `Query`, 100 items | 0.65 ms | 1.68 ms | 1.71 ms |

- **Cold start** (import and first read): 51 ms, against 115 ms for boto3.
- **In memory**, a `get()` takes about 13 µs, so large test suites run in seconds.

Why it is fast: signing, HTTP, JSON and retries run in Rust outside the GIL; each model plans its
encoding once, when the class is created; and features you do not use cost nothing.

Measured with dynamoxy 0.2.0 on Python 3.14, aarch64 Linux, October 2026. Your hardware will
differ:
[`scripts/benchmark.py`](https://github.com/kalaspuff/dynamoxy/blob/main/scripts/benchmark.py)
measures it on yours.

## Contributing

See [CONTRIBUTING.md](https://github.com/kalaspuff/dynamoxy/blob/main/CONTRIBUTING.md). The project is young (0.x): breaking changes are possible
in minor versions and are called out in the changelog.

## License

[MIT](https://github.com/kalaspuff/dynamoxy/blob/main/LICENSE)
