"""Errors cross process boundaries: multiprocessing and task queues pickle them."""

from __future__ import annotations

import pickle
from typing import Any

import pytest

import dynamoxy
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
    assert error_type.__module__ == "dynamoxy._core"
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
