# 0003: An in-memory DynamoDB in the core, held to parity with the emulator

Status: Accepted

## Context

Applications need fast, deterministic tests without AWS accounts or Docker, and dynamoxy itself
needs to test retries, throttling and unprocessed items, which real services rarely produce on
demand. Mocks of a client library drift from DynamoDB's behaviour.

## Decision

* `backend/memory` implements every `Backend` operation with DynamoDB's semantics: item and key
  validation, the expression language (shared with expression validation), conditional writes,
  `ReturnValues`, indexes with projections, 1 MB pages and `Limit`, batches, transactions with
  cancellation reasons and idempotency tokens, TTL, a PartiQL subset, table administration,
  backups, consumed capacity and DynamoDB's error codes and messages.
* It supports fault injection (errors, latency, unprocessed batch items) for tests.
* Parity is enforced: Rust scenarios (`tests/emulator.rs`) and every Python integration test run
  against both the in-memory backend and MiniStack. A test that fails only on the emulator is
  investigated as a protocol detail and modeled in the memory backend.

## Consequences

* `dynamoxy.testing.memory_client()` gives users a realistic DynamoDB in tests.
* The memory backend is substantial code that must follow DynamoDB's behaviour; the parity suite
  keeps it honest.
* The same expression evaluator serves transactions (refreshing instances locally,
  `_core._apply_update`).
