# 0006: Fields are `Attr[T]` descriptors declared with `dataclass_transform`

Status: Accepted

## Context

Model fields must be values on instances (`user.name: str`), typed expression paths on the class
(`User.name.begins_with("A")`), validated on assignment, and understood by type checkers
(constructor signatures with required and optional fields).

## Decision

* Fields are annotated `Attr[T]`. `Attr` is a descriptor: `__get__` returns the value on
  instances and the typed path (`FieldPath[T]`) on the class; `__set__` validates.
* `Model` and `Document` use `typing.dataclass_transform` with field specifier *functions*
  (`field`, `partition_key`, `sort_key`, `created_at`, `updated_at`, `version`, `ttl`); automatic
  and templated fields declare `init=False` through overloads.
* Pydantic models are supported separately (`PydanticModel` with `Annotated` markers), with
  `dynamoxy.attr()` for typed paths.

## Consequences

* Type checkers verify constructors and expression operand types (`Attr[Decimal]` accepts `int`
  operands through overloads).
* Index declarations must not be class constructors listed as field specifiers (some type
  checkers would then treat them as fields), hence functions only.
* Field names cannot shadow the model API (`key`, `validate`, ...); `attribute_name=` keeps
  stored names.
