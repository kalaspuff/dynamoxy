# 0001: Rust core for protocol, timing and I/O; Python for models and application objects

Status: Accepted

## Context

dynamoxy must be fast and robust (retries, timeouts, batching with unprocessed items, transaction
handling, exact numbers, a faithful local DynamoDB for tests) yet feel native to Python developers,
whose data are typed classes (native or pydantic), whose hooks are `async def` functions and who
instrument their services with OpenTelemetry.

## Decision

* `dynamoxy-core` owns everything that needs no Python objects: values and numbers, the
  expression language, the DynamoDB transport (AWS SDK adapter), retries, timeouts, managed
  batches, events, metrics, and the in-memory DynamoDB. It has no PyO3 dependency.
* `dynamoxy-py` is a thin binding: conversions, the asyncio bridge, the exception tree.
* Python owns modeling (descriptors, pydantic, codecs, validation), repositories, transactions
  planning, unique guards, hooks, middleware, logging and OpenTelemetry.

## Consequences

* The Python API stays idiomatic and fully typed; user code never touches Rust types.
* Every core feature needs a Python surface (`_core.pyi`, wrappers, docs);
  `scripts/check_consistency.py` verifies the stub against the native module.
* The core never calls into Python from its threads: events are pulled by Python.
* Domain logic that needs model knowledge (key templates, guards, versions) lives in Python, so
  the Rust core stays a general-purpose DynamoDB client.
