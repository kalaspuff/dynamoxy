# Getting started

## Install

```bash
uv add dynamoxy                 # or: pip install dynamoxy
uv add 'dynamoxy[pydantic]'     # pydantic models as tables
uv add 'dynamoxy[otel]'         # OpenTelemetry spans and metrics
```

dynamoxy supports CPython 3.12 and newer on Linux (x86_64 and aarch64, glibc and musl), macOS
(Apple silicon and Intel) and Windows. Wheels contain the compiled Rust core; nothing else is
installed.

## A first model

```python
from datetime import datetime

import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    name: Attr[str]
    visits: Attr[int] = field(default=0)
    created_at: Attr[datetime] = dynamoxy.created_at()

    by_email = dynamoxy.GlobalIndex(partition_key=email)
```

A model is a class with a table name. Each field is annotated `Attr[T]`; the annotation decides
how values are stored and validated, and type checkers understand the constructor (required
fields, defaults, automatic fields). On the class, attributes are typed expression paths
(`User.visits > 3`, `User.visits.add(1)`); on instances they are values.

## Connect

```python
async with dynamoxy.Client(region="eu-west-1") as client:
    ...
```

Credentials and region come from the standard AWS configuration (environment variables such as
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_REGION`, `~/.aws/config` profiles, SSO,
container and instance roles); the [deployment guide](deployment.md#credentials) lists the order
in which they are tried. Other targets:

```python
dynamoxy.Client(profile="dev")                                    # a named profile
dynamoxy.Client(endpoint_url="http://localhost:4566", region="us-east-1",
                credentials=dynamoxy.Credentials("test", "test"))  # MiniStack, DynamoDB Local
dynamoxy.testing.memory_client()                                  # in-memory DynamoDB, no network
```

The client connects on first use. Create one per process and share it.

## Create tables

```python
await client.ensure_tables(User)
```

`ensure_tables()` creates missing tables, global secondary indexes and Time to Live settings
from the models, and never deletes anything. It is ideal for development and tests; in
production, manage tables with your infrastructure tooling (the [tables guide](tables.md) shows
how to get the definitions).

## Read and write

```python
users = client.model(User)

ada = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
await ada.increment(User.visits)                     # atomic, refreshes `ada` in place
ada.name = "Ada Lovelace"
await ada.save()                                     # writes the changed instance
same = await users.get("u1")
by_email = await users.query(index=User.by_email, email="ada@example.com").first()
await ada.delete()
```

`client.model(User)` gives the typed operations of a model on this client, a
`dynamoxy.Repository[User]`. An instance remembers the repository that read or wrote it, so
`ada.save()` is `users.save(ada)` and `ada.increment(User.visits)` is
`users.update(ada, User.visits.add(1))`. The repository does the same by key, without an
instance: `await users.update(User.key("u1"), User.visits.add(1))`.

## Next steps

- [Modeling](modeling.md) for every field option.
- [Reading data](reading-data.md) and [writing data](writing-data.md).
- [Testing](testing.md) to set up fast tests with the in-memory DynamoDB.
- Run `dynamoxy guide` for the compact reference, or browse the [examples](../../examples/README.md).
