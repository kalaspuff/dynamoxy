"""Probes dynamoxy on one backend, and compares the facts of two runs.

The in-memory backend models DynamoDB, and the end-to-end tests run on emulators; this checks all
of them against DynamoDB itself. ``run`` performs about thirty scenarios through dynamoxy (models,
tables, transactions, PartiQL and native calls) on a target and writes what happened as facts:
outcomes, exception classes, error codes and messages, cancellation reasons, sizes and counts.
``compare`` lists every fact that differs between two runs, the first one usually DynamoDB's.

    uv run python scripts/probe_backends.py run --target memory
    uv run python scripts/probe_backends.py run --target ministack --endpoint http://127.0.0.1:4599
    uv run python scripts/probe_backends.py run --target localstack      # http://127.0.0.1:4566
    AWS_PROFILE=<test-profile> uv run python scripts/probe_backends.py run --target aws --region eu-north-1
    uv run python scripts/probe_backends.py compare probe-aws.json probe-memory.json
    uv run python scripts/probe_backends.py iam-policy --account <account-id>
    uv run python scripts/probe_backends.py cleanup --target aws --region eu-north-1

Every table is named ``dynamoxy-probe-<random>-<name>`` and deleted at the end (``cleanup``
deletes what an interrupted run left). A run on AWS takes about 15 minutes, most of it waiting for
global indexes and billing switches, and costs cents: it writes about 25 MB. The facts hold no
account ids, credentials or request ids; table names appear without the random prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dynamoxy
from dynamoxy import Attr, Model, field, partition_key, testing
from dynamoxy.table import IndexDefinition, Projection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

PREFIX = "dynamoxy-probe-"
WAIT = 1500  # seconds: DynamoDB took up to 506 s to backfill a global index of one item
KB = 1024
DEFAULT_ENDPOINTS = {"localstack": "http://127.0.0.1:4566"}


class Player(Model, table="players"):
    """A versioned model."""

    player_id: Attr[str] = partition_key()
    name: Attr[str] = field(default="")
    version: Attr[str] = dynamoxy.version()


class Account(Model, table="accounts"):
    """A model with a unique attribute."""

    account_id: Attr[str] = partition_key()
    email: Attr[str] = field(unique=True)


class Wallet(Model, table="wallets"):
    """Versioned, without timestamps: a transaction planned again is the same request."""

    wallet_id: Attr[str] = partition_key()
    balance: Attr[int] = field(default=0)
    version: Attr[str] = dynamoxy.version()


class Probe:
    """One run: the client, the facts it collects, and helpers for native calls."""

    def __init__(self, client: dynamoxy.Client, prefix: str) -> None:
        self.client = client
        self.prefix = prefix
        self.facts: dict[str, dict[str, Any]] = {}
        self.scenario = ""

    def name(self, logical: str) -> str:
        return self.client.table_name(logical)

    def normalize(self, text: str | None) -> str | None:
        """Without the run's prefix and random tokens, so that runs compare."""
        if text is None:
            return None
        text = text.replace(self.prefix, "")
        return re.sub(r"\b[0-9a-f]{16}\b", "<token>", text)

    def error(self, error: BaseException) -> dict[str, Any]:
        fact: dict[str, Any] = {"error": type(error).__name__}
        code = getattr(error, "code", None)
        if code:
            fact["code"] = code
        message = getattr(error, "message", None) if isinstance(error, dynamoxy.DynamoxyError) else str(error)
        fact["message"] = self.normalize(str(message) if message is not None else str(error))
        reasons = getattr(error, "cancellation_reasons", None)
        if reasons:
            fact["reasons"] = [[reason.code, self.normalize(reason.message)] for reason in reasons]
        return fact

    async def fact(self, name: str, action: Awaitable[Any], summary: Callable[[Any], Any] | None = None) -> Any:
        """Records the outcome of ``action`` as the fact ``name``; returns its result (or None)."""
        try:
            result = await action
        except (dynamoxy.DynamoxyError, ValueError, TypeError) as error:
            self.facts[self.scenario][name] = self.error(error)
            return None
        self.facts[self.scenario][name] = {"ok": summary(result) if summary is not None else True}
        return result

    def note(self, name: str, value: Any) -> None:
        self.facts[self.scenario][name] = value

    async def call(self, method: str, operation: str, **params: Any) -> Any:
        table = params.get("table_name")
        return await self.client.call(method, operation, table=table, **params)

    async def table(self, logical: str, **options: Any) -> dynamoxy.table.Table:
        table = self.client.table(logical)
        await table.create(**options)
        return table


