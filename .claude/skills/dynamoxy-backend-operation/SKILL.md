---
name: dynamoxy-backend-operation
description: Add or change a DynamoDB operation in dynamoxy end to end - Backend trait, AWS SDK adapter, in-memory DynamoDB, core client, PyO3 binding, stub and Python wrapper - with parity tests on both backends. Use when supporting a new DynamoDB API call or parameter.
---

# Adding a backend operation

1. **Types**: input/output structs in `crates/dynamoxy-core/src/types/ops.rs` (snake_case,
   mirroring the DynamoDB API; dynamoxy types only).
2. **Port**: a method on `Backend` in `crates/dynamoxy-core/src/backend/mod.rs`.
3. **AWS adapter** (`backend/aws/mod.rs`): build the SDK request with conversions in
   `backend/aws/convert.rs`; map errors in `backend/aws/errors.rs` (codes, retry classification,
   extra details). SDK types never leave `backend/aws`.
4. **Memory backend** (`backend/memory/`): implement DynamoDB's semantics: validation and error
   messages, limits, consumed capacity, `ReturnValues`. Add rule tests to
   `backend/memory/tests.rs`. Check the DynamoDB API reference for edge cases.
5. **Core client** (`client.rs`): a method that validates, calls through `self.call(...)`
   (retries, timeouts, events, metrics) and returns `Call<Output>`; add the `Operation` to
   `metrics.rs`.
6. **Parity**: a scenario in `crates/dynamoxy-core/tests/emulator.rs` (runs on memory and, with
   `DYNAMOXY_TEST_ENDPOINT_URL`, on MiniStack).
7. **Binding**: a method on `PyCoreClient` (`crates/dynamoxy-py/src/client.rs`) returning
   `self.run(py, async move { finish(client.operation(input).await, convert) })`: `finish` builds
   a `runtime::Deferred`, so `convert` runs on the event loop's thread (never take the GIL in the
   future). Conversions in `convert.rs` (intern constant dict keys with `intern!`); the stub in
   `python/dynamoxy/_core.pyi`.
8. **Python**: a wrapper (repository, `Table`, or `Client`) calling `client.call(method,
   Operation, ...)`; tests in `tests/integration/` (both backends).
9. Docs (guide, AGENT_GUIDE, reference), then `make check`.
