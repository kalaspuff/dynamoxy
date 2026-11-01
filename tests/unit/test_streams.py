"""dynamoxy.streams: DynamoDB Streams records (as Lambda receives them) decoded into models."""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key, streams, testing
from dynamoxy.constraints import guard_item
from dynamoxy.schema import schema_of
from tests.models import Customer, Event, LineItem, Order, User

ARN = "arn:aws:dynamodb:eu-north-1:123456789012:table/{table}/stream/2026-11-01T00:00:00.000"


class Reading(Model, table="ExampleTableWithStream"):
    Id: Attr[int] = partition_key()
    Message: Attr[str]


class Invoice(Model, table="app", entity="Invoice"):
    pk: Attr[str] = partition_key(template="CUSTOMER#{customer_id}")
    sk: Attr[str] = sort_key(template="INVOICE#{invoice_id}")
    customer_id: Attr[str]
    invoice_id: Attr[str]


class Note(Model, table="notes", entity="Note"):
    pk: Attr[str] = partition_key(template="NOTE#{note_id}")
    note_id: Attr[str]


class Memo(Model, table="notes", entity="Memo"):
    pk: Attr[str] = partition_key(template="NOTE#{memo_id}")
    memo_id: Attr[str]


class Sticker(Model, table="notes"):
    """The model of a shared table that takes the items without a type attribute."""

    pk: Attr[str] = partition_key()
    color: Attr[str] = field(default="yellow")


def record(
    event_name: str,
    keys: dict[str, Any],
    *,
    new: dict[str, Any] | None = None,
    old: dict[str, Any] | None = None,
    sequence: str = "100",
    view: str = "NEW_AND_OLD_IMAGES",
    table: str = "users",
    ttl: bool = False,
) -> dict[str, Any]:
    """A record in the shape a Lambda function receives (images in DynamoDB's typed JSON)."""
    data: dict[str, Any] = {
        "ApproximateCreationDateTime": 1793520000,
        "Keys": dynamoxy.to_wire(keys, base64_binary=True),
        "SequenceNumber": sequence,
        "SizeBytes": 120,
        "StreamViewType": view,
    }
    if new is not None and event_name != "REMOVE" and view in ("NEW_IMAGE", "NEW_AND_OLD_IMAGES"):
        data["NewImage"] = dynamoxy.to_wire(new, base64_binary=True)
    if old is not None and event_name != "INSERT" and view in ("OLD_IMAGE", "NEW_AND_OLD_IMAGES"):
        data["OldImage"] = dynamoxy.to_wire(old, base64_binary=True)
    result: dict[str, Any] = {
        "eventID": f"event-{sequence}",
        "eventName": event_name,
        "eventVersion": "1.1",
        "eventSource": "aws:dynamodb",
        "awsRegion": "eu-north-1",
        "dynamodb": data,
        "eventSourceARN": ARN.format(table=table),
    }
    if ttl:
        result["userIdentity"] = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}
    return result


def user_item(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"user_id": "u1", "email": "ada@example.com", "name": "Ada", **changes}
    return User(**values).to_item()


def guard_of(email: str) -> dict[str, Any]:
    schema = schema_of(User)
    return guard_item(schema, schema.fields["email"], email, {"user_id": "u1"})


def order_item(order_id: str = "o1") -> dict[str, Any]:
    return Order(
        customer_id="c1", order_id=order_id, placed_at=datetime(2026, 11, 1, tzinfo=UTC), total=Decimal("9.5")
    ).to_item()