def count(output: Any) -> Any:
    return output["count"]


def size_message(output: Any) -> Any:
    return len(output["responses"])


# ---- scenarios -------------------------------------------------------------------------------


async def versions(probe: Probe) -> None:
    """#84: a stale instance never matches a version again."""
    await probe.client.ensure_tables(Player)
    players = probe.client.model(Player)
    await players.create(Player(player_id="p1", name="Ada"))
    stale = await players.get_or_raise("p1")
    await players.put(Player(player_id="p1", name="Replaced"))
    stale.name = "Stale"
    await probe.fact("save_after_put", players.save(stale))
    await players.delete(Player.key("p1"))
    await players.create(Player(player_id="p1", name="Again"))
    await probe.fact("save_after_delete_and_create", players.save(stale))


async def constraints(probe: Probe) -> None:
    await probe.client.ensure_tables(Account, Player)
    accounts, players = probe.client.model(Account), probe.client.model(Player)
    await accounts.create(Account(account_id="c1", email="c1@example.com"))
    await probe.fact("unique", accounts.create(Account(account_id="c2", email="c1@example.com")))
    await probe.fact("create_existing", accounts.create(Account(account_id="c1", email="c3@example.com")))
    player = await players.create(Player(player_id="c1"))
    await probe.fact("condition", players.update(player, Player.name.set("x"), condition=Player.name == "other"))


async def transactions(probe: Probe) -> None:
    await probe.client.ensure_tables(Account, Wallet)
    await probe.client.model(Account).create(Account(account_id="t1", email="t1@example.com"))
    wallets = probe.client.model(Wallet)
    await wallets.create(Wallet(wallet_id="w1"))

    async def canceled() -> None:
        async with probe.client.transaction() as tx:
            tx.create(Account(account_id="t1", email="t2@example.com"))
            tx.update(Wallet.key("w1"), Wallet.balance.add(1), condition=Wallet.balance > 5)

    await probe.fact("reasons", canceled())
    token = uuid.uuid4().hex
    for _ in range(2):
        async with probe.client.transaction(token=token) as tx:
            tx.update(Wallet.key("w1"), Wallet.balance.add(10))
    probe.note("planned_again_with_token", (await wallets.get_or_raise("w1")).balance)

    async def twice() -> None:
        async with probe.client.transaction() as tx:
            tx.update(Wallet.key("w1"), Wallet.balance.add(1))
            tx.check(Wallet, Wallet.key("w1"), Wallet.balance >= 0)

    await probe.fact("one_item_twice", twice())


