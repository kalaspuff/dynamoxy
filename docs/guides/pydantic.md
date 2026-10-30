# Pydantic models

Applications that already use pydantic can store pydantic models directly
(`pip install 'dynamoxy[pydantic]'`, pydantic 2.7 or newer):

```python
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

import dynamoxy


class Address(BaseModel):
    street: str
    city: str


class Customer(dynamoxy.PydanticModel, table="customers"):
    customer_id: Annotated[str, dynamoxy.PartitionKey()]
    email: Annotated[str, dynamoxy.Unique()]
    name: str = Field(min_length=1)
    credit: Decimal = Decimal(0)
    address: Address | None = None
    created_at: Annotated[datetime | None, dynamoxy.CreatedAt()] = None
    updated_at: Annotated[datetime | None, dynamoxy.UpdatedAt()] = None
    version: Annotated[int | None, dynamoxy.Version()] = None
    expires_at: Annotated[datetime | None, dynamoxy.TTL()] = None

    by_email = dynamoxy.GlobalIndex(partition_key="email")
```

Pydantic validates (types, `Field` constraints, validators); dynamoxy adds the table mapping and
persistence. Everything else works the same: repositories, transactions, unique values,
optimistic locking, hooks, single-table design (`entity=`), indexes. The class is still a pydantic
model: `model_dump()`, JSON schema and FastAPI request and response models work as usual, and
`==` compares values, so an instance read from DynamoDB equals a new one with the same values.

## Existing models

A pydantic model that is already used elsewhere, such as an API schema, can stay unchanged.
Subclass it together with `dynamoxy.PydanticModel` and re-declare only the fields that need a
marker. The other fields, their defaults and the validators are inherited:

```python
class Contact(BaseModel):  # unchanged
    contact_id: str
    email: str
    name: str = Field(min_length=1)


class ContactRecord(Contact, dynamoxy.PydanticModel, table="contacts"):
    contact_id: Annotated[str, dynamoxy.PartitionKey()]
    email: Annotated[str, dynamoxy.Unique()]


contacts = client.model(ContactRecord)
await contacts.create(ContactRecord.model_validate(contact.model_dump()))  # from a Contact
record = await contacts.get("c1")  # a ContactRecord, and therefore a Contact
```

A re-declared field replaces the inherited one, so repeat its type and its default.

Fields cannot take the names of the model API (`key`, `to_item`, `create`, `get`, `query`,
`save`, `update`, `delete`, ... as in [native models](modeling.md#names-to-avoid)): the class
raises `TypeError` when it is defined. When an existing model has such a field, give it another
Python name and keep its name in pydantic's input and output with an alias, and in DynamoDB with
`AttributeName`:

```python
class SearchRequest(BaseModel):  # an API schema whose JSON has a "query" field
    model_config = ConfigDict(populate_by_name=True)  # dynamoxy fills fields by name

    search_id: str
    search_query: str = Field(alias="query")


class SearchRecord(SearchRequest, dynamoxy.PydanticModel, table="searches"):
    search_id: Annotated[str, dynamoxy.PartitionKey()]
    search_query: Annotated[str, dynamoxy.AttributeName("query")] = Field(alias="query")
```

## Markers

| Marker | Meaning |
| --- | --- |
| `PartitionKey(template=None)` | partition key (optionally templated) |
| `SortKey(template=None)` | sort key |
| `Unique()` | unique value with guard items |
| `AttributeName("name")` | stored attribute name |
| `Template("STATUS#{status}")` | derived field |
| `CreatedAt()`, `UpdatedAt()`, `Version()`, `TTL()` | automatic fields (declare them optional with a `None` default); `CreatedAt("iso_z")` chooses a timestamp format |
| `UseCodec(codec)` | custom storage |

Class keywords are the same as for native models, including `datetime_format`
(`class Customer(dynamoxy.PydanticModel, table="customers", datetime_format="iso_z")` stores
datetimes as `2026-10-13T08:48:47.966937Z`; see
[timestamp formats](modeling.md#timestamp-formats)).

## Expressions

At runtime, class attributes are expression paths (`Customer.credit > 10`). Type checkers see
pydantic's field types instead, so wrap them for static typing:

```python
rich = await customers.scan().filter(dynamoxy.attr(Customer.credit) > 10).all()
await customers.update(customer, dynamoxy.attr(Customer.credit).add(Decimal(5)))
```

Instances have the same persistence methods as native models (`save()`, `delete()`,
`update(...)`, `add(...)`, `increment(...)`, `decrement(...)`, `refresh()`), through the
repository that read or wrote them:

```python
customer = await customers.get_or_raise("c1")
customer.name = "Ada"  # validated by pydantic
await customer.save()
await customer.increment(dynamoxy.attr(Customer.credit), Decimal(5))
```

## Nested models

Plain `pydantic.BaseModel` fields (and lists of them) are stored as maps and validated by pydantic
when read. Their `datetime` fields use the default format (`+00:00`); annotate them
`Annotated[datetime, dynamoxy.UseCodec(dynamoxy.codecs.DateTimeCodec("iso_z"))]` for another.

## Differences from native models

- Assignments are validated: `PydanticModel` sets `validate_assignment=True` in its `model_config`.
- Defaults for automatic fields must be `None`; dynamoxy fills them on write.
- `model_dump()` gives Python values; `dynamoxy.schema_of(Customer).encode(customer)` gives the
  stored form.
