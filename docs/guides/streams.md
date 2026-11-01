# Streams and Lambda

A table's [DynamoDB stream](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html)
records every write to its items, in order per item. A Lambda function subscribed to the stream
receives the records in batches, and is the usual way to react to writes: projections and
counters, search indexes, an outbox, an audit log. `dynamoxy.streams` turns the records into
changes of models, with no dependency of its own.

## Changes of models

```python
from dynamoxy import streams

for change in streams.changes(event, Customer, Order):   # Iterator[Change[Customer | Order]]
    change.event_name       # "INSERT", "MODIFY" or "REMOVE"
    change.new, change.old  # instances, or None (see view types below)
    change.model            # Customer or Order
    change.key              # the stored primary key: {"pk": "CUSTOMER#c1", "sk": "ORDER#o1"}
    change.expired          # removed by Time to Live
    change.sequence_number  # the record's SequenceNumber
```

`changes()` takes a Lambda event (`{"Records": [...]}`), the response of the streams API's
`GetRecords` (boto3), a list of records or one record, and the models whose changes you want. A
`Change` also holds the record's `event_id`, `table` (from `eventSourceARN`), `created_at` (UTC),
`view_type` and the `record` itself.

Which model a record is:

- Guard items of unique values ([Constraints](constraints-and-concurrency.md)), which dynamoxy
  stores in the same table, are skipped.
- An image whose type attribute (`_type`, set by `entity=`) names a model given is that model's;
  other entities are skipped, so a handler lists only the entities it cares about. Items without
  a type attribute are the model's without an entity type, at most one per table, and are
  skipped when there is none.
- A record without images (a `KEYS_ONLY` stream, a `REMOVE` in a `NEW_IMAGE` stream, an `INSERT`
  in an `OLD_IMAGE` stream) is the model's whose key it fits: key attributes and types, and the
  prefixes of key templates (`CUSTOMER#{customer_id}` with `PROFILE` against `ORDER#{order_id}`).
  A key that fits several models raises `StreamRecordError`.

| Stream view type | `new` | `old` |
| --- | --- | --- |
| `NEW_AND_OLD_IMAGES` | after `INSERT` and `MODIFY` | before `MODIFY` and `REMOVE` |
| `NEW_IMAGE` | after `INSERT` and `MODIFY` | `None` |
| `OLD_IMAGE` | `None` | before `MODIFY` and `REMOVE` |
| `KEYS_ONLY` | `None` | `None` |

Enable a stream with `await client.table("shop").update_settings(stream="NEW_AND_OLD_IMAGES")`
([Tables](tables.md)) or `stream=` of `create_tables()`.

## A Lambda handler

Create the client once per execution environment (at module level), and report the record that
failed as a [partial batch response](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html)
(`ReportBatchItemFailures` on the event source mapping): Lambda retries the batch from that
record, so the changes before it are not processed again.

```python
import asyncio

import dynamoxy
from dynamoxy import streams

client = dynamoxy.Client()


async def handle(event):
    current = None
    try:
        for change in streams.changes(event, Order, client=client):
            current = change.sequence_number
            if change.event_name == "INSERT":
                await client.model(Customer).update(
                    {"customer_id": change.new.customer_id}, Customer.orders.add(1)
                )
    except dynamoxy.StreamRecordError as error:      # a record that does not decode
        return {"batchItemFailures": [{"itemIdentifier": error.sequence_number}]}
    except dynamoxy.DynamoxyError:                   # a write of the handler failed
        return {"batchItemFailures": [{"itemIdentifier": current}]}
    return {"batchItemFailures": []}


def handler(event, context):
    return asyncio.run(handle(event))
```

[examples/stream_handler.py](../../examples/stream_handler.py) runs such a handler on records
built from the in-memory DynamoDB. Lambda delivers a record at least once: make effects that must
happen once idempotent ([Application patterns](patterns.md), idempotent consumers).

With `client=`:

- instances are bound to the client's repositories, as if read through them: `after_load` hooks
  run, and `save()`, `update()` and `delete()` write through the repository. A versioned model
  checks the image's version, so writing back an image that later writes superseded raises
  `VersionConflictError`.
- records are routed by table: only the records of the tables of the models given (by
  `eventSourceARN`, with the client's `table_prefix` and `table_names`) are read, so models of
  several tables without an entity type can be given together. Without a client, every record
  is read as one of the models.

Without `client=`, instances are not bound to a repository (as from `Model.from_item()`), and
reading needs no client at all.

## Errors

`changes()` checks the models when it is called (`TypeError`: no models, a model without a
table, two models whose items cannot be told apart) and each record when the iterator reaches
it. A record that cannot become a change raises `StreamRecordError`, a subclass of
`ItemDecodeError` with `.sequence_number`, `.event_id`, `.model`, `.key` and `.errors`: an image
that does not fit its model, typed JSON that is not valid, a record that is not a DynamoDB
Streams record (records of Kinesis Data Streams for DynamoDB are not supported), or a key that
fits several models.

## One item at a time

To decode one image yourself:

```python
item = dynamoxy.from_wire(record["dynamodb"]["NewImage"])
user = User.from_item(item)                 # an unbound instance
user = client.model(User).from_item(item)   # bound to the repository: user.save() works
```

Both raise `ItemDecodeError` for an item that is not the model's: a guard item
(`dynamoxy.constraints.is_guard()` tells them), or an item whose type attribute names another
model of the table.

Why it works this way: [ADR 0024](../adr/0024-stream-records-decode-into-model-changes.md).