async def index_reads(probe: Probe) -> None:
    """#106: filters and projections of attributes an index does not hold."""
    await probe.table(
        "idx",
        partition_key=("pk", "S"),
        sort_key=("sk", "S"),
        indexes=[
            IndexDefinition("lsi", "local", ("pk", "S"), ("lsk", "S"), projection=Projection.keys_only()),
            IndexDefinition("gsi", "global", ("gk", "S"), projection=Projection.keys_only()),
        ],
    )
    name = probe.name("idx")
    item = {"pk": "a", "sk": "1", "lsk": "x", "gk": "g", "secret": "s", "note": "o", "m": {"x": "s"}}
    await probe.call("put_item", "PutItem", table_name=name, item=item)
    query = {"table_name": name, "index_name": "lsi", "key_condition_expression": "pk = :p"}
    values = {":p": "a", ":s": "s"}
    for select in (None, "ALL_ATTRIBUTES", "COUNT"):
        params = {**query, "filter_expression": "secret = :s", "expression_attribute_values": values}
        if select:
            params["select"] = select
        await probe.fact(f"lsi_query_filter_{select or 'default'}", probe.call("query", "Query", **params), count)
    await probe.fact(
        "lsi_query_filter_order",
        probe.call(
            "query",
            "Query",
            **query,
            filter_expression="note = :o AND m.x = :s AND size(secret) > :n",
            expression_attribute_values={":p": "a", ":o": "o", ":s": "s", ":n": 0},
        ),
        count,
    )
    await probe.fact(
        "lsi_query_filter_with_held",
        probe.call(
            "query",
            "Query",
            **query,
            filter_expression="sk = :one AND secret = :s",
            expression_attribute_values={":p": "a", ":one": "1", ":s": "s"},
        ),
        count,
    )
    await probe.fact(
        "lsi_query_projection",
        probe.call(
            "query", "Query", **query, projection_expression="pk, secret", expression_attribute_values={":p": "a"}
        ),
        lambda output: output["items"],
    )
    await probe.fact(
        "gsi_query_projection",
        probe.call(
            "query",
            "Query",
            table_name=name,
            index_name="gsi",
            key_condition_expression="gk = :g",
            projection_expression="note, m.x, pk, secret",
            expression_attribute_values={":g": "g"},
        ),
        count,
    )
    for index, select, projection in (
        ("lsi", None, None),
        ("lsi", "ALL_ATTRIBUTES", None),
        ("lsi", None, "pk, secret"),
        ("gsi", None, None),
    ):
        params: dict[str, Any] = {
            "table_name": name,
            "index_name": index,
            "filter_expression": "secret = :s",
            "expression_attribute_values": {":s": "s"},
        }
        if select:
            params["select"] = select
        if projection:
            params["projection_expression"] = projection
        label = f"{index}_scan_filter_{select or projection or 'default'}".replace(", ", "_")
        await probe.fact(label, probe.call("scan", "Scan", **params), count)


async def expressions(probe: Probe) -> None:
    """#97: placeholders, long names, attribute_type with a path, the envelope of writes."""
    await probe.table("expr", partition_key=("pk", "S"), sort_key=("sk", "S"))
    name = probe.name("expr")
    key = {"pk": "a", "sk": "1"}
    for size in (255, 256):
        placeholder = "#" + "n" * (size - 1)
        await probe.fact(
            f"name_placeholder_{size}",
            probe.call(
                "get_item",
                "GetItem",
                table_name=name,
                key=key,
                projection_expression=placeholder,
                expression_attribute_names={placeholder: "x"},
            ),
        )
    for used in (True, False):
        await probe.fact(
            f"dash_placeholder_{'used' if used else 'unused'}",
            probe.call(
                "get_item",
                "GetItem",
                table_name=name,
                key=key,
                projection_expression="#a-b" if used else "pk",
                expression_attribute_names={"#a-b": "x"},
            ),
        )
    await probe.fact(
        "dash_placeholder_put",
        probe.call(
            "put_item",
            "PutItem",
            table_name=name,
            item=key,
            condition_expression="attribute_not_exists(#a-b)",
            expression_attribute_names={"#a-b": "x"},
        ),
    )
    for size in (65_535, 65_536):
        await probe.fact(
            f"item_name_{size}", probe.call("put_item", "PutItem", table_name=name, item={**key, "n" * size: "v"})
        )
        await probe.fact(
            f"placeholder_name_{size}",
            probe.call(
                "get_item",
                "GetItem",
                table_name=name,
                key=key,
                projection_expression="#n",
                expression_attribute_names={"#n": "n" * size},
            ),
        )
    await probe.fact(
        "map_key_65536",
        probe.call("put_item", "PutItem", table_name=name, item={**key, "m": {"n" * 65_536: "v"}}),
    )
    await probe.call("put_item", "PutItem", table_name=name, item={**key, "a": "x", "b": "S"})
    await probe.fact(
        "attribute_type_path_filter",
        probe.call(
            "query",
            "Query",
            table_name=name,
            key_condition_expression="pk = :p",
            filter_expression="attribute_type(a, b)",
            expression_attribute_values={":p": "a"},
        ),
        count,
    )
    await probe.fact(
        "attribute_type_path_update",
        probe.call(
            "update_item",
            "UpdateItem",
            table_name=name,
            key=key,
            update_expression="SET c = :c",
            condition_expression="attribute_type(a, b)",
            expression_attribute_values={":c": "c"},
        ),
    )
    await probe.fact(
        "reserved_word_put",
        probe.call("put_item", "PutItem", table_name=name, item=key, condition_expression="attribute_exists(status)"),
    )
    await probe.fact(
        "reserved_word_get",
        probe.call("get_item", "GetItem", table_name=name, key=key, projection_expression="status"),
    )
    missing = probe.name("missing")
    select = f'SELECT * FROM "{name}" WHERE pk = ? AND sk = ?'
    await probe.fact(
        "read_transaction_missing_table",
        probe.client.execute_transaction(
            [(select, ["a", "1"]), (f'SELECT * FROM "{missing}" WHERE pk = ? AND sk = ?', ["a", "1"])]
        ),
    )
    await probe.fact(
        "transact_get_missing_table",
        probe.call("transact_get_items", "TransactGetItems", transact_items=[{"table_name": missing, "key": key}]),
    )
    await probe.fact("get_missing_table", probe.call("get_item", "GetItem", table_name=missing, key=key))


