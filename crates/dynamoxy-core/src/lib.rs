//! # dynamoxy-core
//!
//! The Rust core of dynamoxy: everything that is protocol, timing, I/O or CPU heavy. It has no
//! Python dependency, so it can be used and tested from Rust alone. See `docs/architecture.md`
//! for the layers and the invariants every change must preserve.
//!
//! * [`Client`]: retries, timeouts, events, metrics and managed batches over a [`backend`].
//! * [`backend`]: the DynamoDB port with two adapters, the AWS SDK and an in-memory DynamoDB.
//! * [`expr`]: the DynamoDB expression language (parser, validation, evaluation).
//! * [`value`]: attribute values and DynamoDB's number type.
//! * [`types`]: inputs and outputs of every operation.

pub mod backend;
pub mod client;
pub mod config;
pub mod error;
pub mod events;
pub mod expr;
pub mod metrics;
pub mod retry;
pub mod types;
pub mod value;

pub use client::{BatchGetResult, BatchWriteResult, Call, Client, OperationStats};
pub use config::{ClientConfig, RetryConfig};
pub use error::{CancellationReason, Error, ErrorKind, Result, ServiceError};
pub use value::{AttributeValue, Item, Number};

/// Version of the core crate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
