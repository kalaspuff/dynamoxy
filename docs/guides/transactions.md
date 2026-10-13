# Transactions

`TransactWriteItems` applies up to 100 writes and checks atomically: all of them or none. dynamoxy
wraps it so that model writes inside a transaction behave exactly like repository writes.

```python
async with client.transaction() as tx:
    tx.create(order)                                   # fails the transaction if the key exists
    tx.save(customer)                                  # optimistic locking
    tx.update(account, Account.balance.add(-order.total), condition=Account.balance >= order.total)
    tx.update(Inventory.key("sku-1"), Inventory.stock.add(-1), model=Inventory)
    tx.delete(cart)
    tx.check(Coupon, Coupon.key(code), Coupon.redeemed == False)  # noqa: E712 - an expression
```

The transaction commits when the block ends without an exception; an exception in the block
discards it. Without the block, call `await tx.commit()`. Registering an operation does no I/O:
validation, hooks and planning run at commit, in order.

## Operations

| Method | Operation |
| --- | --- |
| `create(item, condition=)` | put that fails if the key exists |
| `put(item, condition=)` | put |
| `save(item, condition=)` | put with the version check |
| `update(instance_or_key, *actions, condition=, model=, upsert=)` | update (`model=` for keys, unless the actions name model attributes) |
| `delete(instance_or_key, condition=, model=)` | delete |
| `check(Model, key, condition)` | condition check without a write |
| `put_item(table, item)`, `update_item(table, key, *actions)`, `delete_item(table, key)`, `check_item(table, key, condition)` | dict-level operations on logical table names |

Unique guards, versions, timestamps and derived keys are included automatically. Guard items
count toward the 100-operation limit, which dynamoxy checks before sending.

## After the commit

Instances you wrote are updated: created and saved instances get their automatic fields, and
updated instances are refreshed by applying the update to the item as it was loaded (with the
same evaluator the in-memory DynamoDB uses), so `account.balance` is the new balance without
another read. This is exact for versioned models; for others, writes made by other clients
between your read and the commit are not reflected (call `refresh()` if that matters).

## When a transaction is canceled

```python
try:
    async with client.transaction() as tx:
        ...
except dynamoxy.TransactionCanceledError as error:
    for reason in error.cancellation_reasons:
        print(reason.index, reason.operation, reason.code, reason.error)
    first = error.errors[0] if error.errors else None
```

- `reason.code` is DynamoDB's code: `"None"` (this operation was fine), `"ConditionalCheckFailed"`,
  `"TransactionConflict"`, `"ValidationError"`, `"ThrottlingError"`, `"DuplicateItem"`, ...
- `reason.operation` names the operation (`"create Order"`, `"check Coupon"`, `"put events"`).
- `reason.item` is the stored item when a condition failed.
- `reason.error` is the specific error when dynamoxy knows what the condition meant:
  `ItemAlreadyExistsError`, `VersionConflictError`, `UniqueConstraintError`,
  `ItemNotFoundError`, or `ConditionalCheckFailedError` for your own condition.
- `error.errors` lists those specific errors.

Transactions canceled only because of conflicts with other transactions are retried
automatically (with the same token).

## Idempotency tokens

Every commit sends a client request token (generated, or `client.transaction(token=...)`; 1 to 36
characters, such as a UUID, and unique per logical operation across the account and region).
DynamoDB treats a repeated request with the same token within 10 minutes as the same transaction:
it succeeds without being applied again. A *different* transaction with a used token raises
`IdempotentParameterMismatchError`. Because automatic fields (`updated_at`) differ between
attempts, application-level retries are best protected by conditions, such as creating an item
keyed by the operation id in the same transaction (see `examples/transactions.py`).

## Limits and costs

- 100 operations, 4 MB in total, and each item at most once per transaction.
- Every operation costs twice the capacity of the same non-transactional write.
- Items may be in different tables of the same account and region.
- A transaction conflicts with other transactions and writes on the same items; DynamoDB cancels
  one of them (`TransactionConflict`).

## Transactional reads

```python
user, order, missing = await client.transact_get(
    dynamoxy.Get(User, "u1"),
    dynamoxy.Get(Order, customer_id="c1", order_id="o1"),
    dynamoxy.Get(Order, customer_id="c1", order_id="nope"),
)
```

Up to 100 items read as one consistent snapshot (`None` for missing ones), at twice the cost of
consistent reads.
