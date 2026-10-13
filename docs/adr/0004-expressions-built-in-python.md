# 0004: Expressions are built in Python and parsed, validated and evaluated in Rust

Status: Accepted

## Context

Typed model attributes (`User.visits > 3`, `User.tags.add({"x"})`) must produce DynamoDB
expressions. Values must be encoded with the field's codec, which is Python code. The in-memory
backend needs a full parser and evaluator anyway.

## Decision

* Python builds expression trees from typed paths and renders them with placeholders
  (`#aN` for names, `:vN` for values) in `ExpressionBuilder`. Values are encoded by codecs and
  passed as expression attribute values; names always go through placeholders.
* Rust parses and validates expressions (`expr/`) for the in-memory backend and exposes the
  evaluator to Python where useful (`_apply_update`, `_check_expressions` for tests).

## Consequences

* User values never appear in expression strings (no injection, no reserved-word problems).
* The builder's output is checked against the Rust parser by property tests.
* `dynamoxy.condition()` remains an escape hatch for hand-written expressions.
