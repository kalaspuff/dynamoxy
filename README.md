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

## Why dynamoxy

- **Typed models**: `Attr[T]` fields are values on instances and typed expression paths on the
  class (`User.visits > 3`); type checkers understand constructors and expressions. Pydantic
  models work too.
- **Correct by default**: validation before I/O, `create` that never overwrites, optimistic
  locking with `version()`, unique attributes enforced transactionally, automatic timestamps,
  conditions on every write, no values in expression strings.
- **Every DynamoDB feature**: queries and scans with cursors, global and local indexes,
  batches with unprocessed-item handling, transactions with per-operation cancellation reasons,
  TTL, streams, backups and point-in-time recovery, PartiQL, table management.
- **Single-table design** with key templates, derived index keys and mixed-entity collections.
- **Observable**: structured events and logging, hooks, middleware, OpenTelemetry spans and
  metrics, per-request cost accounting (`dynamoxy.collect()`).
- **Testable**: `dynamoxy.testing.memory_client()` is an in-memory DynamoDB with DynamoDB's
  semantics and fault injection; no Docker, no AWS account.
- **Fast and robust**: a Rust core (AWS SDK for Rust, Tokio) handles transport, retries with
  backoff, timeouts and batching; wheels for Linux (x86_64, aarch64, glibc and musl), macOS and
  Windows, CPython 3.12+.

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

## How it works

The Rust core (`crates/dynamoxy-core`) speaks DynamoDB through the AWS SDK for Rust, with its own
retries, timeouts, batching, events and metrics, and contains a complete in-memory DynamoDB. A
thin PyO3 binding exposes it to asyncio, and the Python package adds models, validation,
repositories, transactions, constraints, hooks and OpenTelemetry.

## Contributing

See [CONTRIBUTING.md](https://github.com/kalaspuff/dynamoxy/blob/main/CONTRIBUTING.md). The project is young (0.x): breaking changes are possible
in minor versions and are called out in the changelog.

## License

[MIT](https://github.com/kalaspuff/dynamoxy/blob/main/LICENSE)
