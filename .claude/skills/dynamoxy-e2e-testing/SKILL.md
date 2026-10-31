---
name: dynamoxy-e2e-testing
description: Run and debug dynamoxy's end-to-end tests against MiniStack (the pip-installed AWS emulator) in Python and Rust, and resolve differences between the in-memory backend and the emulator. Use when emulator tests fail, when adding e2e or parity tests, or when checking protocol behaviour.
---

# End-to-end testing with MiniStack

1. **Python**: `uv run pytest` starts MiniStack automatically (session fixture in
   `tests/conftest.py`, free port, health check at `/_ministack/health`). Run only emulator tests
   with `uv run pytest -m e2e`, skip them with `-m "not e2e"`.
2. **Reuse a running emulator**: `ministack` (with `GATEWAY_PORT=4599`) in another terminal, then
   `DYNAMOXY_TEST_ENDPOINT_URL=http://127.0.0.1:4599 uv run pytest -m e2e`.
3. **Rust parity**: `DYNAMOXY_TEST_ENDPOINT_URL=http://127.0.0.1:4599 cargo test -p dynamoxy-core
   --test emulator` (without the variable, the scenarios run on the memory backend only).
4. **When a test fails only on the emulator**:
   - Read the DynamoDB documentation for the operation and the answers measured against DynamoDB
     in `docs/parity/README.md`. Usually the emulator is right and the memory backend must be
     fixed (with a rule test in `backend/memory/tests.rs`); MiniStack accepts much of what
     DynamoDB refuses (index filters, size limits, table updates), so tests of those refusals
     assert on the memory backend only.
   - If the documentation shows the emulator deviates, tolerate it narrowly with a comment naming
     MiniStack (`if backend == "memory": ...`), or normalize in the AWS adapter when real DynamoDB
     would agree (example: cancellation reason codes in `backend/aws/errors.rs`).
5. **Interop** with boto3 lives in `tests/e2e/test_boto3_interop.py`; keep wire-format checks there.
6. Kill a stray emulator by PID, never with `pkill -f`.
7. **LocalStack** (DynamoDB Local inside, closer to DynamoDB than MiniStack) runs without Docker:
   `docs/parity/README.md` shows how. Point `DYNAMOXY_TEST_ENDPOINT_URL` at it
   (`http://127.0.0.1:4566`) to run the e2e tests against it; tests take the `target` fixture
   (`"memory"`, `"ministack"`, `"localstack"`) to skip an emulator's gaps, with a comment.
8. **Comparing backends**: `scripts/probe_backends.py run --target <memory|ministack|localstack|aws>`
   writes dynamoxy's behaviour on a target as facts, and `compare` lists what differs from
   another run (DynamoDB's, when a maintainer has run it). Add a scenario there for a new
   question; `docs/parity/README.md` shows the commands.
