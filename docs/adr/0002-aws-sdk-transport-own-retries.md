# 0002: The AWS SDK for Rust as transport, with dynamoxy's own retries

Status: Accepted

## Context

Talking to DynamoDB needs SigV4 signing, the credential chain (environment, profiles, SSO, web
identity, container and instance metadata), endpoint resolution, HTTP connection pooling and the
JSON protocol. The SDK provides all of that, maintained by AWS. Its retry strategy, however, is
opaque to callers: we could not count attempts and throttles per call, emit events per retry,
apply per-attempt timeouts or classify transaction cancellations caused only by conflicts.

## Decision

* Use `aws-sdk-dynamodb` and `aws-config` behind the `Backend` trait (`backend/aws`). SDK types
  never leave that module; all SDK errors are classified in `backend/aws/errors.rs`.
* Disable the SDK's retries. `dynamoxy-core::client` retries with exponential backoff and full
  jitter, a timeout per attempt, and events and metrics per attempt.

## Consequences

* `OperationStats` (attempts, throttles, duration) are exact, and `request.retry`,
  `request.throttled` and `request.gave_up` events are possible.
* We own the retry classification and must keep it aligned with DynamoDB's error codes.
* SDK upgrades are routine dependency updates; the binding and Python API do not change.