def test_a_record_as_aws_documents_it() -> None:
    # The example event of the DynamoDB Streams and Lambda tutorial, unchanged.
    event = {
        "Records": [
            {
                "eventID": "c4ca4238a0b923820dcc509a6f75849b",
                "eventName": "INSERT",
                "eventVersion": "1.1",
                "eventSource": "aws:dynamodb",
                "awsRegion": "us-east-1",
                "dynamodb": {
                    "Keys": {"Id": {"N": "101"}},
                    "NewImage": {"Message": {"S": "New item!"}, "Id": {"N": "101"}},
                    "ApproximateCreationDateTime": 1428537600,
                    "SequenceNumber": "4421584500000000017450439091",
                    "SizeBytes": 26,
                    "StreamViewType": "NEW_AND_OLD_IMAGES",
                },
                "eventSourceARN": (
                    "arn:aws:dynamodb:us-east-1:123456789012:table/ExampleTableWithStream/stream/2015-06-27T00:48:05.899"
                ),
            }
        ]
    }
    [change] = streams.changes(event, Reading)
    assert change.event_name == "INSERT"
    assert change.model is Reading
    assert change.key == {"Id": 101}
    assert change.new == Reading(Id=101, Message="New item!")
    assert change.old is None
    assert not change.expired
    assert change.sequence_number == "4421584500000000017450439091"
    assert change.event_id == "c4ca4238a0b923820dcc509a6f75849b"
    assert change.table == "ExampleTableWithStream"
    assert change.created_at == datetime(2015, 4, 9, tzinfo=UTC)
    assert change.view_type == "NEW_AND_OLD_IMAGES"
    assert change.record is event["Records"][0]


async def test_inserts_modifies_and_removes() -> None:
    before, after = user_item(visits=1), user_item(visits=2)
    event = {
        "Records": [
            record("INSERT", {"user_id": "u1"}, new=before, sequence="1"),
            record("MODIFY", {"user_id": "u1"}, new=after, old=before, sequence="2"),
            record("REMOVE", {"user_id": "u1"}, old=after, sequence="3"),
            record("REMOVE", {"user_id": "u1"}, old=after, sequence="4", ttl=True),
        ]
    }
    changes = list(streams.changes(event, User))
    assert [(change.event_name, change.sequence_number, change.expired) for change in changes] == [
        ("INSERT", "1", False),
        ("MODIFY", "2", False),
        ("REMOVE", "3", False),
        ("REMOVE", "4", True),
    ]
    assert [(change.new and change.new.visits, change.old and change.old.visits) for change in changes] == [
        (1, None),
        (2, 1),
        (None, 2),
        (None, 2),
    ]
    assert all(change.model is User and change.key == {"user_id": "u1"} for change in changes)
    assert changes[1].new is not None
    with pytest.raises(RuntimeError, match="not bound"):
        await changes[1].new.save()  # without a client, instances are not bound to a repository


def test_a_removal_by_a_user_is_not_expired() -> None:
    removed = record("REMOVE", {"user_id": "u1"}, old=user_item())
    removed["userIdentity"] = {"type": "Service", "principalId": "someone.amazonaws.com"}
    [change] = streams.changes(removed, User)
    assert not change.expired


def test_guard_items_are_skipped() -> None:
    guard = guard_of("ada@example.com")
    event = [
        record("INSERT", {"user_id": guard["user_id"]}, new=guard, sequence="1"),
        record("INSERT", {"user_id": "u1"}, new=user_item(), sequence="2"),
        record("REMOVE", {"user_id": guard["user_id"]}, old=guard, sequence="3"),
        record("REMOVE", {"user_id": guard["user_id"]}, sequence="4", view="KEYS_ONLY"),
    ]
    assert [change.sequence_number for change in streams.changes(event, User)] == ["2"]


def test_entities_are_dispatched_by_type_and_others_skipped() -> None:
    customer = Customer(customer_id="c1", name="Ada").to_item()
    invoice = Invoice(customer_id="c1", invoice_id="i1").to_item()
    event = {
        "Records": [
            record("INSERT", {"pk": customer["pk"], "sk": customer["sk"]}, new=customer, sequence="1", table="app"),
            record("INSERT", {"pk": "CUSTOMER#c1", "sk": "ORDER#o1"}, new=order_item(), sequence="2", table="app"),
            record("INSERT", {"pk": invoice["pk"], "sk": invoice["sk"]}, new=invoice, sequence="3", table="app"),
        ]
    }
    changes = list(streams.changes(event, Customer, Order))
    assert [(change.model, type(change.new)) for change in changes] == [(Customer, Customer), (Order, Order)]
    only_orders = list(streams.changes(event, Order))
    assert [change.sequence_number for change in only_orders] == ["2"]