async def sizes(probe: Probe) -> None:
    """#93: local index entries in the 400 KB limit, the 4 MB of transactions."""
    await probe.table(
        "lsi",
        partition_key=("pk", "S"),
        sort_key=("sk", "S"),
        indexes=[IndexDefinition("lsi", "local", ("pk", "S"), ("lsk", "S"), projection=Projection.all())],
    )
    await probe.table("big", partition_key=("pk", "S"), sort_key=("sk", "S"))
    lsi, big = probe.name("lsi"), probe.name("big")

    def sized(total: int, sk: str, **extra: str) -> dict[str, Any]:
        item: dict[str, Any] = {"pk": "b", "sk": sk, **extra}
        used = sum(len(name) + len(value) for name, value in item.items()) + len("filler")
        return {**item, "filler": "a" * (total - used)}

    for total in (204_800, 204_801):
        await probe.fact(
            f"local_index_put_{total}",
            probe.call("put_item", "PutItem", table_name=lsi, item=sized(total, f"all{total}", lsk="x")),
        )
    await probe.call("put_item", "PutItem", table_name=lsi, item=sized(150 * KB, "grow", lsk="x"))
    await probe.fact(
        "local_index_update",
        probe.call(
            "update_item",
            "UpdateItem",
            table_name=lsi,
            key={"pk": "b", "sk": "grow"},
            update_expression="SET more = :v",
            expression_attribute_values={":v": "m" * (60 * KB)},
        ),
    )
    await probe.fact(
        "local_index_transaction_put",
        probe.call(
            "transact_write_items",
            "TransactWriteItems",
            transact_items=[{"kind": "put", "table_name": lsi, "item": sized(210 * KB, "tx", lsk="x")}],
        ),
    )
    filler = "c" * (390 * KB)

    def updates(many: int, pk: str = "upd", condition: str | None = None) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for sk in range(many):
            operation: dict[str, Any] = {
                "kind": "update",
                "table_name": big,
                "key": {"pk": pk, "sk": f"{sk:02}"},
                "update_expression": "SET filler = :v",
                "expression_attribute_values": {":v": filler},
            }
            if condition is not None:
                operation["condition_expression"] = condition
                used = {":w": "w", ":ww": "ww", ":t": "S"}  # :t a type name, as long as :w
                extra = {name: value for name, value in used.items() if re.search(rf"{name}\b", condition)}
                operation["expression_attribute_values"] = {":v": filler, **extra}
            operations.append(operation)
        return operations

    await probe.fact(
        "payload_updates_11", probe.call("transact_write_items", "TransactWriteItems", transact_items=updates(11))
    )
    await probe.fact(
        "payload_updates_longer_key",
        probe.call("transact_write_items", "TransactWriteItems", transact_items=updates(11, "updupd")),
    )
    puts = [
        {"kind": "put", "table_name": big, "item": {"pk": "upd", "sk": f"{sk:02}", "filler": filler}}
        for sk in range(11)
    ]
    await probe.fact("payload_puts_11", probe.call("transact_write_items", "TransactWriteItems", transact_items=puts))
    # How conditions, deletes and condition checks count (the payload size of the refusal says).
    for label, condition in (
        ("exists_pk", "attribute_exists(pk)"),
        ("exists_long", "attribute_exists(abcdefghij)"),
        ("exists_nested", "attribute_exists(ab.cd)"),
        ("compare", "note = :w"),
        ("compare_longer_value", "note = :ww"),
        ("compare_paths", "note = alt"),
        ("compare_size", "size(note) > :w"),
        ("not_equal", "note <> :w"),
        ("begins_with", "begins_with(note, :w)"),
        ("contains", "contains(note, :w)"),
        ("attribute_type", "attribute_type(note, :t)"),
        ("between", "note BETWEEN :w AND :w"),
        ("in", "note IN (:w, :w)"),
        ("and", "attribute_exists(pk) AND attribute_exists(pk)"),
        ("or", "attribute_exists(pk) OR attribute_exists(pk)"),
        ("not", "NOT attribute_exists(pk)"),
    ):
        await probe.fact(
            f"payload_updates_condition_{label}",
            probe.call("transact_write_items", "TransactWriteItems", transact_items=updates(11, condition=condition)),
        )
    extra_key = {"pk": "other", "sk": "00"}
    for label, extra in (
        ("delete", {"kind": "delete", "table_name": big, "key": extra_key}),
        (
            "check",
            {
                "kind": "condition_check",
                "table_name": big,
                "key": extra_key,
                "condition_expression": "attribute_not_exists(pk)",
            },
        ),
        (
            "put_with_condition",
            {"kind": "put", "table_name": big, "item": extra_key, "condition_expression": "attribute_not_exists(pk)"},
        ),
    ):
        await probe.fact(
            f"payload_updates_plus_{label}",
            probe.call("transact_write_items", "TransactWriteItems", transact_items=[*updates(11), extra]),
        )
    await probe.fact(
        "payload_updates_10", probe.call("transact_write_items", "TransactWriteItems", transact_items=updates(10))
    )
    for sk in range(11):
        await probe.call("put_item", "PutItem", table_name=big, item={"pk": "get", "sk": f"{sk:02}", "filler": filler})
    for many in (10, 11):
        gets = [{"table_name": big, "key": {"pk": "get", "sk": f"{sk:02}"}} for sk in range(many)]
        await probe.fact(
            f"transact_get_{many}",
            probe.call("transact_get_items", "TransactGetItems", transact_items=gets),
            size_message,
        )
    await probe.fact(
        "update_charge_over_400kb",
        probe.call(
            "update_item",
            "UpdateItem",
            table_name=big,
            key={"pk": "new", "sk": "00"},
            update_expression="SET filler = :v",
            expression_attribute_values={":v": "c" * 409_580},
        ),
    )


