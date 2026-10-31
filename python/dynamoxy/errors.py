"""The exception hierarchy. Every dynamoxy error derives from :class:`DynamoxyError`.

Errors raised by the Rust core are defined in the native module and re-exported here; errors
that only the Python layer raises are defined below and derive from the same base classes.

Every error has ``.retryable`` (whether the same call may succeed later). Errors returned by
DynamoDB also carry ``.code`` (e.g. ``"ConditionalCheckFailedException"``), ``.message``,
``.operation``, ``.request_id`` and ``.status_code``; errors of a call carry ``.attempts``,
``.throttles``, ``.requests`` and ``.duration``.

::

    DynamoxyError
    ├── ConfigurationError
    ├── ValidationError                  (.errors: field errors for model validation)
    │   └── ItemDecodeError              a stored item does not match its model
    ├── ItemNotFoundError                get_or_raise() found nothing
    ├── ServiceError
    │   ├── ConditionalCheckFailedError  (.item)
    │   │   ├── ItemAlreadyExistsError   create() of an existing key
    │   │   ├── VersionConflictError     optimistic locking: the item changed meanwhile
    │   │   └── UniqueConstraintError    a unique value is taken (.field, .value)
    │   ├── TransactionCanceledError     (.cancellation_reasons, .errors)
    │   ├── ThrottlingError, ResourceNotFoundError, ResourceInUseError, ...
    ├── UnprocessedItemsError            a managed batch could not finish (throttling)
    ├── TransportError
    │   └── WriteOutcomeUnknownError     a write may have been applied (.item)
    ├── CredentialsError, ShutdownError
"""

from __future__ import annotations

import copy
import copyreg
import dataclasses
import pickle
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from dynamoxy._core import (
    AccessDeniedError,
    BackupError,
    ConditionalCheckFailedError,
    ConfigurationError,
    CredentialsError,
    DuplicateItemError,
    DynamoxyError,
    IdempotentParameterMismatchError,
    InternalServerError,
    ItemCollectionSizeLimitExceededError,
    LimitExceededError,
    ResourceInUseError,
    ResourceNotFoundError,
    ServiceError,
    ShutdownError,
    ThrottlingError,
    TransactionCanceledError,
    TransactionConflictError,
    TransactionInProgressError,
    TransportError,
    ValidationError,
    WriteOutcomeUnknownError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "AccessDeniedError",
    "BackupError",
    "CancellationReason",
    "ConditionalCheckFailedError",
    "ConfigurationError",
    "CredentialsError",
    "DuplicateItemError",
    "DynamoxyError",
    "FieldError",
    "IdempotentParameterMismatchError",
    "InternalServerError",
    "ItemAlreadyExistsError",
    "ItemCollectionSizeLimitExceededError",
    "ItemDecodeError",
    "ItemNotFoundError",
    "LimitExceededError",
    "ResourceInUseError",
    "ResourceNotFoundError",
    "ServiceError",
    "ShutdownError",
    "ThrottlingError",
    "TransactionCanceledError",
    "TransactionConflictError",
    "TransactionInProgressError",
    "TransportError",
    "UniqueConstraintError",
    "UnprocessedItemsError",
    "ValidationError",
    "VersionConflictError",
    "WriteOutcomeUnknownError",
]

# Class-level defaults: ValidationError carries field errors when raised by model validation, and
# ConditionalCheckFailedError the key of the item when raised by a model write.
ValidationError.errors = ()
ConditionalCheckFailedError.key = None


@dataclass(frozen=True, slots=True)
class FieldError:
    """One problem found by model validation.

    Attributes:
        path: Where the problem is (``"email"``, ``"address.zip"``, ``"tags[2]"``).
        message: What is wrong and what is allowed.
        value: The offending value (left out of ``str()`` and ``repr()``: values may be personal
            data, and messages end up in logs).
    """

    path: str
    message: str
    value: Any = field(default=None, repr=False)

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"

    def __reduce__(self) -> tuple[Any, ...]:
        # A validation error crosses process boundaries (multiprocessing, task queues) with its
        # field errors also when a value that failed validation does not pickle (a function, a
        # lock): the pickle leaves such a value out.
        value = self.value
        try:
            pickle.dumps(value)
        except Exception:  # noqa: BLE001 - whatever does not pickle
            value = None
        return (FieldError, (self.path, self.message, value))

    def __copy__(self) -> FieldError:
        return self  # immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> FieldError:
        return FieldError(self.path, self.message, copy.deepcopy(self.value, memo))


