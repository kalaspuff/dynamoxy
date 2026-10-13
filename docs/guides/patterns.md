# Application patterns

## Repositories

A repository owns persistence for one aggregate. Subclass `Repository` and give it methods named
after your domain; services call those methods and never build queries themselves.

```python
class OrderRepository(dynamoxy.Repository[Order]):
    async def recent_for(self, customer_id: str, limit: int = 20) -> list[Order]:
        return await self.query(customer_id=customer_id).descending().limit(limit).all()

    async def mark_paid(self, order: Order) -> Order:
        return await self.update(order, Order.status.set(OrderStatus.PAID), condition=Order.status == OrderStatus.NEW)
```

Why: queries depend on key design, which changes as access patterns evolve; keeping them in one
class keeps the rest of the code stable, makes cost reviews easy (every query is in one place)
and makes services trivially testable with the memory client.

Construct repositories once from the shared client (`OrderRepository(client)`) and inject them.
`client.repository(Order)` returns a plain `Repository[Order]` when you need no extra methods.

## Services and the unit of work

Services implement use cases. When a use case changes several items, use one transaction as the
unit of work so that either everything or nothing happens:

```python
class CheckoutService:
    def __init__(self, client: dynamoxy.Client) -> None:
        self.client = client
        self.orders = OrderRepository(client)
        self.carts = CartRepository(client)

    async def checkout(self, cart: Cart, payment_id: str) -> Order:
        order = Order.from_cart(cart, payment_id)
        async with self.client.transaction(token=payment_id) as tx:  # a UUID: at most 36 characters
            tx.create(order)
            tx.delete(cart)
            for line in order.lines:
                tx.update(Stock.key(line.sku), Stock.available.add(-line.quantity),
                          condition=Stock.available >= line.quantity, model=Stock)
        return order
```

Keep transactions small (they are limited to 100 operations and conflict with concurrent writers)
and free of slow work: compute everything first, then commit.

## Web applications

Create the client in the application's lifespan, share it, and close it at shutdown:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with dynamoxy.Client(table_prefix=settings.table_prefix) as client:
        app.state.orders = OrderRepository(client)
        yield


def orders(request: Request) -> OrderRepository:
    return request.app.state.orders


@app.get("/customers/{customer_id}/orders")
async def list_orders(customer_id: str, cursor: str | None = None, repo: OrderRepository = Depends(orders)):
    page = await repo.query(customer_id=customer_id).descending().page_size(20).after(cursor).page()
    return {"items": [order.to_dict() for order in page.items], "cursor": page.cursor}
```

Map errors to responses in one exception handler (`ItemNotFoundError` → 404,
`UniqueConstraintError`/`ItemAlreadyExistsError`/`VersionConflictError` → 409,
`ValidationError` → 422 with `.errors`).

## Message consumers

Consumers usually get at-least-once delivery. Make handlers idempotent:

```python
async def handle(message: Message) -> None:
    async with client.transaction() as tx:
        tx.create(Processed(message_id=message.id))   # a second delivery cancels the transaction
        tx.update(Balance.key(message.account), Balance.amount.add(message.amount), model=Balance)
```

Catch `TransactionCanceledError` and check whether the `Processed` create failed with
`ItemAlreadyExistsError` (duplicate: acknowledge) or something else (retry).

## Read models and counters

Maintain aggregates when writing instead of counting when reading: an `update(..., add(1))` on a
summary item in the same transaction as the write keeps them consistent and makes reads O(1).

## Evolving data

- Adding a field: give it a default; old items decode with it.
- Renaming a field: keep the stored name with `attribute_name=`.
- Changing a shape: upgrade in `@dynamoxy.after_load`, write back lazily on the next save, and
  backfill with a scan job (`scan().segment(i, n)` per worker, `save()` each item).
- Changing keys or templates: create a new table (or new items), dual-write, backfill, switch
  reads, then remove the old data.
- Changing a timestamp format (`datetime_format`, `format=`): ISO text in either style is read,
  so attributes that are only read and written switch freely (each item is rewritten on its next
  save). Keys, templates, conditions and unique values compare stored text: change those like keys.

## Multi-tenancy

Put the tenant in the partition key (`template="TENANT#{tenant_id}#USER#{user_id}"`) and enforce
it in one place, either a repository base class that requires `tenant_id`, or middleware that
checks `request.params["key"]`/`key_condition_expression` values against the current tenant.
