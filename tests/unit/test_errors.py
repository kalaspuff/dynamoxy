"""Errors cross process boundaries: multiprocessing and task queues pickle them."""

from __future__ import annotations

import copy as copy_module
import pickle
import traceback
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

import dynamoxy
import dynamoxy.errors
from dynamoxy import _core, testing
from tests.models import User

NATIVE = sorted(
    (value for value in vars(_core).values() if isinstance(value, type) and issubclass(value, dynamoxy.DynamoxyError)),
    key=lambda value: value.__name__,
)
ALL = sorted(
    (
        value
        for name in dynamoxy.errors.__all__
        if isinstance(value := getattr(dynamoxy.errors, name), type) and issubclass(value, BaseException)
    ),
    key=lambda value: value.__name__,
)


def round_trip(error: BaseException) -> Any:
    return pickle.loads(pickle.dumps(error))


@pytest.mark.parametrize("error_type", NATIVE, ids=lambda error_type: error_type.__name__)
def test_native_errors_name_their_module(error_type: type[Exception]) -> None:
    assert error_type.__module__ == "dynamoxy.errors"
    assert getattr(dynamoxy.errors, error_type.__name__) is error_type
    assert traceback.format_exception_only(error_type("boom")) == [f"dynamoxy.errors.{error_type.__name__}: boom\n"]
    copy = round_trip(error_type("boom"))
    assert (type(copy), str(copy)) == (error_type, "boom")


@pytest.mark.parametrize("error_type", ALL, ids=lambda error_type: error_type.__name__)
def test_every_error_round_trips(error_type: type[Exception]) -> None:
    # Every class pickles on every protocol and comes back with its message and attributes
    # (subclasses with more arguments than the message: test_subclasses_with_more_arguments_unpickle).
    error = error_type("boom")
    error.extra = {"n": 1}  # pyrefly: ignore[missing-attribute]
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        copy = pickle.loads(pickle.dumps(error, protocol))
        assert (type(copy), copy.args, vars(copy)) == (error_type, ("boom",), vars(error))


async def test_errors_of_calls_keep_their_attributes() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(User)
        await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
        with pytest.raises(dynamoxy.ConditionalCheckFailedError) as raw:
            await client.model(User).table.put({"user_id": "u1"}, condition=dynamoxy.F("user_id").not_exists())
        with pytest.raises(dynamoxy.ItemAlreadyExistsError) as exists:
            await User.create(client, user_id="u1", email="bob@example.com", name="Bob")
        with pytest.raises(dynamoxy.UniqueConstraintError) as unique:
            await User.create(client, user_id="u2", email="ada@example.com", name="Ada")
        testing.inject_fault(client, operation="GetItem", error="throttling", times=10)
        with pytest.raises(dynamoxy.ThrottlingError) as throttled:
            await User.get(client, "u1")
    for caught in (raw, exists, unique, throttled):
        error = caught.value
        copy = round_trip(error)
        assert type(copy) is type(error)
        assert str(copy) == str(error)
        assert vars(copy) == vars(error)
        assert (copy.code, copy.retryable, copy.attempts) == (error.code, error.retryable, error.attempts)


def test_python_errors_keep_their_attributes() -> None:
    errors: list[dynamoxy.DynamoxyError] = [
        dynamoxy.ItemNotFoundError("missing", model=User, key={"user_id": "u1"}),
        dynamoxy.VersionConflictError("stale", model=User, key={"user_id": "u1"}, expected_version="3f2a"),
        dynamoxy.ItemDecodeError("bad", model=User, key={"user_id": "u1"}),
        dynamoxy.UnprocessedItemsError("left", unprocessed=[("put", {"user_id": "u1"})]),
    ]
    for error in errors:
        copy = round_trip(error)
        assert (type(copy), str(copy), vars(copy)) == (type(error), str(error), vars(error))


def test_validation_errors_leave_out_values_that_do_not_pickle() -> None:
    with pytest.raises(dynamoxy.ValidationError) as caught:
        User(user_id="u1", email=lambda: 1, name="", age=-1)  # type: ignore[arg-type]
    copy = round_trip(caught.value)
    assert type(copy) is dynamoxy.ValidationError
    assert [(error.path, error.message) for error in copy.errors] == [
        (error.path, error.message) for error in caught.value.errors
    ]
    values = {error.path: error.value for error in copy.errors}
    assert values == {"email": None, "name": "", "age": -1}, "the function is left out"
    assert {error.path: error.value for error in deepcopy(caught.value.errors)}["email"] is not None


async def test_canceled_transactions_keep_their_reasons() -> None:
    async with testing.memory_client() as client:
        await client.create_tables(User)
        await User.create(client, user_id="u1", email="ada@example.com", name="Ada")
        with pytest.raises(dynamoxy.TransactionCanceledError) as caught:  # noqa: PT012 - the block commits
            async with client.transaction() as tx:
                tx.create(User(user_id="u1", email="bob@example.com", name="Bob"))
                tx.create(User(user_id="u2", email="ada@example.com", name="Ada"))
    error = caught.value
    copy = round_trip(error)
    assert type(copy) is dynamoxy.TransactionCanceledError
    assert copy.reasons == error.reasons
    assert [replace(reason, error=None) for reason in copy.cancellation_reasons] == [
        replace(reason, error=None) for reason in error.cancellation_reasons
    ]
    assert [type(reason.error) for reason in copy.cancellation_reasons] == [
        type(reason.error) for reason in error.cancellation_reasons
    ]
    assert [type(specific) for specific in copy.errors] == [
        dynamoxy.ItemAlreadyExistsError,
        dynamoxy.UniqueConstraintError,
    ]
    assert [vars(specific) for specific in copy.errors] == [vars(specific) for specific in error.errors]


