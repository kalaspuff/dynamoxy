"""Observability: event hooks, request cost accounting, metrics and middleware.

OpenTelemetry spans and metrics are recorded automatically when the application configures an
OpenTelemetry SDK (pip install 'dynamoxy[otel]'); this example shows what needs no setup.

Run: uv run python examples/observability.py
"""

from __future__ import annotations

import asyncio
import logging

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key


class Session(Model, table="web-sessions"):
    session_id: Attr[str] = partition_key()
    user_id: Attr[str]
    hits: Attr[int] = field(default=0)


async def main(client: dynamoxy.Client | None = None) -> dict[str, object]:
    """Runs on the in-memory DynamoDB unless a client is given."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    async with client or dynamoxy.testing.memory_client() as db:
        # Hooks receive structured events (run `dynamoxy events` for every kind).
        retries: list[dynamoxy.Event] = []
        db.on("request.retry", retries.append)

        # Middleware wraps every call: here it tags requests and counts calls per table.
        calls: dict[str, int] = {}

        async def count_calls(request: dynamoxy.Request, call_next: dynamoxy.CallNext) -> dynamoxy.Response:
            calls[request.table or "-"] = calls.get(request.table or "-", 0) + 1
            return await call_next(request)

        db.use(count_calls)
        await db.ensure_tables(Session)
        sessions = db.model(Session)

        # What did this "request" cost? collect() records every operation inside the block.
        with dynamoxy.collect() as usage:
            await sessions.put(Session(session_id="s1", user_id="u1"))
            for _ in range(3):
                await sessions.update(Session.key("s1"), Session.hits.add(1))
            await sessions.get("s1")
        summary = usage.summary()
        print(f"request cost: {summary['calls']} calls, {summary['write_capacity_units']} WCU, ", end="")
        print(f"{summary['read_capacity_units']} RCU, operations {summary['operations']}")

        metrics = db.metrics().operations["UpdateItem"]
        print(f"UpdateItem so far: {metrics.calls} calls, {metrics.errors} errors, {metrics.attempts} attempts")
        print("calls per table:", calls, "| retries seen:", len(retries))
        return summary


if __name__ == "__main__":
    asyncio.run(main())