async def tables(probe: Probe) -> None:
    """#87: changes while a table or index is being created, and billing switches."""
    table = probe.client.table("creating")
    await table.create(partition_key=("pk", "S"), wait=False)
    await probe.fact("ttl_while_creating", table.enable_time_to_live("expires"))
    await probe.fact(
        "index_while_creating",
        table.add_index(IndexDefinition("by_a", "global", ("a", "S")), billing=dynamoxy.Billing(), wait=False),
    )
    await table.wait_until_active(timeout=WAIT)
    # Another name: emulators take the first index while the table is being created.
    await table.add_index(IndexDefinition("by_c", "global", ("c", "S")), wait=False)
    await probe.fact(
        "second_index_while_creating",
        table.add_index(IndexDefinition("by_b", "global", ("b", "S")), billing=dynamoxy.Billing(), wait=False),
    )
    await table.wait_until_active(timeout=WAIT)

    billed = await probe.table(
        "billing", partition_key=("pk", "S"), indexes=[IndexDefinition("gsi1", "global", ("gk", "S"))]
    )
    name = probe.name("billing")
    await probe.fact(
        "provisioned_without_index_capacity",
        probe.call(
            "update_table",
            "UpdateTable",
            table_name=name,
            billing_mode="PROVISIONED",
            provisioned_throughput=(1, 1),
        ),
    )
    await billed.wait_until_active(timeout=WAIT)
    await probe.fact("provisioned", billed.update_settings(billing=dynamoxy.Billing.provisioned(1, 1)))
    await probe.fact(
        "index_without_capacity_on_provisioned",
        probe.call(
            "update_table",
            "UpdateTable",
            table_name=name,
            attribute_definitions=[("gk2", "S")],
            global_secondary_index_updates=[
                {"create": {"index_name": "gsi2", "key_schema": ("gk2", None), "projection": ("KEYS_ONLY", [])}}
            ],
        ),
    )
    await billed.wait_until_active(timeout=WAIT)
    await probe.fact("on_demand_again", billed.update_settings(billing=dynamoxy.Billing()))
    described = await probe.call("describe_table", "DescribeTable", table_name=name)
    capacity = described["global_secondary_indexes"][0]["provisioned_throughput"]
    probe.note("index_capacity_on_demand", list(capacity) if capacity else [0, 0])
    await probe.fact(
        "index_capacity_on_on_demand",
        probe.call(
            "update_table",
            "UpdateTable",
            table_name=name,
            global_secondary_index_updates=[{"update": {"index_name": "gsi1", "provisioned_throughput": (1, 1)}}],
        ),
    )
    await billed.wait_until_active(timeout=WAIT)