def make_local_model() -> type[dynamoxy.Model]:
    class Local(dynamoxy.Model, table="local"):
        pk: dynamoxy.Attr[str] = dynamoxy.partition_key()

    return Local


def test_attributes_that_do_not_pickle_are_left_out() -> None:
    # A model defined in a function does not pickle: the error still crosses process boundaries.
    local = make_local_model()
    unique = dynamoxy.UniqueConstraintError("taken", model=User, field="email", value=lambda: 1)
    missing = dynamoxy.ItemNotFoundError("missing", model=local, key={"pk": "p1"})
    for error in (missing, unique):
        with pytest.raises((pickle.PicklingError, AttributeError, TypeError)):
            pickle.dumps(dict(vars(error)))
    copy = round_trip(missing)
    assert (type(copy), str(copy), copy.model, copy.key) == (dynamoxy.ItemNotFoundError, "missing", None, {"pk": "p1"})
    copy = round_trip(unique)
    assert (copy.model, copy.field, copy.value) == (User, "email", None)
    reason = dynamoxy.CancellationReason(index=0, code="ConditionalCheckFailed", error=missing)
    canceled = dynamoxy.TransactionCanceledError("canceled")
    canceled._explained_reasons = [reason]  # pyrefly: ignore[missing-attribute]
    copy = round_trip(canceled)
    assert copy.cancellation_reasons[0].error.key == {"pk": "p1"}
    assert copy.cancellation_reasons[0].error.model is None


def test_copies_keep_every_attribute() -> None:
    local = make_local_model()
    error = dynamoxy.ItemNotFoundError("missing", model=local, key={"pk": "p1"})
    error.attempts = 2
    shallow, deep = copy_module.copy(error), deepcopy(error)
    for copied in (shallow, deep):
        assert (type(copied), copied.args, vars(copied)) == (dynamoxy.ItemNotFoundError, ("missing",), vars(error))
        assert copied.model is local
    assert shallow.key is error.key
    assert deep.key is not error.key


class OrderMissingError(dynamoxy.ItemNotFoundError):
    """An application's error with a second required argument."""

    def __init__(self, message: str, order_id: str) -> None:
        super().__init__(message, model=User, key={"user_id": order_id})
        self.order_id = order_id


def test_subclasses_with_more_arguments_unpickle() -> None:
    # Unpickling and copying do not call the constructor: they restore the attributes.
    error = OrderMissingError("order o1 does not exist", "o1")
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        copy = pickle.loads(pickle.dumps(error, protocol))
        assert (type(copy), copy.args, vars(copy)) == (OrderMissingError, error.args, vars(error))
    for copied in (copy_module.copy(error), deepcopy(error)):
        assert (type(copied), copied.args, vars(copied)) == (OrderMissingError, error.args, vars(error))


class StrictError(Exception):
    """A foreign error that pickles, but does not unpickle (its constructor takes two arguments)."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def test_only_the_parts_that_do_not_pickle_are_left_out() -> None:
    local = make_local_model()
    error = dynamoxy.ItemNotFoundError("missing", model=User, key={"user_id": "u1", "tenant": local})
    error.history = [("get", lambda: 1), ("query", 2)]  # pyrefly: ignore[missing-attribute]
    foreign = StrictError("boom", "detail")
    error.foreign = foreign  # pyrefly: ignore[missing-attribute]
    pickle.dumps(foreign)  # it pickles; unpickling it fails
    copy = round_trip(error)
    assert (copy.model, copy.key) == (User, {"user_id": "u1", "tenant": None})
    assert copy.history == [("get", None), ("query", 2)]
    assert copy.foreign is None
    reason = dynamoxy.CancellationReason(index=0, code="ConditionalCheckFailed", item={"pk": "p1", "f": lambda: 1})
    canceled = dynamoxy.TransactionCanceledError("canceled")
    canceled._explained_reasons = [reason]  # pyrefly: ignore[missing-attribute]
    restored = round_trip(canceled).cancellation_reasons[0]
    assert (restored.code, restored.item) == ("ConditionalCheckFailed", {"pk": "p1", "f": None})


def test_values_left_out_always_pickle() -> None:
    # A dict key that does not pickle leaves out the dict; a list that holds itself does not recurse
    # forever.
    local = make_local_model()
    unique = dynamoxy.UniqueConstraintError("taken", model=User, field="email", value={local: 1, "a": 2})
    assert round_trip(unique).value is None
    cycle: list[Any] = [lambda: 1, 2]
    cycle.append(cycle)
    error = dynamoxy.ItemNotFoundError("missing")
    error.history = cycle  # pyrefly: ignore[missing-attribute]
    assert round_trip(error).history == [None, 2, None]


def test_errors_that_hold_themselves_pickle() -> None:
    error = dynamoxy.ItemNotFoundError("missing")
    error.me = error  # pyrefly: ignore[missing-attribute]
    copy = round_trip(error)
    assert copy.me is copy
    for history in ([lambda: 1, error], [error, lambda: 1]):
        error.me = None  # pyrefly: ignore[missing-attribute]
        error.history = history  # pyrefly: ignore[missing-attribute]
        copy = round_trip(error)
        assert type(copy) is dynamoxy.ItemNotFoundError
        assert len(vars(copy)["history"]) == 2