def test_items_without_a_type_go_to_the_model_without_an_entity_type() -> None:
    event = [
        record("INSERT", {"pk": "NOTE#n1"}, new=Note(note_id="n1").to_item(), sequence="1", table="notes"),
        record("INSERT", {"pk": "s1"}, new={"pk": "s1", "color": "red"}, sequence="2", table="notes"),
        record("INSERT", {"pk": "x1"}, new={"pk": "x1", "_type": "Other"}, sequence="3", table="notes"),
    ]
    assert [type(change.new) for change in streams.changes(event, Note, Sticker)] == [Note, Sticker]
    assert [type(change.new) for change in streams.changes(event, Note)] == [Note], "items without a type: skipped"


def test_records_without_images_go_to_the_model_their_key_fits() -> None:
    customer_key = {"pk": "CUSTOMER#c1", "sk": "PROFILE"}
    order_key = {"pk": "CUSTOMER#c1", "sk": "ORDER#o1"}
    event = [
        record("INSERT", customer_key, sequence="1", view="KEYS_ONLY", table="app"),
        record("MODIFY", order_key, sequence="2", view="KEYS_ONLY", table="app"),
        record("INSERT", {"pk": "CUSTOMER#c1", "sk": "INVOICE#i1"}, sequence="3", view="KEYS_ONLY", table="app"),
        # A removal in a NEW_IMAGE stream, and an insertion in an OLD_IMAGE stream, have only keys.
        record("REMOVE", order_key, new=order_item(), sequence="4", view="NEW_IMAGE", table="app"),
        record("INSERT", order_key, new=order_item(), sequence="5", view="OLD_IMAGE", table="app"),
    ]
    changes = list(streams.changes(event, Customer, Order))
    assert [(change.sequence_number, change.model, change.key) for change in changes] == [
        ("1", Customer, customer_key),
        ("2", Order, order_key),
        ("4", Order, order_key),
        ("5", Order, order_key),
    ]
    assert all(change.new is None and change.old is None for change in changes)


def test_keys_only_records_of_plain_models_check_the_key_types() -> None:
    event = [
        record("INSERT", {"stream": "s1", "seq": 1}, sequence="1", view="KEYS_ONLY", table="events"),
        record("INSERT", {"stream": "s1", "seq": "1"}, sequence="2", view="KEYS_ONLY", table="events"),
        record("INSERT", {"stream": "s1"}, sequence="3", view="KEYS_ONLY", table="events"),
    ]
    assert [change.sequence_number for change in streams.changes(event, Event)] == ["1"]


def test_a_key_that_fits_several_models_is_refused() -> None:
    event = [
        record("INSERT", {"pk": "NOTE#n1"}, new=Note(note_id="n1").to_item(), sequence="1", table="notes"),
        record("INSERT", {"pk": "NOTE#n2"}, sequence="2", view="KEYS_ONLY", table="notes"),
    ]
    changes = streams.changes(event, Note, Memo)
    assert next(changes).sequence_number == "1", "the changes before the record come first"
    with pytest.raises(dynamoxy.StreamRecordError, match="fits several models: Note, Memo") as caught:
        next(changes)
    assert (caught.value.sequence_number, caught.value.event_id) == ("2", "event-2")
    assert caught.value.key == {"pk": "NOTE#n2"}