async def partiql(probe: Probe) -> None:
    await probe.table("pq", partition_key=("pk", "S"), sort_key=("sk", "S"))
    name = probe.name("pq")
    insert = f"INSERT INTO \"{name}\" VALUE {{'pk': ?, 'sk': ?}}"
    for sk in ("1", "2", "3", "4"):
        await probe.client.execute(insert, "a", sk)
    await probe.fact("insert_existing", probe.client.execute(insert, "a", "1"))
    await probe.fact(
        "transaction_insert_existing", probe.client.execute_transaction([(insert, ["b", "1"]), (insert, ["a", "1"])])
    )
    await probe.fact(
        "batch_insert_existing",
        probe.client.batch_execute([(insert, ["c", "1"]), (insert, ["a", "1"])]),
        lambda rows: [row["error"][0] if row["error"] else None for row in rows],
    )
    await probe.fact(
        "select_page",
        probe.call(
            "execute_statement",
            "ExecuteStatement",
            statement=f'SELECT * FROM "{name}" WHERE pk = ?',
            parameters=["a"],
            consistent_read=True,
            limit=2,
        ),
        lambda output: {
            "items": len(output["items"]),
            "next": output["next_token"] is not None,
            "last": output["last_evaluated_key"],
        },
    )


async def table_settings(probe: Probe) -> None:
    table = await probe.table("ttl", partition_key=("pk", "S"))
    await table.enable_time_to_live("expires")
    await table.delete_table()
    await table.create(partition_key=("pk", "S"))
    probe.note("ttl_after_created_again", (await table.time_to_live()).status)
    await probe.table(
        "icm",
        partition_key=("pk", "S"),
        sort_key=("sk", "S"),
        indexes=[IndexDefinition("lsi", "local", ("pk", "S"), ("lsk", "S"), projection=Projection.keys_only())],
    )
    name = probe.name("icm")

    def metrics(entries: Any) -> Any:
        return [entry["size_estimate_range_gb"] for entry in entries]

    put = await probe.call(
        "put_item",
        "PutItem",
        table_name=name,
        item={"pk": "c", "sk": "1", "lsk": "x"},
        return_item_collection_metrics=True,
    )
    probe.note("metrics_put", metrics([put["item_collection_metrics"]]) if put["item_collection_metrics"] else None)
    batch = await probe.call(
        "batch_write_item",
        "BatchWriteItem",
        request_items={name: [("put", {"pk": "c", "sk": "2", "lsk": "y"})]},
        return_item_collection_metrics=True,
    )
    probe.note("metrics_batch", metrics(batch["item_collection_metrics"].get(name, [])))
    transaction = await probe.call(
        "transact_write_items",
        "TransactWriteItems",
        transact_items=[{"kind": "put", "table_name": name, "item": {"pk": "c", "sk": "3", "lsk": "z"}}],
        return_item_collection_metrics=True,
    )
    probe.note("metrics_transaction", metrics(transaction["item_collection_metrics"].get(name, [])))
    cost = await probe.call(
        "put_item",
        "PutItem",
        table_name=name,
        item={"pk": "d", "sk": "1", "lsk": "x", "filler": "f" * 1500},
        return_consumed_capacity="INDEXES",
    )
    probe.note("capacity_put", cost["consumed_capacity"]["capacity_units"] if cost["consumed_capacity"] else None)
    read = await probe.call(
        "get_item",
        "GetItem",
        table_name=name,
        key={"pk": "d", "sk": "1"},
        consistent_read=True,
        return_consumed_capacity="TOTAL",
    )
    probe.note("capacity_get", read["consumed_capacity"]["capacity_units"] if read["consumed_capacity"] else None)


