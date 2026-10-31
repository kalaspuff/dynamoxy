"""Errors cross process boundaries: multiprocessing and task queues pickle them."""

from __future__ import annotations

import pickle
import traceback
from copy import deepcopy
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


def round_trip(error: BaseException) -> Any:
    return pickle.loads(pickle.dumps(error))


@pytest.mark.parametrize("error_type", NATIVE, ids=lambda error_type: error_type.__name__)
def test_native_errors_name_their_module(error_type: type[Exception]) -> None:
    assert error_type.__module__ == "dynamoxy.errors"
    assert getattr(dynamoxy.errors, error_type.__name__) is error_type
    assert traceback.format_exception_only(error_type("boom")) == [f"dynamoxy.errors.{error_type.__name__}: boom\n"]
    copy = round_trip(error_type("boom"))
    assert (type(copy), str(copy)) == (error_type, "boom")


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
        dynamoxy.VersionConflictError("stale", model=User, key={"user_id": "u1"}, expected_version=3),
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
