//! Typed events. The core records them in a bounded buffer; the Python layer pulls them and
//! routes them to logging and hooks, so Rust threads never call into Python.
//!
//! When adding a kind: add a line to `event_kinds!` and the same member, at the same position,
//! to `EventKind` in `python/dynamoxy/events.py` (`scripts/check_consistency.py` compares them),
//! then run `uv run python scripts/generate_reference.py`.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use parking_lot::Mutex;
use tokio::sync::Notify;

/// Severity of an event (maps to a Python logging level).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    /// Detailed diagnostics.
    Debug,
    /// Normal milestones.
    Info,
    /// Something unexpected that was handled.
    Warning,
    /// A failure that needs attention.
    Error,
}

impl Level {
    /// Lowercase name.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Debug => "debug",
            Self::Info => "info",
            Self::Warning => "warning",
            Self::Error => "error",
        }
    }
}

macro_rules! event_kinds {
    ($($variant:ident => ($name:literal, $level:ident, $doc:literal)),+ $(,)?) => {
        /// Every kind of event the core can emit.
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        #[repr(u8)]
        pub enum EventKind {
            $(#[doc = $doc] $variant),+
        }

        impl EventKind {
            /// All kinds, in declaration order.
            pub const ALL: &'static [EventKind] = &[$(EventKind::$variant),+];

            /// Stable dotted name, e.g. `request.retry`.
            pub fn as_str(self) -> &'static str {
                match self { $(EventKind::$variant => $name),+ }
            }

            /// Default severity.
            pub fn default_level(self) -> Level {
                match self { $(EventKind::$variant => Level::$level),+ }
            }

            /// One-line description (also used for the generated reference docs).
            pub fn description(self) -> &'static str {
                match self { $(EventKind::$variant => $doc),+ }
            }

            /// Parses a dotted name.
            pub fn parse(name: &str) -> Option<Self> {
                match name { $($name => Some(EventKind::$variant),)+ _ => None }
            }
        }
    };
}

event_kinds! {
    RequestRetry => ("request.retry", Info, "A request failed with a retryable error and will be retried after a delay."),
    RequestThrottled => ("request.throttled", Warning, "DynamoDB throttled a request (ProvisionedThroughputExceeded, ThrottlingException, RequestLimitExceeded)."),
    RequestGaveUp => ("request.gave_up", Error, "A request still failed with a retryable error after the last attempt."),
    RequestSlow => ("request.slow", Warning, "A request took longer than slow_operation_threshold."),
    BatchUnprocessed => ("batch.unprocessed", Info, "A batch request returned unprocessed items or keys; they are sent again."),
    BatchIncomplete => ("batch.incomplete", Error, "Unprocessed batch items or keys remained after batch_max_rounds rounds."),
    TableCreated => ("table.created", Info, "A table was created."),
    TableUpdated => ("table.updated", Info, "A table's settings or indexes were updated."),
    TableDeleted => ("table.deleted", Info, "A table was deleted."),
}

/// One event. Fields carry ids, names and sizes - never item contents (they may hold personal data).
#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    /// What happened.
    pub kind: EventKind,
    /// Human readable summary.
    pub message: String,
    /// Structured context.
    pub fields: Vec<(&'static str, String)>,
}

impl Event {
    /// Creates an event without fields.
    pub fn new(kind: EventKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into(), fields: Vec::new() }
    }

    /// Adds a field.
    #[must_use]
    pub fn field(mut self, name: &'static str, value: impl ToString) -> Self {
        self.fields.push((name, value.to_string()));
        self
    }
}

/// Bounded event buffer. Emitting a disabled kind costs one atomic load.
#[derive(Debug)]
pub struct EventBus {
    enabled: AtomicU64,
    buffer: Mutex<VecDeque<Event>>,
    capacity: usize,
    dropped: AtomicU64,
    notify: Notify,
}

impl EventBus {
    /// A bus that keeps at most `capacity` events; every kind is disabled.
    pub fn new(capacity: usize) -> Self {
        const { assert!(EventKind::ALL.len() <= 64, "the enabled set is a 64-bit mask") };
        Self {
            enabled: AtomicU64::new(0),
            buffer: Mutex::new(VecDeque::new()),
            capacity,
            dropped: AtomicU64::new(0),
            notify: Notify::new(),
        }
    }

    /// Enables exactly `kinds` (the Python layer passes what logging and hooks want).
    pub fn set_enabled(&self, kinds: &[EventKind]) {
        let mask = kinds.iter().fold(0u64, |mask, kind| mask | 1 << *kind as u8);
        self.enabled.store(mask, Ordering::Relaxed);
    }

    /// Whether events of `kind` are recorded.
    pub fn is_enabled(&self, kind: EventKind) -> bool {
        self.enabled.load(Ordering::Relaxed) & (1 << kind as u8) != 0
    }

    /// Records an event; `build` only runs when the kind is enabled.
    pub fn emit(&self, kind: EventKind, build: impl FnOnce() -> Event) {
        if !self.is_enabled(kind) {
            return;
        }
        let event = build();
        {
            let mut buffer = self.buffer.lock();
            if buffer.len() >= self.capacity {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                return;
            }
            buffer.push_back(event);
        }
        self.notify.notify_waiters();
    }

    /// Takes up to `max` buffered events without waiting.
    pub fn drain(&self, max: usize) -> Vec<Event> {
        let mut buffer = self.buffer.lock();
        let count = max.min(buffer.len());
        buffer.drain(..count).collect()
    }

    /// Waits up to `timeout` until at least one event is buffered, without taking any.
    ///
    /// Returns whether events are buffered. The Python layer waits with this and then drains
    /// synchronously, so no batch of events is ever in flight between two consumers.
    pub async fn wait(&self, timeout: Duration) -> bool {
        let notified = self.notify.notified();
        tokio::pin!(notified);
        // Register before checking the buffer so that an event emitted in between wakes us.
        notified.as_mut().enable();
        if !self.buffer.lock().is_empty() {
            return true;
        }
        let _ = tokio::time::timeout(timeout, notified).await;
        !self.buffer.lock().is_empty()
    }

    /// Returns and resets the number of events dropped because the buffer was full.
    pub fn take_dropped(&self) -> u64 {
        self.dropped.swap(0, Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    #[test]
    fn names_are_unique_and_parse_back() {
        for kind in EventKind::ALL {
            assert_eq!(EventKind::parse(kind.as_str()), Some(*kind));
        }
    }

    #[test]
    fn disabled_kinds_are_not_built_and_overflow_is_counted() {
        let bus = EventBus::new(1);
        bus.emit(EventKind::RequestRetry, || unreachable!("disabled kinds are not built"));
        bus.set_enabled(&[EventKind::RequestRetry]);
        bus.emit(EventKind::RequestRetry, || Event::new(EventKind::RequestRetry, "one"));
        bus.emit(EventKind::RequestRetry, || Event::new(EventKind::RequestRetry, "two"));
        assert_eq!(bus.drain(10).len(), 1);
        assert_eq!(bus.take_dropped(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn wait_returns_when_events_are_buffered_or_times_out() {
        let bus = Arc::new(EventBus::new(10));
        bus.set_enabled(EventKind::ALL);
        assert!(!bus.wait(Duration::from_secs(1)).await);
        let emitter = Arc::clone(&bus);
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(100)).await;
            emitter.emit(EventKind::TableCreated, || Event::new(EventKind::TableCreated, "created"));
        });
        assert!(bus.wait(Duration::from_secs(5)).await);
        assert!(bus.wait(Duration::from_secs(5)).await, "waiting takes nothing");
        assert_eq!(bus.drain(10).len(), 1);
    }
}