SCENARIOS: dict[str, Callable[[Probe], Awaitable[None]]] = {
    "versions": versions,
    "constraints": constraints,
    "transactions": transactions,
    "index_reads": index_reads,
    "expressions": expressions,
    "sizes": sizes,
    "tables": tables,
    "partiql": partiql,
    "table_settings": table_settings,
}


# ---- commands --------------------------------------------------------------------------------


def make_client(target: str, prefix: str, args: argparse.Namespace) -> dynamoxy.Client:
    if target == "memory":
        return testing.memory_client(table_prefix=prefix)
    if target == "aws":
        # A new on-demand table throttles bursts of large writes (eleven 390 KB items at once):
        # more attempts, longer backoff.
        retry = dynamoxy.RetryPolicy(max_attempts=12, base_delay=0.5, max_delay=20)
        return dynamoxy.Client(region=args.region, table_prefix=prefix, retry=retry)
    endpoint = args.endpoint or DEFAULT_ENDPOINTS.get(target)
    if endpoint is None:
        raise SystemExit(f"--endpoint is needed for {target}")
    return dynamoxy.Client(
        endpoint_url=endpoint,
        region=args.region or "us-east-1",
        credentials=dynamoxy.Credentials("test", "test"),
        table_prefix=prefix,
    )


async def delete_tables(client: dynamoxy.Client, prefix: str) -> list[str]:
    """Deletes the tables named with ``prefix``, waiting for each to be active first.

    A table that cannot be deleted is reported on stderr and left: ``cleanup`` deletes it later.
    """
    deleted = []
    for name in await client.list_tables():
        if not name.startswith(prefix):
            continue
        table = client.table(name.removeprefix(client.table_prefix))
        try:
            status = (await table.describe()).status
            if status == "DELETING":
                continue  # an interrupted run's deletion, still going
            if status != "ACTIVE":
                await table.wait_until_active(timeout=900)
            await table.delete_table(missing_ok=True)
        except dynamoxy.ResourceNotFoundError:
            continue  # deleted meanwhile
        except dynamoxy.DynamoxyError as error:
            print(f"could not delete {name} ({error}): run cleanup later", file=sys.stderr)
        else:
            deleted.append(name)
    return deleted