def test_an_image_that_does_not_fit_raises_with_the_sequence_number() -> None:
    broken = user_item()
    del broken["name"]
    event = [
        record("INSERT", {"user_id": "u0"}, new=user_item(user_id="u0"), sequence="1"),
        record("MODIFY", {"user_id": "u1"}, new=broken, old=user_item(), sequence="2"),
    ]
    changes = streams.changes(event, User)
    next(changes)
    with pytest.raises(dynamoxy.StreamRecordError) as caught:
        next(changes)
    error = caught.value
    assert isinstance(error, dynamoxy.ItemDecodeError)
    assert str(error) == (
        "stream record 2 (MODIFY) (see .key): its NewImage does not decode as User: "
        "name: the stored item has no 'name' attribute"
    )
    assert (error.model, error.key, error.sequence_number) == (User, {"user_id": "u1"}, "2")
    assert [problem.path for problem in error.errors] == ["name"]
    assert isinstance(error.__cause__, dynamoxy.ItemDecodeError)
    copy = pickle.loads(pickle.dumps(error))
    assert (type(copy), str(copy), vars(copy)) == (type(error), str(error), vars(error))


def test_events_records_and_one_record() -> None:
    one = record("INSERT", {"user_id": "u1"}, new=user_item())
    for event in ({"Records": [one]}, [one], (one,), one, iter([one])):
        assert [change.sequence_number for change in streams.changes(event, User)] == ["100"]
    assert list(streams.changes({"Records": []}, User)) == []


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ("INSERT", "not a str"),
        (42, "not a int"),
        ({"Items": []}, "got a mapping without them"),
        ({"Records": {"a": 1}}, "is a dict, not a list of records"),
    ],
)
def test_events_that_are_no_records_are_refused(event: Any, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        streams.changes(event, User)


def test_models_are_checked_when_called() -> None:
    with pytest.raises(TypeError, match="at least one model"):
        streams.changes([])
    with pytest.raises(TypeError, match="LineItem is not stored in a table"):
        streams.changes([], LineItem)
    with pytest.raises(TypeError, match="is not a dynamoxy model"):
        streams.changes([], dict)
    with pytest.raises(TypeError, match=r"User and Event have no entity type, so .* pass client="):
        streams.changes([], User, Event)
    with pytest.raises(TypeError, match="Sticker and Reading have no entity type"):
        streams.changes([], Sticker, Reading)

    class OtherNote(Model, table="elsewhere", entity="Note"):
        pk: Attr[str] = partition_key()

    with pytest.raises(TypeError, match="Note and OtherNote store the same entity type, 'Note'"):
        streams.changes([], Note, OtherNote)
    assert list(streams.changes([], User, User)) == [], "a model given twice is one model"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"dynamodb": None}, r"stream record event-100 \(INSERT\): it has no 'dynamodb' object"),
        ({"eventName": "UPSERT"}, "stream record 100: its eventName is 'UPSERT', not 'INSERT', 'MODIFY' or 'REMOVE'"),
        ({"eventSource": "aws:kinesis"}, "it is not a DynamoDB Streams record \\(eventSource is 'aws:kinesis'\\)"),
        ({"dynamodb": {"SequenceNumber": "7", "NewImage": {"user_id": {"N": 5}}}}, "stream record 7 .* NewImage"),
        ({"dynamodb": {"SequenceNumber": "7", "OldImage": [1]}}, "its OldImage is a list"),
        ({"dynamodb": {"SequenceNumber": "7"}}, "stream record 7 \\(INSERT\\): it has no Keys, NewImage or OldImage"),
        *(
            (
                {
                    "dynamodb": {
                        "SequenceNumber": "7",
                        "Keys": {"user_id": {"S": "u1"}},
                        "ApproximateCreationDateTime": t,
                    }
                },
                "stream record 7 \\(INSERT\\): its ApproximateCreationDateTime is ",
            )
            for t in (1e20, float("nan"), float("inf"), Decimal("1e400"), "1793520000", True)
        ),
    ],
)
def test_malformed_records_are_refused(change: dict[str, Any], message: str) -> None:
    malformed = {**record("INSERT", {"user_id": "u1"}, new=user_item()), **change}
    with pytest.raises(dynamoxy.StreamRecordError, match=message):
        list(streams.changes(malformed, User))
    with pytest.raises(dynamoxy.StreamRecordError, match="a stream record is a mapping, not a str"):
        list(streams.changes(["INSERT"], User))  # pyrefly: ignore[no-matching-overload]


