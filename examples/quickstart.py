"""Quickstart: declare a model, create its table, write, update and query.

Run: uv run python examples/quickstart.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import dynamoxy
from dynamoxy import Attr, field, partition_key


class User(dynamoxy.Model, table="users"):
    user_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True, pattern=r"[^@\s]+@[^@\s]+")
    name: Attr[str] = field(min_length=1)
    visits: Attr[int] = field(default=0)
    tags: Attr[set[str]] = field(default_factory=set)
    created_at: Attr[datetime] = dynamoxy.created_at()
    updated_at: Attr[datetime] = dynamoxy.updated_at()
    version: Attr[int] = dynamoxy.version()

    by_email = dynamoxy.GlobalIndex(partition_key=email)


async def main(client: dynamoxy.Client | None = None) -> User:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(User)
        users = db.repository(User)

        ada = await users.create(User(user_id="u1", email="ada@example.com", name="Ada"))
        print(f"created {ada.user_id} (version {ada.version}) at {ada.created_at:%Y-%m-%d %H:%M}")

        await users.update(ada, User.visits.add(1), User.tags.add({"admin"}))
        print(f"after update: visits={ada.visits} tags={sorted(ada.tags)} version={ada.version}")

        found = await users.query(index=User.by_email, email="ada@example.com").first()
        assert found is not None
        print(f"found by email: {found.name}")
        return found


if __name__ == "__main__":
    asyncio.run(main())
