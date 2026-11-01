# Examples

Every example is a runnable script with an `async def main(client=None)`. It uses the in-memory
DynamoDB by default (`uv run python examples/<name>.py`), and the test suite runs each one on the
in-memory backend and on the emulator (`tests/integration/test_examples.py`). To run one against
an emulator or AWS, pass your own client:

```python
import asyncio

import dynamoxy
from examples import quickstart

asyncio.run(quickstart.main(dynamoxy.Client(endpoint_url="http://localhost:4566", region="us-east-1")))
```

| Example | Shows |
| --- | --- |
| [quickstart.py](quickstart.py) | A model with a unique email, timestamps and a version; create, atomic updates and `save()` through the instance, query an index |
| [single_table.py](single_table.py) | Several entities in one table, key templates, derived index keys, collections, nested documents |
| [transactions.py](transactions.py) | An atomic money transfer, idempotency tokens, cancellation reasons, transactional reads |
| [unique_constraints.py](unique_constraints.py) | Unique values, freeing a value by changing it, optimistic locking with retries |
| [pydantic_models.py](pydantic_models.py) | Pydantic models as tables, `Annotated` markers, nested pydantic models, `dynamoxy.attr()` |
| [repository_pattern.py](repository_pattern.py) | Domain repositories, a service layer and transactions as a unit of work |
| [pagination.py](pagination.py) | API pagination with opaque cursors, newest first |
| [observability.py](observability.py) | Event hooks, middleware, request cost accounting with `collect()`, metrics |
| [testing_with_faults.py](testing_with_faults.py) | The in-memory DynamoDB, injected throttling, captured events, TTL purging |
| [stream_handler.py](stream_handler.py) | A DynamoDB Streams handler for AWS Lambda: changes of models from stream records, partial batch failures |
| [raw_tables.py](raw_tables.py) | Tables without models: dict-level items, PartiQL, DynamoDB's typed JSON |