def test_records_from_boto3() -> None:
    # The streams API through boto3: binary values as bytes and the creation time as a datetime.
    class Blob(Model, table="blobs"):
        blob_id: Attr[bytes] = partition_key()
        data: Attr[bytes]

    created = datetime(2026, 11, 1, 12, 0)
    raw = {
        "eventID": "e1",
        "eventName": "INSERT",
        "dynamodb": {
            "ApproximateCreationDateTime": created,
            "Keys": {"blob_id": {"B": b"\x00k"}},
            "NewImage": {"blob_id": {"B": b"\x00k"}, "data": {"B": b"\xff"}},
            "SequenceNumber": "1",
        },
    }
    [change] = streams.changes({"Records": [raw], "NextShardIterator": "x"}, Blob)
    assert change.new == Blob(blob_id=b"\x00k", data=b"\xff")
    assert change.created_at == created.replace(tzinfo=UTC)
    assert change.table is None


async def test_with_a_client_records_are_routed_by_table_and_instances_bound() -> None:
    loaded: list[str] = []
    async with testing.memory_client(table_prefix="prod-") as client:
        client.hooks.add("after_load", lambda instance, context: loaded.append(type(instance).__name__))
        event = [
            record("INSERT", {"user_id": "u1"}, new=user_item(), sequence="1", table="prod-users"),
            record("INSERT", {"user_id": "u1"}, new=user_item(), sequence="2", table="users"),
            record(
                "INSERT",
                {"stream": "s", "seq": 1},
                new=Event(stream="s", seq=1, kind="k").to_item(),
                table="prod-events",
            ),
        ]
        changes = list(streams.changes(event, User, Event, client=client))
        assert [(change.sequence_number, change.model) for change in changes] == [("1", User), ("100", Event)]
        assert changes[0].new is not None
        assert changes[0].new.is_loaded
        assert loaded == ["User", "Event"]

        unrouted = record("INSERT", {"user_id": "u1"}, new=user_item())
        del unrouted["eventSourceARN"]
        with pytest.raises(dynamoxy.StreamRecordError, match="no eventSourceARN, so its table is unknown"):
            list(streams.changes(unrouted, User, Event, client=client))
        assert [change.model for change in streams.changes(unrouted, User, client=client)] == [User]


def test_from_item_refuses_guard_items_and_other_entities() -> None:
    with pytest.raises(dynamoxy.ItemDecodeError, match="it is the guard item of a unique value") as caught:
        User.from_item(guard_of("ada@example.com"))
    assert caught.value.key == {"user_id": "_unique#User#email#ada@example.com"}
    with pytest.raises(
        dynamoxy.ItemDecodeError,
        match=r"it stores another model, Order \(_type is 'Order'\): read it with Order.from_item\(\)",
    ):
        Customer.from_item(order_item())
    renamed = {**Customer(customer_id="c1", name="Ada").to_item(), "_type": "Client"}
    assert Customer.from_item(renamed).name == "Ada", "an entity no model declares (renamed since) still decodes"


async def test_repository_from_item_binds_the_instance() -> None:
    async with testing.memory_client() as client:
        users = client.model(User)
        user = users.from_item(user_item())
        assert user.is_loaded
        with pytest.raises(dynamoxy.ItemDecodeError, match="guard item"):
            users.from_item(guard_of("ada@example.com"))
        with pytest.raises(dynamoxy.ItemDecodeError, match=r"read it with client.model\(Order\).from_item\(\)"):
            client.model(Customer).from_item(order_item())
