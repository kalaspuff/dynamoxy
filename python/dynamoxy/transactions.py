"""Transactions: several writes that succeed or fail together (``TransactWriteItems``).

::

    async with client.transaction() as tx:
        tx.create(order)
        tx.update(account, Account.balance.add(-order.total), condition=Account.balance >= order.total)
        tx.check(Inventory, Inventory.key(sku="A1"), Inventory.stock > 0)
        tx.delete(cart)

The transaction commits when the block ends without an exception. Model writes behave exactly
like their repository counterparts (validation, hooks, versions, timestamps, unique guards). If
DynamoDB cancels the transaction, :class:`dynamoxy.TransactionCanceledError` is raised; its
``cancellation_reasons`` explain each operation, with a specific ``error``
(:class:`ItemAlreadyExistsError`, :class:`VersionConflictError`, :class:`UniqueConstraintError`,
...) where dynamoxy knows what the failed condition meant.

Limits: at most 100 operations (guard items of unique attributes count), 4 MB in total, one
operation per item. Transactions cost twice the capacity of the same writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from dynamoxy.errors import (
    CancellationReason,
    DynamoxyError,
    TransactionCanceledError,
    cancellation_reasons,
    validation_error,
)
from dynamoxy.expressions import Condition, ExpressionBuilder, UpdateAction, as_condition
from dynamoxy.schema import bound_repository, schema_of

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from types import TracebackType

    from dynamoxy.client import Client
    from dynamoxy.repository import Repository, _WritePlan

__all__ = ["Get", "Transaction", "transact_get"]

#: Most operations DynamoDB accepts in one transaction.
MAX_OPERATIONS = 100


def check_token(token: str | None) -> str | None:
    """Validates an idempotency token: 1 to 36 characters (DynamoDB's ``ClientRequestToken``).

    Raises:
        ValueError: The token is empty or too long.
    """
    if token is not None and not 1 <= len(token) <= 36:
        raise ValueError(
            f"an idempotency token has 1 to 36 characters (a UUID fits), got {len(token)}: use a shorter, unique id"
        )
    return token


class Get[M]:
    """One read of :meth:`dynamoxy.Client.transact_get`: ``Get(User, "u1")``.

    Args:
        model: The model class.
        key: Positional key values (partition, then sort).
        key_values: Key fields or the fields their templates use.
    """

    def __init__(self, model: type[M], *key: Any, **key_values: Any) -> None:
        self.model = model
        self.key = schema_of(model).key_from(key, key_values)

    def __repr__(self) -> str:
        return f"Get({self.model.__name__}, {self.key})"


async def transact_get(client: Client, gets: Sequence[Get[Any]]) -> list[Any]:
    """Reads items in one consistent snapshot (see :meth:`dynamoxy.Client.transact_get`)."""
    if not gets:
        return []
    if len(gets) > MAX_OPERATIONS:
        raise ValueError(f"a transaction reads at most {MAX_OPERATIONS} items, got {len(gets)}")
    repositories: dict[type, Repository[Any]] = {}
    for get in gets:
        if get.model not in repositories:
            repositories[get.model] = client.model(get.model)
    items = [{"table_name": repositories[get.model].table_name, "key": get.key} for get in gets]
    output = await client.call("transact_get_items", "TransactGetItems", transact_items=items)
    return [
        None if item is None else repositories[get.model]._load(item)
        for get, item in zip(gets, output["responses"], strict=True)
    ]


@dataclass(eq=False)
class _Step:
    """A registered operation, planned at commit."""

    description: str
    plan: Callable[[], Awaitable[_WritePlan | list[dict[str, Any]] | None]]


class Transaction:
    """A write transaction; see the module documentation.

    Use it as ``async with client.transaction() as tx:`` (commits at the end) or call
    :meth:`commit` yourself. Registering operations does no I/O.
    """

    def __init__(self, client: Client, *, token: str | None = None) -> None:
        self.client = client
        self.token = check_token(token)
        self._steps: list[_Step] = []
        self._committed = False
        self._repositories: dict[type, Repository[Any]] = {}

    def __len__(self) -> int:
        return len(self._steps)

    def _repository(self, model: type, target: Any = None) -> Repository[Any]:
        """The repository that plans a write: the one ``target`` is bound to, if it uses this client."""
        bound = bound_repository(target, model, self.client)
        if bound is not None:
            return bound
        repository = self._repositories.get(model)
        if repository is None:
            repository = self._repositories[model] = self.client.model(model)
        return repository

    def _add(self, description: str, plan: Callable[[], Awaitable[Any]]) -> Self:
        if self._committed:
            raise RuntimeError("this transaction was committed already; start a new one")
        self._steps.append(_Step(description, plan))
        return self

    # ---- model operations -------------------------------------------------------------------

    def create(self, item: Any, *, condition: Condition | bool | None = None) -> Self:
        """Creates an item (fails the transaction if the key exists)."""
        repository = self._repository(type(item), item)
        return self._add(f"create {type(item).__name__}", lambda: repository._plan_put(item, "create", condition))

    def put(self, item: Any, *, condition: Condition | bool | None = None) -> Self:
        """Writes an item, replacing any item with the same key."""
        repository = self._repository(type(item), item)
        return self._add(f"put {type(item).__name__}", lambda: repository._plan_put(item, "put", condition))

    def save(self, item: Any, *, condition: Condition | bool | None = None) -> Self:
        """Writes an item with optimistic locking (versioned models)."""
        repository = self._repository(type(item), item)
        return self._add(f"save {type(item).__name__}", lambda: repository._plan_put(item, "save", condition))

    def update(
        self,
        target: Any,
        *actions: UpdateAction,
        condition: Condition | bool | None = None,
        model: type | None = None,
        upsert: bool = False,
    ) -> Self:
        """Updates an item: an instance, or a stored key.

        For a key, give ``model=`` or use model attributes in the actions. Instances are refreshed
        after the commit: dynamoxy applies the update to the item as it was loaded, like DynamoDB
        does (exact for versioned models; reload other models if concurrent writers matter).
        """
        resolved = model or _model_of(target, actions)
        repository = self._repository(resolved, target)
        return self._add(
            f"update {resolved.__name__}",
            lambda: repository._plan_update(target, actions, condition, upsert),
        )

    def delete(self, target: Any, *, condition: Condition | bool | None = None, model: type | None = None) -> Self:
        """Deletes an item (an instance, or a stored key with ``model=``)."""
        resolved = model or _model_of(target, ())
        repository = self._repository(resolved, target)
        return self._add(f"delete {resolved.__name__}", lambda: repository._plan_delete(target, condition))

    def check(self, model: type, key: Mapping[str, Any] | Any, condition: Condition | bool) -> Self:
        """Requires ``condition`` to hold for an item without writing it (``ConditionCheck``).

        Example::

            tx.check(Account, Account.key("a1"), Account.status == "open")
        """
        repository = self._repository(model)
        checked = as_condition(condition)
        if checked is None:
            raise ValueError("check() needs a condition")

        async def plan() -> list[dict[str, Any]]:
            builder = ExpressionBuilder()
            return [
                {
                    "kind": "condition_check",
                    "table_name": repository.table_name,
                    "key": repository._target_key(key),
                    "condition_expression": builder.condition(checked),
                    "return_values_on_condition_check_failure": "ALL_OLD",
                    **builder.arguments(),
                }
            ]

        return self._add(f"check {model.__name__}", plan)

    # ---- dict-level operations ----------------------------------------------------------------

    def put_item(self, table: str, item: Mapping[str, Any], *, condition: Condition | bool | None = None) -> Self:
        """Writes a raw item to a table (logical name)."""
        return self._add(f"put {table}", self._raw("put", table, {"item": dict(item)}, condition))

    def update_item(
        self, table: str, key: Mapping[str, Any], *actions: UpdateAction, condition: Condition | bool | None = None
    ) -> Self:
        """Updates a raw item."""
        return self._add(f"update {table}", self._raw("update", table, {"key": dict(key)}, condition, actions))

    def delete_item(self, table: str, key: Mapping[str, Any], *, condition: Condition | bool | None = None) -> Self:
        """Deletes a raw item."""
        return self._add(f"delete {table}", self._raw("delete", table, {"key": dict(key)}, condition))

    def check_item(self, table: str, key: Mapping[str, Any], condition: Condition | bool) -> Self:
        """Requires ``condition`` to hold for a raw item."""
        return self._add(f"check {table}", self._raw("condition_check", table, {"key": dict(key)}, condition))

    def _raw(
        self,
        kind: str,
        table: str,
        fields: dict[str, Any],
        condition: Condition | bool | None,
        actions: Sequence[UpdateAction] = (),
    ) -> Callable[[], Awaitable[list[dict[str, Any]]]]:
        async def plan() -> list[dict[str, Any]]:
            builder = ExpressionBuilder()
            operation: dict[str, Any] = {
                "kind": kind,
                "table_name": self.client.table_name(table),
                **fields,
                "return_values_on_condition_check_failure": "ALL_OLD",
            }
            if actions:
                operation["update_expression"] = builder.update(actions)
            checked = as_condition(condition)
            if checked is not None:
                operation["condition_expression"] = builder.condition(checked)
            operation.update(builder.arguments())
            return [operation]

        return plan

    # ---- commit -------------------------------------------------------------------------------

    async def commit(self) -> None:
        """Plans every operation (running ``before_*`` hooks) and writes them atomically.

        Raises:
            TransactionCanceledError: DynamoDB canceled the transaction; see
                ``error.cancellation_reasons``.
            ValueError: More than 100 operations, or none.
        """
        if self._committed:
            raise RuntimeError("this transaction was committed already")
        self._committed = True
        if not self._steps:
            return
        operations: list[dict[str, Any]] = []
        owners: list[tuple[int, _WritePlan | None, Any]] = []  # per operation: (step, plan, explain)
        plans: list[_WritePlan] = []
        for index, step in enumerate(self._steps):
            planned = await step.plan()
            if planned is None:  # e.g. deleting an item that does not exist (unique guards)
                continue
            if isinstance(planned, list):
                for operation in planned:
                    operations.append(operation)
                    owners.append((index, None, None))
                continue
            plans.append(planned)
            for operation, explain in zip(planned.plan.operations, planned.plan.explain, strict=True):
                operations.append(operation)
                owners.append((index, planned, explain))
        if len(operations) > MAX_OPERATIONS:
            raise validation_error(
                f"the transaction has {len(operations)} operations (guard items of unique attributes included); "
                f"DynamoDB allows {MAX_OPERATIONS}"
            )
        if not operations:
            return
        try:
            await self.client.call(
                "transact_write_items",
                "TransactWriteItems",
                transact_items=operations,
                client_request_token=self.token or str(uuid.uuid4()),
            )
        except TransactionCanceledError as error:
            self._explain(error, owners)
            raise
        # Every write was made: each instance is finalized even when a hook of another one raises.
        errors: list[Exception] = []
        for plan in plans:
            try:
                await plan.finalize(None)
            except Exception as error:  # noqa: BLE001 - raised once every plan is final
                errors.append(error)
        if errors:
            raise errors[0]

    def _explain(self, error: TransactionCanceledError, owners: list[tuple[int, _WritePlan | None, Any]]) -> None:
        reasons: list[CancellationReason] = []
        errors: list[DynamoxyError] = []
        for reason in cancellation_reasons(error):
            if reason.index >= len(owners):
                reasons.append(reason)
                continue
            step_index, plan, explain = owners[reason.index]
            specific: DynamoxyError | None = None
            if reason.code == "ConditionalCheckFailed" and plan is not None:
                specific = plan.repository.explain_failure(explain, reason.item, plan, error)
            if specific is not None:
                errors.append(specific)
            reasons.append(
                CancellationReason(
                    index=reason.index,
                    code=reason.code,
                    message=reason.message,
                    item=reason.item,
                    error=specific,
                    operation=self._steps[step_index].description,
                )
            )
        error._explained_reasons = reasons  # pyrefly: ignore[missing-attribute]
        error.errors = tuple(errors)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is None:
            await self.commit()


def _model_of(target: Any, actions: Sequence[UpdateAction]) -> type:
    from dynamoxy.attributes import FieldPath

    if hasattr(type(target), "__dynamoxy_build_schema__") and not isinstance(target, type):
        return type(target)
    for action in actions:
        if isinstance(action.path, FieldPath):
            return action.path.model
    raise TypeError("cannot tell the model of this operation; pass model=User")
