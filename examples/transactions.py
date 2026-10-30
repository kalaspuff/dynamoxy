"""Transactions: move money between accounts atomically, and read why a transaction failed.

Run: uv run python examples/transactions.py
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key


class Account(Model, table="bank-accounts"):
    account_id: Attr[str] = partition_key()
    owner: Attr[str]
    balance: Attr[Decimal] = field(default=Decimal(0), ge=0)
    version: Attr[int] = dynamoxy.version()


class Transfer(Model, table="bank-transfers"):
    transfer_id: Attr[str] = partition_key()
    source: Attr[str]
    target: Attr[str]
    amount: Attr[Decimal] = field(gt=0)


class LedgerEntry(Model, table="bank-ledger"):
    account_id: Attr[str] = partition_key()
    transfer_id: Attr[str] = sort_key()
    amount: Attr[Decimal]


def new_transfer_id() -> str:
    """A unique transfer id; it doubles as the idempotency token (account-wide, at most 36 characters)."""
    return uuid.uuid4().hex


async def transfer(db: dynamoxy.Client, transfer_id: str, source: str, target: str, amount: Decimal) -> None:
    """Moves ``amount`` exactly once.

    Using the transfer id as the token makes a retry of the same call (after a timeout, say)
    harmless for 10 minutes, and the transfer item refuses the id forever after.
    """
    async with db.transaction(token=transfer_id) as tx:
        tx.create(Transfer(transfer_id=transfer_id, source=source, target=target, amount=amount))
        tx.update(Account.key(source), Account.balance.add(-amount), condition=Account.balance >= amount)
        tx.update(Account.key(target), Account.balance.add(amount))
        tx.create(LedgerEntry(account_id=source, transfer_id=transfer_id, amount=-amount))
        tx.create(LedgerEntry(account_id=target, transfer_id=transfer_id, amount=amount))


async def main(client: dynamoxy.Client | None = None) -> tuple[Decimal, Decimal]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Account, Transfer, LedgerEntry)
        accounts = db.model(Account)
        await accounts.create(Account(account_id="alice", owner="Alice", balance=Decimal(100)))
        await accounts.create(Account(account_id="bob", owner="Bob"))

        first = new_transfer_id()
        await transfer(db, first, "alice", "bob", Decimal(30))
        await transfer(db, first, "alice", "bob", Decimal(30))  # a retry: succeeds, applied once

        try:
            await transfer(db, new_transfer_id(), "alice", "bob", Decimal(500))
        except dynamoxy.TransactionCanceledError as error:
            for reason in error.cancellation_reasons:
                if reason.failed:
                    print(f"canceled: {reason.operation} failed with {reason.code}")

        try:
            await transfer(db, first, "bob", "alice", Decimal(1))  # another transfer with a used id
        except dynamoxy.IdempotentParameterMismatchError:
            print("that transfer id is taken by a different transfer")

        alice, bob = await db.transact_get(dynamoxy.Get(Account, "alice"), dynamoxy.Get(Account, "bob"))
        assert alice is not None and bob is not None
        print(f"alice={alice.balance} bob={bob.balance}")
        return alice.balance, bob.balance


if __name__ == "__main__":
    asyncio.run(main())
