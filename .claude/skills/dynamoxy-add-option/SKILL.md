---
name: dynamoxy-add-option
description: Add or change a configuration option of dynamoxy (Client parameter, RetryPolicy/Telemetry/LoggingConfig field, field specifier option) across Python, Rust config and docs. Use when a task introduces, renames or changes the default of any option.
---

# Adding an option

1. **Python surface.**
   - Client option: parameter in `Client.__init__` (`python/dynamoxy/client.py`) with a docstring
     entry in the class docstring `Args:`, validation that raises `ValueError` saying what is
     allowed, stored on `self`.
   - Policy/settings option: dataclass field (`RetryPolicy`, `Telemetry`, `LoggingConfig`) with
     validation in `__post_init__`.
   - Field option: `field()`/`partition_key()`/... in `python/dynamoxy/attributes.py` (both
     overloads), `FieldSpec` in `python/dynamoxy/schema.py`, and the pydantic marker if needed.
2. **Rust config** (only if the core uses it): field in `crates/dynamoxy-core/src/config.rs`
   (`#[serde(default)]`, `deny_unknown_fields` stays), validation in `ClientConfig::validate`,
   and the mapping in `Client.to_core()` / `RetryPolicy.to_core()`.
3. **Tests**: validation errors (`tests/unit/test_client.py`), the `to_core()` mapping, and the
   behaviour (memory backend; MiniStack if it affects the protocol).
4. **Docs**: a row in `docs/reference/configuration.md` (the consistency check fails otherwise),
   the relevant guide, the cheat sheet in `python/dynamoxy/AGENT_GUIDE.md` if applications should
   know it.
5. Run `make check`.