def validation_error(message: str, errors: Sequence[FieldError] = ()) -> ValidationError:
    """Creates a :class:`ValidationError` carrying field errors (``.errors``)."""
    error = ValidationError(message)
    error.errors = tuple(errors)
    error.message = message
    return error


class ItemDecodeError(ValidationError):
    """A stored item does not match its model (wrong types, missing required attributes).

    Attributes:
        model: The model the item was decoded into.
        key: The primary key of the item, when known.
    """

    def __init__(self, message: str, *, model: type | None = None, key: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.key = key
        self.message = message
        self.errors: tuple[FieldError, ...] = ()


class ItemNotFoundError(DynamoxyError):
    """No item has the requested key (raised by ``get_or_raise``).

    Attributes:
        model: The model (or ``None`` for table-level reads).
        key: The key that was looked up.
    """

    def __init__(self, message: str, *, model: type | None = None, key: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.key = key
        self.message = message


def _copy_service_fields(target: DynamoxyError, source: BaseException | None) -> None:
    if source is None:
        return
    for name in ("code", "operation", "request_id", "status_code", "attempts", "throttles", "requests", "duration"):
        value = getattr(source, name, None)
        if value is not None:
            setattr(target, name, value)


class ItemAlreadyExistsError(ConditionalCheckFailedError):
    """``create()`` found an item with the same primary key.

    Attributes:
        model: The model class.
        key: The primary key.
        item: The existing item when DynamoDB returned it, else ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        model: type | None = None,
        key: dict[str, Any] | None = None,
        item: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.key = key
        self.item = item
        self.message = message
        _copy_service_fields(self, cause)


class VersionConflictError(ConditionalCheckFailedError):
    """Optimistic locking failed: the stored item changed since it was loaded.

    The stored version is not the one this instance was loaded with (someone else saved it
    meanwhile), or the item was deleted.

    Reload the item, re-apply the change and save again.

    Attributes:
        model: The model class.
        key: The primary key.
        expected_version: The version token the write expected.
        item: The stored item when DynamoDB returned it, else ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        model: type | None = None,
        key: dict[str, Any] | None = None,
        expected_version: str | None = None,
        item: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.key = key
        self.expected_version = expected_version
        self.item = item
        self.message = message
        _copy_service_fields(self, cause)


class UniqueConstraintError(ConditionalCheckFailedError):
    """A value of a ``unique=True`` attribute is already used by another item.

    Attributes:
        model: The model class.
        field: The Python name of the unique attribute.
        value: The value that is taken.
    """

    def __init__(
        self,
        message: str,
        *,
        model: type | None = None,
        field: str = "",
        value: Any = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.field = field
        self.value = value
        self.message = message
        self.item = None
        _copy_service_fields(self, cause)


class UnprocessedItemsError(DynamoxyError):
    """A managed batch still had unprocessed items after every retry round.

    This means sustained throttling. Nothing is lost silently: ``.unprocessed`` lists what was not
    done.

    Attributes:
        unprocessed: ``(kind, item_or_key)`` pairs, kind ``"put"``, ``"delete"`` or ``"get"``.
    """

    retryable = True

    def __init__(self, message: str, *, unprocessed: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        super().__init__(message)
        # Unpickling (multiprocessing, task queues) calls the class with the message only, then
        # restores the attributes.
        self.unprocessed = unprocessed if unprocessed is not None else []
        self.message = message


@dataclass(frozen=True, slots=True)
class CancellationReason:
    """Why one operation of a canceled transaction failed.

    Attributes:
        index: Position of the operation in the transaction.
        code: ``"None"`` (did not fail), ``"ConditionalCheckFailed"``, ``"TransactionConflict"``,
            ``"ValidationError"``, ``"ThrottlingError"``, ``"DuplicateItem"``, ...
        message: Details from DynamoDB.
        item: The item as it was, when the operation asked for it on condition failure (left out
            of ``repr()``).
        error: A specific error for this operation when dynamoxy knows what the condition meant
            (``ItemAlreadyExistsError``, ``VersionConflictError``, ``UniqueConstraintError``),
            else ``None``.
        operation: A description of the operation (``"put User"``), when known.
    """

    index: int
    code: str
    message: str | None = None
    item: dict[str, Any] | None = field(default=None, repr=False)
    error: DynamoxyError | None = None
    operation: str | None = None

    @property
    def failed(self) -> bool:
        """Whether this operation failed."""
        return self.code != "None"


def cancellation_reasons(error: TransactionCanceledError) -> list[CancellationReason]:
    """The reasons of a canceled transaction as :class:`CancellationReason` objects.

    Also available as ``error.cancellation_reasons``. For transactions of
    :class:`dynamoxy.Transaction` the reasons name each operation (``"create Order"``) and carry
    a specific ``error`` where dynamoxy knows what the failed condition meant.
    """
    explained = getattr(error, "_explained_reasons", None)
    if explained is not None:
        return list(explained)
    raw = getattr(error, "reasons", None) or []
    reasons: list[CancellationReason] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, CancellationReason):
            reasons.append(entry)
            continue
        code, message, item = entry
        reasons.append(CancellationReason(index=index, code=code, message=message, item=item))
    return reasons


# The native class gets the explained view as a property, and an empty ``errors`` default
# (``Transaction`` sets the specific errors of the operations it planned).
setattr(TransactionCanceledError, "cancellation_reasons", property(cancellation_reasons))  # noqa: B010
TransactionCanceledError.errors = ()  # pyrefly: ignore[missing-attribute]


def _round_trips(value: object) -> bool:
    try:
        pickle.loads(pickle.dumps(value))  # noqa: S301 - our own value, unpickled to check it
    except Exception:  # noqa: BLE001 - whatever does not pickle or unpickle
        return False
    return True


def _picklable(value: object, active: frozenset[int] = frozenset()) -> object:
    # Leaves out only what does not pickle: the fields of a dataclass and the items of a tuple, list
    # or dict (a key's value, an item's attribute), else the value itself. Whatever it rebuilds is
    # checked as a whole (a dict key may not pickle), and a container met again inside itself is
    # left out, so the result always pickles.
    if _round_trips(value):
        return value
    if id(value) in active:
        return None
    inner = active | {id(value)}
    rebuilt: object
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            field.name: _picklable(getattr(value, field.name), inner)
            for field in dataclasses.fields(value)
            if field.init
        }
        try:
            rebuilt = dataclasses.replace(value, **fields)
        except Exception:  # noqa: BLE001 - a field the dataclass does not accept as None
            return None
    elif type(value) is tuple:
        rebuilt = tuple(_picklable(item, inner) for item in value)
    elif type(value) is list:
        rebuilt = [_picklable(item, inner) for item in value]
    elif type(value) is dict:
        rebuilt = {key: _picklable(item, inner) for key, item in value.items()}
    else:
        return None
    return rebuilt if _round_trips(rebuilt) else None  # the class or a dict key may not pickle


def _reduce_error(error: BaseException) -> tuple[Any, ...]:
    # An error crosses process boundaries (multiprocessing, task queues) as itself. Unpickling
    # creates it without calling its constructor (an application's subclass may take more
    # arguments than the message) and restores its attributes. A value that does not pickle, or
    # does not unpickle, is left out (None): the model class of a model defined in a function, a
    # unique value of a type that does not pickle. ``__reduce__`` does not know which pickler calls
    # it, so the check uses the standard one; values that only cloudpickle sends (lambdas, classes
    # defined in functions) are left out under it too, so that a process that has cloudpickle
    # loaded still sends errors through ``pickle``.
    state = vars(error)
    if not _round_trips(state):
        state = {name: _picklable(value) for name, value in state.items()}
    return (copyreg.__newobj__, (type(error), *error.args), state)  # pyrefly: ignore[missing-attribute]


def _blank[E: BaseException](error: E, args: tuple[Any, ...]) -> E:
    # A new instance of the error's class, created without calling its constructor, like unpickling.
    cls = type(error)
    return cast("E", cls.__new__(cls, *args))


def _copy_error[E: BaseException](error: E) -> E:
    copied = _blank(error, error.args)
    vars(copied).update(vars(error))
    return copied


def _deepcopy_error[E: BaseException](error: E, memo: dict[int, Any]) -> E:
    copied = _blank(error, copy.deepcopy(error.args, memo))
    memo[id(error)] = copied
    vars(copied).update(copy.deepcopy(vars(error), memo))
    return copied


# Every dynamoxy error, native or defined above, derives from the native base class. Copies keep
# every attribute: only pickles leave out what does not pickle.
setattr(DynamoxyError, "__reduce__", _reduce_error)  # noqa: B010
setattr(DynamoxyError, "__copy__", _copy_error)  # noqa: B010
setattr(DynamoxyError, "__deepcopy__", _deepcopy_error)  # noqa: B010
