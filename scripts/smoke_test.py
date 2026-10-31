"""Smoke test for an installed wheel: standard library and dynamoxy only, no network.

    python -I scripts/smoke_test.py

The release workflow runs it against every wheel on every platform; it touches the native
module (values, expressions, the in-memory DynamoDB), the async bridge and the pure-Python layer
(models, repositories, transactions, events).
"""

from __future__ import annotations

import asyncio
import platform
import sys
from decimal import Decimal

import dynamoxy
from dynamoxy import Attr, field, partition_key, sort_key


class Account(dynamoxy.Model, table="smoke-accounts"):
    account_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    balance: Attr[Decimal] = field(default=Decimal(0), ge=0)
    tags: Attr[set[str]] = field(default_factory=set)
    version: Attr[str] = dynamoxy.version()


class Entry(dynamoxy.Model, table="smoke-entries"):
    account_id: Attr[str] = partition_key()
    seq: Attr[int] = sort_key()
    amount: Attr[Decimal]


async def main() -> None:
    assert dynamoxy.from_wire(dynamoxy.to_wire({"n": Decimal("1.5"), "b": b"\x00"})) == {
        "n": Decimal("1.5"),
        "b": b"\x00",
    }
    seen: list[dynamoxy.Event] = []
    async with dynamoxy.testing.memory_client() as client:
        client.on("table.created", seen.append)
        await client.create_tables(Account, Entry)
        accounts, entries = client.model(Account), client.model(Entry)
        account = await accounts.create(Account(account_id="a1", email="smoke@example.com"))
        created_version = account.version
        try:
            await accounts.create(Account(account_id="a2", email="smoke@example.com"))
        except dynamoxy.UniqueConstraintError:
            pass
        else:
            raise AssertionError("expected UniqueConstraintError")
        async with client.transaction() as tx:
            tx.update(account, Account.balance.add(10), Account.tags.add({"smoke"}))
            tx.create(Entry(account_id="a1", seq=1, amount=Decimal(10)))
        assert account.balance == Decimal(10), account
        assert account.version not in (None, created_version), account
        await entries.batch_put(Entry(account_id="a1", seq=seq, amount=Decimal(seq)) for seq in range(2, 60))
        page = await entries.query(account_id="a1").where(Entry.seq > 10).page_size(20).page()
        assert len(page.items) == 20, page
        assert page.cursor is not None
        assert await entries.query(account_id="a1").after(page.cursor).count() == 29
    assert [event.kind for event in seen] == [dynamoxy.EventKind.TABLE_CREATED] * 2, seen
    python = sys.version.split()[0]
    print(f"dynamoxy {dynamoxy.__version__} OK on {platform.system()} {platform.machine()} Python {python}")


if __name__ == "__main__":
    asyncio.run(main())
