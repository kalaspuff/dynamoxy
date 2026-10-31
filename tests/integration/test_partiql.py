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


async def test_order_by(client: dynamoxy.Client, table: str, backend: str) -> None:
    if backend != "memory":
        pytest.skip("MiniStack does not parse ORDER BY, which DynamoDB supports on key attributes")
    for pk in ("a", "b"):
        for sk in range(3):
            await client.execute(f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?}}", pk, sk)
    rows = await client.execute(f'SELECT * FROM "{table}" WHERE pk IN [?, ?] ORDER BY pk DESC, sk DESC', "a", "b")
    assert [(row["pk"], row["sk"]) for row in rows] == [("b", 2), ("b", 1), ("b", 0), ("a", 2), ("a", 1), ("a", 0)]
    with pytest.raises(dynamoxy.ValidationError, match="must be part of the primary key"):
        await client.execute(f'SELECT * FROM "{table}" WHERE pk = ? ORDER BY n', "a")


async def test_transactions_and_batches(client: dynamoxy.Client, table: str, target: str) -> None:
    insert = f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?}}"
    await client.execute_transaction([(insert, ["t", 1]), (insert, ["t", 2])])
    rows = await client.execute_transaction(
        [(f'SELECT * FROM "{table}" WHERE pk = ? AND sk = ?', ["t", sk]) for sk in (1, 2, 3)]
    )
    assert rows == [{"pk": "t", "sk": 1}, {"pk": "t", "sk": 2}, None]
    with pytest.raises(dynamoxy.TransactionCanceledError) as caught:
        await client.execute_transaction([(insert, ["t", 3]), (insert, ["t", 1])])
    # LocalStack (DynamoDB Local) gives the duplicate INSERT the reason ValidationError, with the
    # message "Duplicate primary key exists in table"; DynamoDB gives DuplicateItem (docs/parity,
    # round 3).
    duplicate = "ValidationError" if target == "localstack" else "DuplicateItem"
    assert [reason.code for reason in caught.value.cancellation_reasons] == ["None", duplicate]
    results = await client.batch_execute([(insert, ["b", 1]), (insert, ["t", 1])])
    assert results[0]["error"] is None
    assert results[1]["error"] is not None
    assert results[1]["error"][0] == "DuplicateItem"


async def test_transactions_name_each_item_once(client: dynamoxy.Client, table: str, backend: str) -> None:
    if backend != "memory":
        pytest.skip("MiniStack runs statements on one item one after another, which DynamoDB refuses")
    await client.execute(f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?, 'a': 0, 'b': 0}}", "x", 1)
    statements = [
        (f'UPDATE "{table}" SET a = ? WHERE pk = ? AND sk = ?', [1, "x", 1]),
        (f'UPDATE "{table}" SET b = ? WHERE pk = ? AND sk = ?', [2, "x", 1]),
    ]
    with pytest.raises(dynamoxy.ValidationError, match="cannot include multiple operations on one item"):
        await client.execute_transaction(statements)
    assert await client.execute(f'SELECT a, b FROM "{table}" WHERE pk = ? AND sk = ?', "x", 1) == [{"a": 0, "b": 0}]


async def test_pages_continue_after_the_rows_a_page_returned(client: dynamoxy.Client, table: str, target: str) -> None:
    # A cleanup loop: select a page, delete its rows, continue with the page's token.
    for sk in range(7):
        await client.execute(f"INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?}}", "a", sk)
    seen: list[int] = []
    token: str | None = None
    while True:
        output = await client.call(
            "execute_statement",
            "ExecuteStatement",
            statement=f'SELECT * FROM "{table}" WHERE pk = ?',
            parameters=["a"],
            consistent_read=True,
            limit=2,
            next_token=token,
        )
        rows = [row["sk"] for row in output["items"]]
        seen += rows
        for sk in rows:
            await client.execute(f'DELETE FROM "{table}" WHERE pk = ? AND sk = ?', "a", sk)
        token = output["next_token"]
        if token is None:
            break
        if target != "localstack":  # LocalStack (DynamoDB Local) returns no LastEvaluatedKey here
            assert output["last_evaluated_key"] == {"pk": "a", "sk": rows[-1]}
    assert seen == list(range(7))
