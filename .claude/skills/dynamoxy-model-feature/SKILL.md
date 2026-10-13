---
name: dynamoxy-model-feature
description: Change how dynamoxy models are declared, validated, encoded or persisted (codecs, field options, templates, indexes, unique guards, versions, hooks, pydantic support, repository write planning). Use when a task touches models.py, attributes.py, schema.py, codecs.py, _pydantic.py, repository.py, constraints.py or transactions.py.
---

# Changing model behaviour

1. **Find the stage** of the pipeline (`docs/architecture.md`, "A model write"):
   declaration (`attributes.py`, `models.py`, `_pydantic.py`) → schema (`schema.py`: `FieldSpec`,
   `ModelSchema` encode/decode/validate, key templates, indexes) → codecs (`codecs.py`) → write
   planning (`repository.py` `_prepare`, `_plan_put`, `_plan_update`, `_plan_delete`) → guards
   (`constraints.py`) → failure mapping (`Repository.explain_failure`) → transactions
   (`transactions.py` reuses the plans).
2. **Keep native and pydantic models equal**: a field feature needs a `field()` option and an
   `Annotated` marker; test both (`tests/unit/test_models.py`, `tests/unit/test_pydantic.py`).
3. **Keep repository and transaction paths equal**: change `_plan_*`, never only the repository
   method; test in `tests/integration/test_repository.py` and `test_transactions.py`.
4. **Stored formats are API** (ADR 0008): a change to what is stored needs backward-compatible
   reading, an ADR and a changelog note.
5. **Typing**: check `uv run pyrefly check` on `tests/models.py`; field specifiers must stay
   functions; constructor typing follows `dataclass_transform` rules (`init=False` overloads).
6. Update `docs/guides/modeling.md` (or the relevant guide), `AGENT_GUIDE.md`, then `make check`.
