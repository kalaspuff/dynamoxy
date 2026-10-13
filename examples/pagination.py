"""Pagination for an API: pages with opaque cursors, newest first.

Run: uv run python examples/pagination.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import dynamoxy
from dynamoxy import Attr, Model, partition_key, sort_key


class Message(Model, table="chat-messages"):
    room_id: Attr[str] = partition_key()
    sent_at: Attr[datetime] = sort_key()
    author: Attr[str]
    text: Attr[str]


async def list_messages(
    messages: dynamoxy.Repository[Message], room_id: str, cursor: str | None = None, page_size: int = 10
) -> tuple[list[Message], str | None]:
    """One page of an API endpoint: the room comes from the caller's own authorization."""
    page = await messages.query(room_id=room_id).descending().page_size(page_size).after(cursor).page()
    return page.items, page.cursor


async def main(client: dynamoxy.Client | None = None) -> int:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Message)
        messages = db.repository(Message)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        await messages.batch_put(
            Message(room_id="general", sent_at=start + timedelta(minutes=index), author="ada", text=f"message {index}")
            for index in range(25)
        )

        pages = 0
        cursor: str | None = None
        while True:
            items, cursor = await list_messages(messages, "general", cursor)
            if items:
                pages += 1
                print(f"page {pages}: {items[0].text} ... {items[-1].text}")
            if cursor is None:
                break
        return pages


if __name__ == "__main__":
    asyncio.run(main())
