---
name: dynamoxy-add-event-kind
description: Add a new event kind to dynamoxy (emitted by the Rust core or by the Python layer), keeping the Rust and Python enums, levels and the generated events reference in sync. Use when a task needs a new event, log line category or hook point for observability.
---

# Adding an event kind

1. **Choose the emitter.** Core behaviour (requests, retries, batches, tables) → Rust. Python
   behaviour (client lifecycle, operations, hooks) → Python.
2. **Rust kinds**: add a line to `event_kinds!` in `crates/dynamoxy-core/src/events.rs` with the
   dotted name, default level and description. Emit with
   `events.emit(EventKind::X, || Event::new(EventKind::X, message).field("name", value))` (the
   closure only runs when the kind is enabled). Add a Rust test.
3. **Python enum**: add the member to `EventKind` in `python/dynamoxy/events.py` at the same
   position as in Rust (core kinds first). Python-only kinds go after the core kinds and into
   `PYTHON_EVENT_KINDS` (level, description); emit with `client.events.emit(kind, message, fields)`
   guarded by `events.wants(kind)` if building fields is costly.
4. **Fields**: ids, names, counts, codes and durations only. Never item contents or key values.
5. **Test** that the event is emitted (`dynamoxy.testing.capture_events`).
6. Run `uv run python scripts/generate_reference.py` and `make check`.
