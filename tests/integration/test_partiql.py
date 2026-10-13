"""PartiQL statements on every backend."""

from __future__ import annotations

import pytest

import dynamoxy


@pytest.fixture
async def table(client: dynamoxy.Client) -> str:
    await client.table("items").create(partition_key=("pk", "S"), sort_key=("sk", "N"))
    return client.table_name("items")


async def test_insert_select_update_delete(client: dynamoxy.Client, table: str) -> None:
    for sk in range(3):
        await client.execute(f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?, 'n': ?}}", "a", sk, sk * 10)
    rows = await client.execute(f'SELECT * FROM "{table}" WHERE pk = ?', "a", consistent=True)
    assert [row["n"] for row in rows] == [0, 10, 20]
    await client.execute(f'UPDATE "{table}" SET n = n + ? WHERE pk = ? AND sk = ?', 5, "a", 1)
    rows = await client.execute(f'SELECT n FROM "{table}" WHERE pk = ? AND sk = ?', "a", 1)
    assert rows == [{"n": 15}]
    await client.execute(f'DELETE FROM "{table}" WHERE pk = ? AND sk = ?', "a", 0)
    assert len(await client.execute(f'SELECT * FROM "{table}" WHERE pk = ?', "a")) == 2
    with pytest.raises(dynamoxy.DuplicateItemError):
        await client.execute(f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?}}", "a", 1)


async def test_transactions_and_batches(client: dynamoxy.Client, table: str) -> None:
    insert = f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?}}"
    await client.execute_transaction([(insert, ["t", 1]), (insert, ["t", 2])])
    rows = await client.execute_transaction(
        [(f'SELECT * FROM "{table}" WHERE pk = ? AND sk = ?', ["t", sk]) for sk in (1, 2, 3)]
    )
    assert rows == [{"pk": "t", "sk": 1}, {"pk": "t", "sk": 2}, None]
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:
        await client.execute_transaction([(insert, ["t", 3]), (insert, ["t", 1])])
    assert [reason.code for reason in caught.value.cancellation_reasons] == ["None", "DuplicateItem"]
    results = await client.batch_execute([(insert, ["b", 1]), (insert, ["t", 1])])
    assert results[0]["error"] is None
    assert results[1]["error"] is not None
    assert results[1]["error"][0] == "DuplicateItem"
