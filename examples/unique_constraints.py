"""Unique values and optimistic locking: emails stay unique, concurrent edits are detected.

Run: uv run python examples/unique_constraints.py
"""

from __future__ import annotations

import asyncio

import dynamoxy
from dynamoxy import Attr, field, partition_key


class Member(dynamoxy.Model, table="members"):
    member_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)
    handle: Attr[str] = field(unique=True, pattern=r"[a-z0-9_]{3,20}")
    display_name: Attr[str]
    version: Attr[int] = dynamoxy.version()


async def rename(members: dynamoxy.Repository[Member], member_id: str, display_name: str) -> Member:
    """Read-modify-write with optimistic locking: retries when someone else saved meanwhile."""
    for _ in range(3):
        member = await members.get_or_raise(member_id)
        member.display_name = display_name
        try:
            return await members.save(member)
        except dynamoxy.VersionConflictError:
            continue
    raise RuntimeError(f"{member_id} is changing too often")


async def main(client: dynamoxy.Client | None = None) -> list[str]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    outcomes: list[str] = []
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Member)
        members = db.model(Member)
        ada = await members.create(Member(member_id="m1", email="ada@example.com", handle="ada", display_name="Ada"))

        try:
            await members.create(
                Member(member_id="m2", email="ada@example.com", handle="ada2", display_name="Imposter")
            )
        except dynamoxy.UniqueConstraintError as error:
            outcomes.append(f"{error.field} {error.value!r} is taken")

        # Changing a unique value frees the old one in the same transaction.
        await members.update(ada, Member.email.set("ada@lovelace.dev"))
        await members.create(Member(member_id="m2", email="ada@example.com", handle="grace", display_name="Grace"))
        outcomes.append("ada@example.com could be reused after the change")

        stale = await members.get_or_raise("m1")
        await rename(members, "m1", "Ada Lovelace")
        stale.display_name = "Countess"
        try:
            await members.save(stale)
        except dynamoxy.VersionConflictError:
            outcomes.append("the stale copy was refused")
    for outcome in outcomes:
        print(outcome)
    return outcomes


if __name__ == "__main__":
    asyncio.run(main())
