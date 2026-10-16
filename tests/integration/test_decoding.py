"""Items that other writers stored with other types decode exactly or not at all, on every backend.

Converting them (``int(2.5)``, ``bool("false")``) changed them silently: the next write stored
the converted value, and a converted key pointed at another item.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, sort_key


class Account(Model, table="decoding-accounts"):
    account_id: Attr[str] = partition_key()
    credits: Attr[int] = field(default=0)
    active: Attr[bool] = field(default=False)
    note: Attr[str] = field(default="")


class Reading(Model, table="decoding-readings"):
    sensor: Attr[str] = partition_key()
    at: Attr[int] = sort_key()
    value: Attr[str] = field(default="")


@pytest.fixture
async def accounts(client: dynamoxy.Client) -> dynamoxy.Repository[Account]:
    await client.create_tables(Account, Reading)
    return client.repository(Account)


async def test_a_fraction_in_an_int_field_is_not_truncated(accounts: dynamoxy.Repository[Account]) -> None:
    await accounts.table.put({"account_id": "a1", "credits": Decimal("2.5")})  # another writer
    with pytest.raises(dynamoxy.ItemDecodeError, match="credits: cannot decode a stored Decimal as int") as caught:
        await accounts.get("a1")
    assert caught.value.errors[0].value == Decimal("2.5")
    assert (await accounts.table.get({"account_id": "a1"}) or {})["credits"] == Decimal("2.5")


async def test_text_in_a_bool_field_is_not_converted(accounts: dynamoxy.Repository[Account]) -> None:
    await accounts.table.put({"account_id": "a1", "active": "false"})  # bool("false") is True
    with pytest.raises(dynamoxy.ItemDecodeError, match="active: cannot decode a stored str as bool"):
        await accounts.get("a1")


async def test_a_fraction_in_an_int_key_does_not_read_another_items_key(client: dynamoxy.Client) -> None:
    await client.create_tables(Reading)
    readings = client.repository(Reading)
    await readings.table.put({"sensor": "s1", "at": Decimal("1700000000.5"), "value": "x"})  # another writer
    await readings.table.put({"sensor": "s1", "at": 1700000000, "value": "y"})
    # Truncated, the first item read as the second one's key, and delete() or update() of it
    # changed the second item.
    with pytest.raises(dynamoxy.ItemDecodeError, match="at: cannot decode a stored Decimal as int"):
        await readings.query(sensor="s1").all()
    stored = await readings.table.scan(consistent=True).all()
    assert sorted(str(item["at"]) for item in stored) == ["1700000000", "1700000000.5"]


async def test_writes_whose_returned_item_does_not_decode_say_they_were_applied(
    accounts: dynamoxy.Repository[Account],
) -> None:
    await accounts.table.put({"account_id": "a1", "credits": Decimal("2.5")})
    with pytest.raises(dynamoxy.ItemDecodeError, match="the update was applied") as caught:
        await accounts.update({"account_id": "a1"}, Account.note.set("checked"))
    assert caught.value.key == {"account_id": "a1"}
    assert (await accounts.table.get({"account_id": "a1"}) or {})["note"] == "checked"

    with pytest.raises(dynamoxy.ItemDecodeError, match="the delete was applied"):
        await accounts.delete({"account_id": "a1"})
    assert await accounts.table.get({"account_id": "a1"}) is None
