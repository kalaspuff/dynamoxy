//! Lock-free metrics per operation, read as a snapshot on demand (`client.metrics()` in Python,
//! which can also export them through OpenTelemetry).

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::time::Duration;

/// Upper bounds of the latency histogram buckets, in milliseconds (the last bucket is open).
pub const LATENCY_BUCKETS_MS: [f64; 14] =
    [1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, f64::INFINITY];

macro_rules! operations {
    ($($variant:ident => $name:literal),+ $(,)?) => {
        /// Every operation the client performs (DynamoDB API names, plus managed batches).
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        #[repr(u8)]
        pub enum Operation {
            $(#[doc = concat!("`", $name, "`")] $variant),+
        }

        impl Operation {
            /// All operations, in declaration order.
            pub const ALL: &'static [Operation] = &[$(Operation::$variant),+];

            /// The DynamoDB API name (`"PutItem"`).
            pub fn as_str(self) -> &'static str {
                match self { $(Operation::$variant => $name),+ }
            }
        }
    };
}

operations! {
    GetItem => "GetItem",
    PutItem => "PutItem",
    UpdateItem => "UpdateItem",
    DeleteItem => "DeleteItem",
    Query => "Query",
    Scan => "Scan",
    BatchGetItem => "BatchGetItem",
    BatchWriteItem => "BatchWriteItem",
    TransactGetItems => "TransactGetItems",
    TransactWriteItems => "TransactWriteItems",
    ExecuteStatement => "ExecuteStatement",
    BatchExecuteStatement => "BatchExecuteStatement",
    ExecuteTransaction => "ExecuteTransaction",
    CreateTable => "CreateTable",
    DeleteTable => "DeleteTable",
    DescribeTable => "DescribeTable",
    UpdateTable => "UpdateTable",
    ListTables => "ListTables",
    UpdateTimeToLive => "UpdateTimeToLive",
    DescribeTimeToLive => "DescribeTimeToLive",
    TagResource => "TagResource",
    UntagResource => "UntagResource",
    ListTagsOfResource => "ListTagsOfResource",
    DescribeContinuousBackups => "DescribeContinuousBackups",
    UpdateContinuousBackups => "UpdateContinuousBackups",
    CreateBackup => "CreateBackup",
    DescribeBackup => "DescribeBackup",
    DeleteBackup => "DeleteBackup",
    ListBackups => "ListBackups",
    RestoreTableFromBackup => "RestoreTableFromBackup",
    RestoreTableToPointInTime => "RestoreTableToPointInTime",
}

#[derive(Debug, Default)]
struct Counters {
    calls: AtomicU64,
    errors: AtomicU64,
    attempts: AtomicU64,
    throttles: AtomicU64,
    duration_micros: AtomicU64,
    read_units_milli: AtomicU64,
    write_units_milli: AtomicU64,
    latency: [AtomicU64; LATENCY_BUCKETS_MS.len()],
}

/// Counters of one operation at the time of a snapshot.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct OperationMetrics {
    /// Completed calls (successful or not).
    pub calls: u64,
    /// Calls that ended with an error.
    pub errors: u64,
    /// Attempts sent (calls plus retries).
    pub attempts: u64,
    /// Attempts that were throttled.
    pub throttles: u64,
    /// Total time spent, in seconds (retries included).
    pub duration: f64,
    /// Read capacity units consumed (when consumed capacity is returned).
    pub read_capacity_units: f64,
    /// Write capacity units consumed (when consumed capacity is returned).
    pub write_capacity_units: f64,
    /// Calls per latency bucket (upper bounds in [`LATENCY_BUCKETS_MS`]).
    pub latency_buckets: Vec<u64>,
}

/// A snapshot of every counter.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MetricsSnapshot {
    /// Operations that were called at least once, with their counters.
    pub operations: Vec<(&'static str, OperationMetrics)>,
    /// Calls in progress.
    pub in_flight: i64,
}

/// Metrics of a client.
#[derive(Debug)]
pub struct Metrics {
    counters: Vec<Counters>,
    in_flight: AtomicI64,
}

impl Default for Metrics {
    fn default() -> Self {
        Self { counters: Operation::ALL.iter().map(|_| Counters::default()).collect(), in_flight: AtomicI64::new(0) }
    }
}

