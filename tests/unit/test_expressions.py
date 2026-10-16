"""The expression builder: placeholders, rendering, and agreement with DynamoDB's grammar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import dynamoxy
from dynamoxy import Attr, F, Model, _core, field, partition_key
from dynamoxy.expressions import Condition, ExpressionBuilder, UpdateAction, as_condition, attribute_roots, describe
from tests.models import Event, Status, User


class Timer(Model, table="timers"):
    timer_id: Attr[str] = partition_key()
    spent: Attr[timedelta] = field(default=timedelta(0))


def compile_condition(condition: Condition) -> tuple[str, dict[str, str], dict[str, Any]]:
    builder = ExpressionBuilder()
    text = builder.condition(condition)
    assert text is not None
    return text, builder.names, builder.values


def check(**expressions: Any) -> None:
    """Validates expressions with the Rust parser (what the in-memory backend and DynamoDB enforce)."""
    _core._check_expressions(**expressions)


def test_names_and_values_become_placeholders() -> None:
    text, names, values = compile_condition((F("status") == "active") & (F("age") >= 18) & (F("status") != "x"))
    assert text == "(#a0 = :v0) AND (#a1 >= :v1) AND (#a0 <> :v2)"
    assert names == {"#a0": "status", "#a1": "age"}
    assert values == {":v0": "active", ":v1": 18, ":v2": "x"}
    check(condition=text, names=names, values=values)


def test_paths_with_map_members_and_list_indexes() -> None:
    builder = ExpressionBuilder()
    assert builder.path(F("address")["city"]) == "#a0.#a1"
    assert builder.path(F("items")[2]["sku"]) == "#a2[2].#a3"
    assert str(F("items")[2]["sku"]) == "items[2].sku"
    with pytest.raises(ValueError, match="non-negative"):
        F("items")[-1]
    with pytest.raises(ValueError, match="starts with an attribute name"):
        dynamoxy.Path()


def test_functions_between_in_and_size() -> None:
    condition = (
        F("a").exists()
        & F("b").not_exists()
        & F("c").begins_with("x")
        & F("d").contains("y")
        & F("e").is_type("SS")
        & F("f").between(1, 5)
        & F("g").is_in([1, 2, 3])
        & (F("h").size() > 2)
    )
    text, names, values = compile_condition(condition)
    assert "attribute_exists(#a0)" in text
    assert "#a5 BETWEEN :v3 AND :v4" in text
    assert "#a6 IN (:v5, :v6, :v7)" in text
    assert "size(#a7) > :v8" in text
    check(condition=text, names=names, values=values)


def test_or_not_and_nesting() -> None:
    condition = (F("a") == 1) | ~((F("b") == 2) & (F("c") == 3))
    text, names, values = compile_condition(condition)
    assert text == "(#a0 = :v0) OR (NOT ((#a1 = :v1) AND (#a2 = :v2)))"
    check(condition=text, names=names, values=values)


def test_conditions_refuse_to_be_booleans() -> None:
    with pytest.raises(TypeError, match="cannot be used as a boolean"):
        bool(F("a") == 1)
    with pytest.raises(TypeError, match="expected a dynamoxy condition"):
        as_condition(True)
    with pytest.raises(ValueError, match="at least one"):
        dynamoxy.and_()
    assert isinstance(dynamoxy.or_(F("a") == 1), Condition)
    assert isinstance(dynamoxy.not_(F("a") == 1), Condition)


def test_raw_conditions_are_renamed_to_avoid_clashes() -> None:
    raw = dynamoxy.condition("#s = :v AND #n > :v", names={"#s": "status", "#n": "n"}, values={":v": 1})
    text, names, values = compile_condition((F("status") == "x") & raw)
    assert names == {"#a0": "status", "#a1": "n"}
    assert values == {":v0": "x", ":v1": 1, ":v2": 1}
    check(condition=text, names=names, values=values)
    with pytest.raises(ValueError, match="does not define"):
        compile_condition(dynamoxy.condition("#x = :y"))


def test_update_actions_are_grouped_by_section() -> None:
    builder = ExpressionBuilder()
    update = builder.update(
        [
            F("visits").add(1),
            F("name").set("Ada"),
            F("score").set(F("score") + 10),
            F("old").remove(),
            F("tags").delete({"x"}),
            F("history").append(["login"]),
            F("first_seen").set_if_not_exists(5),
            F("counter").set(F("counter").if_not_exists(0) - 1),
            F("log").append_or_create(["x"]),
            F("queue").prepend([0]),
            F("n").increment(2),
        ][:10]
    )
    assert update is not None
    assert update.startswith("SET ")
    assert " REMOVE " in update
    assert " ADD " in update
    assert " DELETE " in update
    check(update=update, names=builder.names, values=builder.values)


def test_overlapping_update_paths_are_rejected_early() -> None:
    builder = ExpressionBuilder()
    with pytest.raises(ValueError, match="overlapping paths"):
        builder.update([F("a").set(1), F("a")["b"].set(2)])
    with pytest.raises(ValueError, match="overlapping paths"):
        builder.update([F("a").set(1), F("a").remove()])


def test_increment_and_decrement_are_add_actions() -> None:
    builder = ExpressionBuilder()
    assert builder.update([F("n").decrement(3)]) == "ADD #a0 :v0"
    assert builder.values == {":v0": -3}


def test_none_means_no_attribute_for_model_fields() -> None:
    builder = ExpressionBuilder()
    assert builder.update([User.age.set(None)]) == "REMOVE #a0", "None is not stored, as with put() and save()"
    # A NULL that another writer stored reads as None too.
    text, names, values = compile_condition(User.age == None)  # noqa: E711
    assert (text, names, values) == (
        "(attribute_not_exists(#a0)) OR (attribute_type(#a0, :v0))",
        {"#a0": "age"},
        {":v0": "NULL"},
    )
    text, names, values = compile_condition(User.age != None)  # noqa: E711
    assert text == "(attribute_exists(#a0)) AND (NOT (attribute_type(#a0, :v0)))"
    check(condition=text, names=names, values=values)
    with pytest.raises(ValueError, match="would store nothing"):
        User.age.set_if_not_exists(None)
    # Untyped paths keep DynamoDB's NULL.
    builder = ExpressionBuilder()
    assert builder.update([F("x").set(None)]) == "SET #a0 = :v0"
    assert builder.values == {":v0": None}
    assert compile_condition(F("x") == None)[0] == "#a0 = :v0"  # noqa: E711


def test_increments_of_model_fields_check_their_amount() -> None:
    builder = ExpressionBuilder()
    assert builder.update([User.visits.increment(2), Timer.spent.decrement(timedelta(seconds=1.5))]) == (
        "ADD #a0 :v0, #a1 :v1"
    )
    assert builder.values == {":v0": 2, ":v1": Decimal("-1.5")}
    with pytest.raises(dynamoxy.ValidationError, match="expected int"):
        User.visits.increment(1.5)
    with pytest.raises(TypeError, match="need a number"):
        User.name.increment()
    with pytest.raises(TypeError, match="take a number"):
        User.visits.increment(User.visits)  # type: ignore[arg-type]
    builder = ExpressionBuilder()
    assert builder.update([Timer.spent.set(Timer.spent + timedelta(seconds=2))]) == "SET #a0 = #a0 + :v0"
    assert builder.values == {":v0": Decimal("2.000000")}, "arithmetic encodes with the field's codec"
    with pytest.raises(dynamoxy.ValidationError, match="expected int"):
        User.visits + 0.5


def test_attribute_roots_of_conditions() -> None:
    condition = (
        (F("a")["b"] == 1) & ~F("c").exists()
        | F("d").size().between(1, 2)
        | (F("e") == F("f"))
        | F("g").is_in([F("h")])
    )
    assert attribute_roots(condition) == {"a", "c", "d", "e", "f", "g", "h"}
    assert attribute_roots(User.tags.contains("x") & User.email.begins_with("a")) == {"tags", "email"}
    raw = dynamoxy.condition("#x = :y", names={"#x": "x"}, values={":y": 1})
    assert attribute_roots(raw & (F("z") == 1)) is None, "hand-written conditions hide their attributes"


def test_projection_expressions() -> None:
    builder = ExpressionBuilder()
    assert builder.projection(["a", F("b")["c"], F("l")[0]]) == "#a0, #a1.#a2, #a3[0]"
    assert builder.projection([]) is None


def test_model_paths_encode_values_with_their_codec() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    _, _, values = compile_condition(User.created_at > moment)
    assert values == {":v0": "2026-01-02T03:04:05.000000+00:00"}
    _, _, values = compile_condition(User.status == Status.SUSPENDED)
    assert values == {":v0": "suspended"}
    _, _, values = compile_condition(User.tags.contains("admin"))
    assert values == {":v0": "admin"}
    with pytest.raises(dynamoxy.ValidationError, match="expected int"):
        User.age > "old"  # type: ignore[operator]  # noqa: B015


def test_describe_renders_attribute_names() -> None:
    assert describe((F("status") == "x") & F("n").exists()) == "(status = :v0) AND (attribute_exists(n))"
    assert describe(Event.kind.set("x")) == "kind = :v0"


def test_update_action_repr_and_literal() -> None:
    action = F("a").set(1)
    assert action.literal() == (True, 1)
    assert F("a").add(1).literal() == (False, None)
    assert "SET" in repr(action)
    assert isinstance(action, UpdateAction)


def test_messages_leave_values_out() -> None:
    # Values may be personal data, and messages end up in logs.
    with pytest.raises(TypeError, match="a condition cannot be used as a value") as caught:
        F("a") == (F("b") == "ada@example.com")  # noqa: B015
    assert "ada@example.com" not in str(caught.value)
    with pytest.raises(TypeError, match="expected a dynamoxy condition, got str") as caught:
        as_condition("ada@example.com")  # type: ignore[arg-type]
    assert "ada@example.com" not in str(caught.value)


# ---- property: every expression the builder produces is valid DynamoDB grammar ---------------

attribute_names = st.sampled_from(["a", "b", "status", "name", "with-dash", "1st", "select", "x.y"])
scalars = st.one_of(st.integers(-(10**9), 10**9), st.text(max_size=6), st.binary(max_size=4))
values = st.one_of(scalars, st.booleans(), st.none(), st.lists(st.integers(0, 9), max_size=3))
paths = st.builds(
    lambda name, index: F(name) if index is None else F(name)[index],
    attribute_names,
    st.one_of(st.none(), st.integers(0, 3)),
)


def _comparison(path: Any, operator: str, value: Any) -> Condition:
    return getattr(path, operator)(value)


leaves = st.one_of(
    st.builds(_comparison, paths, st.sampled_from(["__eq__", "__ne__"]), values),
    st.builds(_comparison, paths, st.sampled_from(["__lt__", "__le__", "__gt__", "__ge__"]), scalars),
    st.builds(lambda path, low: path.between(low, low + 10), paths, st.integers(-100, 100)),
    st.builds(lambda path, options: path.is_in(options), paths, st.lists(scalars, min_size=1, max_size=5)),
    st.builds(lambda path: path.exists(), paths),
    st.builds(lambda path: path.not_exists(), paths),
    st.builds(lambda path, prefix: path.begins_with(prefix), paths, st.text(max_size=4)),
    st.builds(lambda path, value: path.contains(value), paths, scalars),
    st.builds(lambda path, kind: path.is_type(kind), paths, st.sampled_from(["S", "N", "BOOL", "M", "L", "SS"])),
    st.builds(lambda path, size: path.size() >= size, paths, st.integers(0, 10)),
)
conditions = st.recursive(
    leaves,
    lambda children: st.one_of(
        st.builds(lambda left, right: left & right, children, children),
        st.builds(lambda left, right: left | right, children, children),
        st.builds(lambda inner: ~inner, children),
    ),
    max_leaves=12,
)


@settings(max_examples=300, deadline=None)
@given(conditions)
def test_generated_conditions_are_valid_grammar(condition: Condition) -> None:
    text, names, values = compile_condition(condition)
    check(condition=text, names=names, values=values)
    check(filter=text, names=names, values=values)


update_kinds = st.sampled_from(["set", "add", "remove", "delete", "append", "set_if_not_exists", "arithmetic"])


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.sampled_from(["a", "b", "c", "status", "x-y", "nested"]), update_kinds, min_size=1))
def test_generated_updates_are_valid_grammar(plan: dict[str, str]) -> None:
    actions: list[UpdateAction] = []
    for attribute, kind in plan.items():
        path = F(attribute)
        actions.append(
            {
                "set": lambda path=path: path.set("value"),
                "add": lambda path=path: path.add(1),
                "remove": lambda path=path: path.remove(),
                "delete": lambda path=path: path.delete({"x"}),
                "append": lambda path=path: path.append([1]),
                "set_if_not_exists": lambda path=path: path.set_if_not_exists(0),
                "arithmetic": lambda path=path: path.set(path + 1),
            }[kind]()
        )
    builder = ExpressionBuilder()
    update = builder.update(actions)
    check(update=update, names=builder.names, values=builder.values)
