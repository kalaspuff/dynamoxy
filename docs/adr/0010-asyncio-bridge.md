# 0010: An own asyncio bridge: results delivered through per-loop ports

Status: Accepted

## Context

Every native call returns an awaitable. dynamoxy 0.1 created them with `pyo3-async-runtimes`,
which spawns two Tokio tasks per call, hands the finished result to a blocking-pool thread,
and that thread takes the GIL to call `loop.call_soon_threadsafe`; the binding also took the
GIL on a Tokio worker to convert the output. Each call paid several thread handoffs and GIL
acquisitions from foreign threads (which wait for the loop to release the GIL, up to the switch
interval when it is busy). An in-memory `GetItem` took 50-80 µs, almost all of it in the
bridge, and every AWS call spent comparable CPU time on handoffs.

## Decision

* `crates/dynamoxy-py/src/runtime.rs` implements the bridge. Runtime threads never attach to
  Python: a call's Rust future produces a `Deferred` (a closure that builds the Python result or
  exception), which is queued on the event loop's `Port`.
* A `Port` per event loop owns the completion queue and a socket pair whose read end the loop
  watches (`loop.add_reader`). The first completion after a drain writes one byte; the loop's
  thread then builds the results of every queued call and resolves their `asyncio.Future`s.
  Loops without `add_reader` (the Windows proactor loop) are woken with
  `call_soon_threadsafe` from a blocking thread instead.
* Calls of in-memory clients are polled once on the calling thread (without the GIL) before
  they are handed to the runtime. When they finish there (no injected latency), the awaitable
  is a `Ready` object that yields to the event loop once, like real I/O, and then returns the
  result.
* Cancelling the asyncio future aborts the Tokio task. A panic in a task becomes a
  `DynamoxyError` on its future.
* A port holds the futures of its pending calls, which keeps tasks that nobody references alive
  while they wait (as a transport does for real I/O). Creating a port releases the pending
  calls of ports whose loop is closed, as asyncio drops a closed loop's callbacks; otherwise a
  loop closed with pending calls would never be freed.

## Consequences

* An in-memory call costs a few microseconds instead of 50-80 µs; AWS calls save the handoffs
  and the GIL contention. `pyo3-async-runtimes` is no longer a dependency.
* Results are converted on the loop's thread, which holds the GIL anyway.
* Awaitables of in-memory calls are not always `asyncio.Future`s (they are awaitables, as the
  stubs declare). Every call, in memory or not, still yields to the event loop at least once.
* The bridge holds weak references to loops and ports; it relies on loops supporting weak
  references (as `LoopLocal` in the Python layer already does), which the standard loops and
  uvloop do.