impl Metrics {
    /// Marks the start of a call; the guard marks its end when dropped.
    pub fn start(&self) -> InFlight<'_> {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
        InFlight { metrics: self }
    }

    /// Records a finished call.
    pub fn record(&self, operation: Operation, duration: Duration, attempts: u32, throttles: u32, failed: bool) {
        let counters = &self.counters[operation as usize];
        counters.calls.fetch_add(1, Ordering::Relaxed);
        if failed {
            counters.errors.fetch_add(1, Ordering::Relaxed);
        }
        counters.attempts.fetch_add(u64::from(attempts), Ordering::Relaxed);
        counters.throttles.fetch_add(u64::from(throttles), Ordering::Relaxed);
        counters.duration_micros.fetch_add(duration.as_micros() as u64, Ordering::Relaxed);
        let millis = duration.as_secs_f64() * 1000.0;
        let bucket =
            LATENCY_BUCKETS_MS.iter().position(|bound| millis <= *bound).unwrap_or(LATENCY_BUCKETS_MS.len() - 1);
        counters.latency[bucket].fetch_add(1, Ordering::Relaxed);
    }

    /// Records consumed capacity.
    pub fn record_capacity(&self, operation: Operation, read: f64, write: f64) {
        let counters = &self.counters[operation as usize];
        counters.read_units_milli.fetch_add((read * 1000.0).round() as u64, Ordering::Relaxed);
        counters.write_units_milli.fetch_add((write * 1000.0).round() as u64, Ordering::Relaxed);
    }

    /// A consistent-enough snapshot (each counter is read atomically).
    pub fn snapshot(&self) -> MetricsSnapshot {
        let operations = Operation::ALL
            .iter()
            .zip(&self.counters)
            .filter(|(_, counters)| counters.calls.load(Ordering::Relaxed) > 0)
            .map(|(operation, counters)| {
                (
                    operation.as_str(),
                    OperationMetrics {
                        calls: counters.calls.load(Ordering::Relaxed),
                        errors: counters.errors.load(Ordering::Relaxed),
                        attempts: counters.attempts.load(Ordering::Relaxed),
                        throttles: counters.throttles.load(Ordering::Relaxed),
                        duration: counters.duration_micros.load(Ordering::Relaxed) as f64 / 1_000_000.0,
                        read_capacity_units: counters.read_units_milli.load(Ordering::Relaxed) as f64 / 1000.0,
                        write_capacity_units: counters.write_units_milli.load(Ordering::Relaxed) as f64 / 1000.0,
                        latency_buckets: counters.latency.iter().map(|bucket| bucket.load(Ordering::Relaxed)).collect(),
                    },
                )
            })
            .collect();
        MetricsSnapshot { operations, in_flight: self.in_flight.load(Ordering::Relaxed) }
    }

    /// Resets every counter (in-flight calls are kept).
    pub fn reset(&self) {
        for counters in &self.counters {
            for counter in [
                &counters.calls,
                &counters.errors,
                &counters.attempts,
                &counters.throttles,
                &counters.duration_micros,
                &counters.read_units_milli,
                &counters.write_units_milli,
            ] {
                counter.store(0, Ordering::Relaxed);
            }
            for bucket in &counters.latency {
                bucket.store(0, Ordering::Relaxed);
            }
        }
    }
}

/// Decrements the in-flight gauge when dropped (also when the call is cancelled).
#[derive(Debug)]
pub struct InFlight<'a> {
    metrics: &'a Metrics,
}

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        self.metrics.in_flight.fetch_sub(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_calls_latency_and_capacity() {
        let metrics = Metrics::default();
        {
            let _guard = metrics.start();
            assert_eq!(metrics.snapshot().in_flight, 1);
            metrics.record(Operation::PutItem, Duration::from_millis(3), 2, 1, false);
            metrics.record_capacity(Operation::PutItem, 0.0, 1.0);
        }
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.in_flight, 0);
        let (name, put) = &snapshot.operations[0];
        assert_eq!(*name, "PutItem");
        assert_eq!((put.calls, put.attempts, put.throttles, put.write_capacity_units), (1, 2, 1, 1.0));
        assert_eq!(put.latency_buckets[2], 1, "3 ms falls in the <= 5 ms bucket");
        metrics.reset();
        assert!(metrics.snapshot().operations.is_empty());
    }
}