async def run(args: argparse.Namespace) -> dict[str, Any]:
    prefix = f"{PREFIX}{secrets.token_hex(3)}-"
    client = make_client(args.target, prefix, args)
    probe = Probe(client, prefix)
    selected = args.only or list(SCENARIOS)
    try:
        for scenario in selected:
            probe.scenario = scenario
            probe.facts[scenario] = {}
            print(f"{scenario} ...", file=sys.stderr, flush=True)
            try:
                await SCENARIOS[scenario](probe)
            except Exception as error:  # noqa: BLE001 - a scenario that breaks is a fact too
                probe.facts[scenario]["scenario_failed"] = probe.error(error)
    finally:
        try:
            await delete_tables(client, prefix)
        finally:
            await client.close()
    return {"target": args.target, "dynamoxy": dynamoxy.__version__, "facts": probe.facts}


def compare(reference: dict[str, Any], other: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The facts both runs hold that differ (``scenario.fact`` and both values), and the facts only
    one of them holds."""
    differing, missing = [], []
    left, right = reference["facts"], other["facts"]
    for scenario in sorted(set(left) | set(right)):
        facts_left, facts_right = left.get(scenario, {}), right.get(scenario, {})
        for name in sorted(set(facts_left) | set(facts_right)):
            if name not in facts_left or name not in facts_right:
                holder = reference["target"] if name in facts_left else other["target"]
                missing.append(f"{scenario}.{name} (only in {holder})")
            elif facts_left[name] != facts_right[name]:
                differing.append(f"{scenario}.{name}:")
                differing.append(f"  {reference['target']}: {json.dumps(facts_left[name])}")
                differing.append(f"  {other['target']}: {json.dumps(facts_right[name])}")
    return differing, missing


def iam_policy(account: str) -> dict[str, Any]:
    """The least a run on AWS needs: everything on the probe's tables, and listing tables."""
    tables = f"arn:aws:dynamodb:*:{account}:table/{PREFIX}*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "dynamodb:*", "Resource": [tables, f"{tables}/index/*"]},
            {"Effect": "Allow", "Action": "dynamodb:ListTables", "Resource": "*"},
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="probe one target and write its facts")
    run_parser.add_argument("--target", required=True, choices=["memory", "ministack", "localstack", "aws"])
    run_parser.add_argument("--endpoint", help="the emulator's URL")
    run_parser.add_argument("--region", help="the AWS region (aws), us-east-1 for emulators")
    run_parser.add_argument("--only", nargs="*", choices=list(SCENARIOS), help="scenarios to run (all by default)")
    run_parser.add_argument("--out", type=Path, help="where to write the facts (probe-<target>.json)")
    compare_parser = commands.add_parser("compare", help="list the facts that differ between two runs")
    compare_parser.add_argument("reference", type=Path)
    compare_parser.add_argument("other", type=Path)
    policy_parser = commands.add_parser("iam-policy", help="print the IAM policy a run on AWS needs")
    policy_parser.add_argument("--account", default="<account-id>")
    cleanup_parser = commands.add_parser("cleanup", help="delete the tables interrupted runs left")
    cleanup_parser.add_argument("--target", required=True, choices=["ministack", "localstack", "aws"])
    cleanup_parser.add_argument("--endpoint")
    cleanup_parser.add_argument("--region")
    args = parser.parse_args(argv)

    if args.command == "run":
        result = asyncio.run(run(args))
        out = args.out or Path(f"probe-{args.target}.json")
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out}")
    elif args.command == "compare":
        differing, missing = compare(json.loads(args.reference.read_text()), json.loads(args.other.read_text()))
        print("\n".join(differing) if differing else "no differences")
        if missing:
            print("\nFacts only one run holds:\n" + "\n".join(missing))
        print(f"{len(differing) // 3} differing facts, {len(missing)} in one run only", file=sys.stderr)
    elif args.command == "iam-policy":
        print(json.dumps(iam_policy(args.account), indent=2))
    else:

        async def cleanup() -> list[str]:
            client = make_client(args.target, "", args)
            try:
                return await delete_tables(client, PREFIX)
            finally:
                await client.close()

        for name in asyncio.run(cleanup()):
            print(f"deleted {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
