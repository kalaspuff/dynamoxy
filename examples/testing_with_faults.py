"""Testing: the in-memory DynamoDB, injected faults, captured events and TTL purging.

The same helpers work in pytest (see the testing guide). This script runs the checks directly.

Run: uv run python examples/testing_with_faults.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import dynamoxy
from dynamoxy import Attr, Model, partition_key, testing


class Token(Model, table="auth-tokens"):
    token_id: Attr[str] = partition_key()
    user_id: Attr[str]
    expires_at: Attr[datetime | None] = dynamoxy.ttl(default=None)


async def main(client: dynamoxy.Client | None = None) -> list[str]:
    """Runs on the in-memory DynamoDB (faults can only be injected there)."""
    del client  # this example needs the in-memory backend
    checks: list[str] = []
    async with testing.memory_client() as db:
        await db.ensure_tables(Token)
        tokens = db.model(Token)

        # Throttling is retried with backoff; the events show it.
        testing.inject_fault(db, operation="PutItem", error="throttling", times=2)
        with testing.capture_events(db, "request.*") as events:
            await tokens.put(Token(token_id="t1", user_id="u1"))
        checks.append(f"put succeeded after {sum(event.kind == 'request.retry' for event in events)} retries")

        # When retries run out, the error says how often it tried.
        testing.inject_fault(db, operation="GetItem", error="throttling", times=10)
        try:
            await tokens.get("t1")
        except dynamoxy.ThrottlingError as error:
            checks.append(f"gave up after {error.attempts} attempts (retryable={error.retryable})")

        # Expired items disappear when DynamoDB's TTL process runs; purge_expired() simulates it.
        now = datetime.now(UTC)
        await tokens.put(Token(token_id="old", user_id="u1", expires_at=now - timedelta(hours=1)))
        checks.append(f"purged {testing.purge_expired(db)} expired token(s)")
    for check in checks:
        print(check)
    return checks


if __name__ == "__main__":
    asyncio.run(main())
