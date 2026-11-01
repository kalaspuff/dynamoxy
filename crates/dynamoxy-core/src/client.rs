//! [`Client`]: validated configuration, a backend, retries, timeouts, events and metrics.
//!
//! Every operation returns a [`Call`]: the result plus [`OperationStats`] (attempts, throttles,
//! duration), so callers can trace failed calls too. Retries follow [`crate::config::RetryConfig`]
//! with exponential backoff and full jitter; every attempt has its own timeout.
//!
//! Managed batches ([`Client::batch_write`], [`Client::batch_get`]) split any number of requests
//! into DynamoDB-sized chunks, send them with bounded concurrency and resend unprocessed items
//! with backoff for up to `batch_max_rounds` rounds.
//!
//! An attempt that timed out, lost its connection or got a 5xx may have been applied
//! ([`Error::may_have_been_applied`]). Reads, transactions (whose client request token makes
//! them idempotent) and writes that apply once even when sent twice are sent again. An update
//! that would apply twice (`ADD n :one`, `SET l = list_append(l, :v)`) is not: the call fails
//! with [`Error::OutcomeUnknown`]. So does a write whose next attempt gives an answer that the
//! earlier attempt may have caused: a failed condition (`create()` of an item the earlier
//! attempt created), a `DuplicateItem` of a PartiQL `INSERT`, or old values (the item the
//! earlier attempt wrote, or the absence of the item it deleted). With `retry_timeouts` off, a
//! write that timed out or lost its connection is not sent again and fails with
//! [`Error::OutcomeUnknown`] too; calls that are always sent again (reads, batch writes, table
//! operations) keep their retryable [`Error::Transport`]. An attempt that failed to connect sent
//! nothing (`Error::Transport { sent: false, .. }`): it leaves no outcome unknown, and every write
//! is sent again after it.
//!
//! Table operations are sent again too, and settle a refusal their earlier attempt may have
//! caused, or the lack of an answer, by describing the table (`run_settled`): a `CreateTable`
//! that finds its own table, a `DeleteTable` that finds none, an `UpdateTable` or
//! `UpdateTimeToLive` whose change is there succeed; a retryable error stands only when the table
//! shows that nothing was applied. `CreateBackup` and the restores, which would create a second backup or fail on their
//! own table, are not sent again.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::task::JoinSet;
use tokio::time::Instant;

use crate::backend::Backend;
use crate::backend::aws::AwsBackend;
use crate::backend::memory::MemoryBackend;
use crate::backend::memory::partiql::{self, Effect};
use crate::config::ClientConfig;
use crate::error::{Error, ErrorKind, Result, ServiceError};
use crate::events::{Event, EventBus, EventKind};
use crate::expr::{Placeholders, is_idempotent, parse_update};
use crate::metrics::{Metrics, Operation};
use crate::retry::{backoff, backoff_at_least};
use crate::types::{
    BackupDescription, BackupDetails, BackupStatus, BatchExecuteStatementInput, BatchExecuteStatementOutput,
    BatchGetItemInput, BatchGetItemOutput, BatchWriteItemInput, BatchWriteItemOutput, BillingMode, ConsumedCapacity,
    ContinuousBackupsDescription, CreateTableInput, DeleteItemInput, ExecuteStatementInput, ExecuteStatementOutput,
    ExecuteTransactionInput, ExecuteTransactionOutput, GetItemInput, GetItemOutput, GlobalSecondaryIndexUpdate,
    IndexDescription, IndexStatus, KeySchema, KeysAndAttributes, ListBackupsInput, ListBackupsOutput, ListTablesInput,
    ListTablesOutput, ListTagsOutput, Projection, PutItemInput, QueryInput, QueryOutput, RestoreTableFromBackupInput,
    RestoreTableToPointInTimeInput, ReturnConsumedCapacity, ReturnValues, ScanInput, TableDescription, TableStatus,
    TimeToLiveDescription, TimeToLiveStatus, TransactGetItemsInput, TransactGetItemsOutput, TransactWriteItemsInput,
    TransactWriteItemsOutput, UpdateItemInput, UpdateTableInput, UpdateTimeToLiveInput, WriteItemOutput, WriteRequest,
};
use crate::value::{BATCH_GET_RESPONSE_LIMIT, Item, MAX_ITEM_SIZE, item_size};

/// What one call cost.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct OperationStats {
    /// The operation.
    pub operation: Operation,
    /// Attempts sent (1 when the first attempt succeeded).
    pub attempts: u32,
    /// Attempts that were throttled.
    pub throttles: u32,
    /// Requests sent: 1 for a single call, however many attempts it took; managed batches send
    /// one per chunk and round of unprocessed items. Retries are `attempts - requests`.
    pub requests: u32,
    /// Wall-clock time, retries and backoff included.
    pub duration: Duration,
}

/// The result of an operation and what it cost.
#[derive(Debug)]
pub struct Call<T> {
    /// The result.
    pub result: Result<T>,
    /// Attempts, throttles and duration.
    pub stats: OperationStats,
}

/// Capacity reported by an output, for metrics.
trait ConsumedCapacityTotals {
    /// Read and write units. `writes` says how to count units reported without a read/write
    /// split: as writes or as reads (`None`: as the operation's kind, which PartiQL outputs do
    /// not know).
    fn capacity(&self, writes: Option<bool>) -> (f64, f64);
}

/// Read and write units of a list of reports. DynamoDB often fills only `capacity_units`: those
/// count as writes or reads as `writes` says; a reported split is taken as it is.
fn split<'a>(capacities: impl IntoIterator<Item = &'a ConsumedCapacity>, writes: bool) -> (f64, f64) {
    let (mut read, mut write) = (0.0, 0.0);
    for capacity in capacities {
        if capacity.read_capacity_units > 0.0 || capacity.write_capacity_units > 0.0 {
            read += capacity.read_capacity_units;
            write += capacity.write_capacity_units;
        } else if writes {
            write += capacity.capacity_units;
        } else {
            read += capacity.capacity_units;
        }
    }
    (read, write)
}

/// Implements [`ConsumedCapacityTotals`] for outputs of read (`read`) or write (`write`) operations.
macro_rules! capacity {
    ($($output:ty => $kind:ident),+ $(,)?) => {$(
        impl ConsumedCapacityTotals for $output {
            fn capacity(&self, writes: Option<bool>) -> (f64, f64) {
                split(self.consumed_capacity.iter(), writes.unwrap_or(capacity!(@writes $kind)))
            }
        }
    )+};
    (@writes read) => { false };
    (@writes write) => { true };
}

// PartiQL outputs are reads unless the statements write (`Safety::writes`).
capacity! {
    GetItemOutput => read,
    WriteItemOutput => write,
    QueryOutput => read,
    ExecuteStatementOutput => read,
    BatchGetItemOutput => read,
    BatchWriteItemOutput => write,
    TransactGetItemsOutput => read,
    TransactWriteItemsOutput => write,
    BatchExecuteStatementOutput => read,
    ExecuteTransactionOutput => read,
}

macro_rules! no_capacity {
    ($($output:ty),+ $(,)?) => {$(
        impl ConsumedCapacityTotals for $output {
            fn capacity(&self, _: Option<bool>) -> (f64, f64) {
                (0.0, 0.0)
            }
        }
    )+};
}

no_capacity!(
    TableDescription,
    ListTablesOutput,
    TimeToLiveDescription,
    (),
    ListTagsOutput,
    ContinuousBackupsDescription,
    BackupDetails,
    BackupDescription,
    ListBackupsOutput,
);

/// The result of [`Client::batch_write`].
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchWriteResult {
    /// Writes not made (normally empty): still unprocessed after `batch_max_rounds` rounds, those
    /// of a request that failed, and, after an error that is not retried, those never sent.
    /// Batch writes are unconditional, so sending them again is safe.
    pub unprocessed: Vec<(String, WriteRequest)>,
    /// Consumed capacity, summed per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
    /// Why writes are left, when a request failed (see [`BatchGetResult::error`]).
    pub error: Option<Error>,
}

/// The result of [`Client::batch_get`].
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchGetResult {
    /// Items found, per table (in no particular order).
    pub items: BTreeMap<String, Vec<Item>>,
    /// Keys not read (normally empty): still unprocessed after `batch_max_rounds` rounds, those of
    /// a request that failed, and, after an error that is not retried, those never sent.
    pub unprocessed: BTreeMap<String, KeysAndAttributes>,
    /// Consumed capacity, summed per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
    /// Why keys are left, when a request failed: an error that is not retried (validation, access
    /// denied, a missing table), which stopped the batch (requests in flight finished, no other was
    /// sent), else the last retryable error (throttling, 5xx, transport) of a request that failed
    /// after its attempts in the last round. `None` when DynamoDB returned the requests
    /// unprocessed.
    pub error: Option<Error>,
}

/// The core client. Cheap to clone; clones share the backend, events and metrics.
#[derive(Clone, Debug)]
pub struct Client {
    inner: Arc<Inner>,
}

#[derive(Debug)]
struct Inner {
    backend: Arc<dyn Backend>,
    memory: Option<MemoryBackend>,
    config: ClientConfig,
    events: EventBus,
    metrics: Metrics,
    closed: AtomicBool,
}

/// What one chunk of a managed batch did.
struct ChunkDone<T> {
    /// What remains unprocessed (and, for reads, the items found).
    left: T,
    /// Consumed capacity of the chunk's requests.
    consumed: Vec<ConsumedCapacity>,
    /// The error of the chunk's last request, when it failed: the requests it carried are part of
    /// `left`. After an error that is not retried, the chunk stopped there.
    error: Option<Error>,
}

/// What one chunk of a managed batch did, with the chunk's stats.
type ChunkOutcome<T> = (ChunkDone<T>, OperationStats);

/// Keeps the error a managed batch reports: the first one that is not retried (which ends the
/// batch), else the last retryable one.
fn keep_error(kept: &mut Option<Error>, error: Option<Error>) {
    let Some(error) = error else { return };
    if kept.as_ref().is_none_or(Error::is_retryable) {
        *kept = Some(error);
    }
}

fn add_stats(stats: &mut OperationStats, chunk: &OperationStats) {
    stats.attempts += chunk.attempts;
    stats.throttles += chunk.throttles;
    stats.requests += chunk.requests;
}

/// The write requests of a `BatchWriteItem` request, as `(table, request)` pairs.
fn flatten_writes(request_items: BTreeMap<String, Vec<WriteRequest>>) -> Vec<(String, WriteRequest)> {
    request_items
        .into_iter()
        .flat_map(|(table, requests)| requests.into_iter().map(move |request| (table.clone(), request)))
        .collect()
}

/// Merges the unprocessed keys of a chunk of a managed `BatchGetItem` into `all`.
fn merge_keys(all: &mut BTreeMap<String, KeysAndAttributes>, keys: BTreeMap<String, KeysAndAttributes>) {
    for (table, keys) in keys {
        match all.entry(table) {
            Entry::Vacant(entry) => {
                entry.insert(keys);
            }
            Entry::Occupied(mut entry) => entry.get_mut().keys.extend(keys.keys),
        }
    }
}

/// Splits `BatchGetItem` keys into requests of at most `size` keys, in order, each table keeping
/// its read settings. Keys are moved, and only the settings are cloned for each chunk.
fn chunk_keys(
    request_items: BTreeMap<String, KeysAndAttributes>,
    size: usize,
) -> Vec<BTreeMap<String, KeysAndAttributes>> {
    let mut chunks: Vec<BTreeMap<String, KeysAndAttributes>> = Vec::new();
    let mut current: BTreeMap<String, KeysAndAttributes> = BTreeMap::new();
    let mut current_size = 0;
    for (table, request) in request_items {
        let KeysAndAttributes { keys, consistent_read, projection_expression, expression_attribute_names } = request;
        let mut keys = keys.into_iter().peekable();
        while keys.peek().is_some() {
            if current_size == size {
                chunks.push(std::mem::take(&mut current));
                current_size = 0;
            }
            let part: Vec<Item> = keys.by_ref().take(size - current_size).collect();
            current_size += part.len();
            current.insert(
                table.clone(),
                KeysAndAttributes {
                    keys: part,
                    consistent_read,
                    projection_expression: projection_expression.clone(),
                    expression_attribute_names: expression_attribute_names.clone(),
                },
            );
        }
    }
    if current_size > 0 {
        chunks.push(current);
    }
    chunks
}

/// Items found by a chunk of a managed `BatchGetItem`, and the keys still unprocessed.
type GotChunk = (BTreeMap<String, Vec<Item>>, BTreeMap<String, KeysAndAttributes>);

/// Generates a client request token (idempotent retries of transactions).
fn request_token() -> String {
    let hex = crate::backend::memory::random_hex(32);
    format!("{}-{}-{}-{}-{}", &hex[..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..])
}

fn merge_capacity(totals: &mut BTreeMap<String, ConsumedCapacity>, capacities: Vec<ConsumedCapacity>) {
    for capacity in capacities {
        let entry = totals.entry(capacity.table_name.clone()).or_insert_with(|| ConsumedCapacity {
            table_name: capacity.table_name.clone(),
            ..ConsumedCapacity::default()
        });
        entry.capacity_units += capacity.capacity_units;
        entry.read_capacity_units += capacity.read_capacity_units;
        entry.write_capacity_units += capacity.write_capacity_units;
    }
}

/// Runs one backend method with the client's retry policy (and the [`Safety`] of a write).
macro_rules! call {
    ($self:ident, $operation:ident, $table:expr, $method:ident($($arg:ident),*)) => {
        call!($self, $operation, $table, $method($($arg),*); Safety::resendable())
    };
    ($self:ident, $operation:ident, $table:expr, $method:ident($($arg:ident),*); $safety:expr) => {{
        let backend = Arc::clone(&$self.inner.backend);
        $self
            .run(Operation::$operation, $table, $safety, move || {
                let backend = Arc::clone(&backend);
                $(let $arg = $arg.clone();)*
                async move { backend.$method($($arg),*).await }
            })
            .await
    }};
}

/// Why the answer to an attempt after one that may have been applied settles nothing.
enum Unsettled {
    /// A failed condition, which the earlier attempt may have caused; the item DynamoDB returned.
    Condition(Option<Item>),
    /// Old values, which may be the earlier attempt's own write.
    OldValues(Option<Item>),
    /// No old item, which the earlier attempt may have deleted.
    Deleted,
    /// No answer: the call gave up (the last attempt failed with a retryable error, which applies
    /// nothing but says nothing about the earlier attempt), or it got no response.
    NoAnswer(String),
}

/// Which old values of an item write the earlier attempt may have shaped.
#[derive(Clone, Copy, PartialEq)]
enum OldValues {
    /// None are returned, or new values only (the same after a second application).
    Trusted,
    /// Returned old values may be the earlier attempt's own write (`PutItem`, `UpdateItem`).
    Present,
    /// A missing old item may have been deleted by the earlier attempt (`DeleteItem`).
    Absent,
}

/// Says whether the answer to a later attempt settles nothing (see [`Unsettled`]).
type Settle<T> = fn(&Result<T>, OldValues) -> Option<Unsettled>;

/// What a call does after an attempt that may have been applied ([`Error::may_have_been_applied`]).
struct Safety<T> {
    /// Whether the request may be sent again: applying it twice in a row is applying it once.
    resend: bool,
    /// Whether the answer to a later attempt settles nothing.
    settle: Option<Settle<T>>,
    /// The old values the request returns.
    old_values: OldValues,
    /// Whether the request writes, for capacity reported without a read/write split (`None`:
    /// as the operation's kind; PartiQL statements say it).
    writes: Option<bool>,
    /// What sending the request again could do, when it is not sent again (`None`: apply a
    /// write twice).
    twice: Option<String>,
    /// A lookup that settles a call in progress: sent after `close()` too, which lets calls in
    /// progress finish.
    lookup: bool,
}

impl<T> Safety<T> {
    /// Reads, and writes that apply once even when sent twice and whose answers stay the same
    /// (batch writes, which have no conditions and return nothing of the items).
    fn resendable() -> Self {
        Self { resend: true, settle: None, old_values: OldValues::Trusted, writes: None, twice: None, lookup: false }
    }

    /// Transactions: their token makes a later attempt return the earlier one's result, so only
    /// the lack of an answer leaves the outcome unknown.
    fn transaction() -> Self {
        Self {
            resend: true,
            settle: Some(settle_write::<T>),
            old_values: OldValues::Trusted,
            writes: None,
            twice: None,
            lookup: false,
        }
    }

    /// A read that settles a call in progress ([`Safety::lookup`]).
    fn lookup() -> Self {
        Self { lookup: true, ..Self::resendable() }
    }

    /// Requests that create something new each time they are applied (`CreateBackup`, restores):
    /// never sent again after an attempt that may have been applied; `twice` says what sending it
    /// again could do, and how to find out what happened.
    fn once(twice: String) -> Self {
        Self { resend: false, twice: Some(twice), ..Self::transaction() }
    }
}

/// Whether a failure says nothing about an earlier attempt: no answer came (a timeout, a lost
/// connection, a 5xx, ...), or DynamoDB refused this attempt only (throttling, conflicts).
fn answers_nothing(error: &Error) -> bool {
    match error {
        Error::Service(service) => service.is_retryable(),
        Error::OutcomeUnknown { .. } | Error::Config(_) => false,
        Error::Validation(_) | Error::Transport { .. } | Error::Credentials(_) | Error::Shutdown => true,
    }
}

/// Whether the answer to a write settles nothing ([`settle_item_write`] and [`settle_statement`]
/// look at the answers to item writes too).
fn settle_write<T>(result: &Result<T>, _: OldValues) -> Option<Unsettled> {
    match result {
        Err(error) if answers_nothing(error) => Some(Unsettled::NoAnswer(reason(error))),
        _ => None,
    }
}

/// Whether the answer to a single-item write settles nothing.
fn settle_item_write(result: &Result<WriteItemOutput>, old_values: OldValues) -> Option<Unsettled> {
    match result {
        Err(error) if error.kind() == Some(ErrorKind::ConditionalCheckFailed) => {
            Some(Unsettled::Condition(error.as_service().and_then(|service| service.item.clone())))
        }
        Ok(output) if old_values == OldValues::Present && output.attributes.is_some() => {
            Some(Unsettled::OldValues(output.attributes.clone()))
        }
        Ok(output) if old_values == OldValues::Absent && output.attributes.is_none() => Some(Unsettled::Deleted),
        _ => settle_write(result, old_values),
    }
}

/// Whether the answer to a PartiQL write settles nothing.
fn settle_statement(result: &Result<ExecuteStatementOutput>, old_values: OldValues) -> Option<Unsettled> {
    match result {
        Err(error) if matches!(error.kind(), Some(ErrorKind::ConditionalCheckFailed | ErrorKind::DuplicateItem)) => {
            Some(Unsettled::Condition(error.as_service().and_then(|service| service.item.clone())))
        }
        Ok(output) if old_values == OldValues::Present && !output.items.is_empty() => {
            Some(Unsettled::OldValues(output.items.first().cloned()))
        }
        Ok(output) if old_values == OldValues::Absent && output.items.is_empty() => Some(Unsettled::Deleted),
        _ => settle_write(result, old_values),
    }
}

/// The [`Safety`] of a single-item write.
fn item_write(resend: bool, old_values: OldValues) -> Safety<WriteItemOutput> {
    Safety { resend, settle: Some(settle_item_write), old_values, writes: None, twice: None, lookup: false }
}

/// What a failed attempt says, for messages.
fn reason(error: &Error) -> String {
    match error {
        Error::Transport { message, .. } => message.clone(),
        Error::Service(service) => format!("{}: {}", service.code, service.message),
        other => other.to_string(),
    }
}

/// DynamoDB's refusal of a control-plane request after an attempt that may have been applied:
/// an answer the earlier attempt may have caused (retryable failures answer nothing).
fn refusal<T>(result: &Result<T>) -> Option<&ServiceError> {
    match result {
        Err(error) if !answers_nothing(error) => error.as_service(),
        _ => None,
    }
}

/// Whether a control-plane call ended without an answer after an attempt that may have been
/// applied: the last attempt may have been applied itself, or later ones failed with retryable
/// errors that say nothing about it.
fn no_answer<T>(result: &Result<T>) -> bool {
    matches!(result, Err(error) if answers_nothing(error))
}

/// The outcome of a control-plane request that could not be settled by looking at the resource.
fn unsettled<T>(operation: Operation, earlier: &str, result: &Result<T>, lookup: &str, failed: &Error) -> Error {
    let ended = match result {
        Err(error) if no_answer(result) => format!("no later attempt answered whether it was ({})", reason(error)),
        Err(error) => format!("the next attempt was refused ({})", reason(error)),
        Ok(_) => "the next attempt succeeded".to_owned(),
    };
    Error::OutcomeUnknown {
        operation: operation.as_str(),
        message: format!(
            "the outcome is unknown: an attempt may have been applied ({earlier}), {ended}, and {lookup} to find out \
             failed ({})",
            reason(failed)
        ),
        item: None,
    }
}

fn same_projection(requested: &Projection, described: &Projection) -> bool {
    fn attributes(projection: &Projection) -> BTreeSet<&str> {
        projection.non_key_attributes.iter().map(String::as_str).collect()
    }
    requested.projection_type == described.projection_type && attributes(requested) == attributes(described)
}

/// Whether `description` is the table that `input` creates: its key, key types and indexes.
fn creates(input: &CreateTableInput, description: &TableDescription) -> bool {
    let same_indexes = |requested: Vec<(&String, &KeySchema, &Projection)>, described: &[IndexDescription]| {
        requested.len() == described.len()
            && requested.iter().all(|(name, key, projection)| {
                described.iter().any(|index| {
                    index.index_name == **name
                        && index.key_schema == **key
                        && same_projection(projection, &index.projection)
                })
            })
    };
    input.key_schema == description.key_schema
        && input
            .attribute_definitions
            .iter()
            .all(|definition| description.attribute_type(&definition.name) == Some(definition.attribute_type))
        && same_indexes(
            input
                .global_secondary_indexes
                .iter()
                .map(|index| (&index.index_name, &index.key_schema, &index.projection))
                .collect(),
            &description.global_secondary_indexes,
        )
        && same_indexes(
            input
                .local_secondary_indexes
                .iter()
                .map(|index| (&index.index_name, &index.key_schema, &index.projection))
                .collect(),
            &description.local_secondary_indexes,
        )
}

/// Whether `description` shows every change of `input` (encryption, which descriptions do not
/// show as requested, never does).
fn updated(input: &UpdateTableInput, description: &TableDescription) -> bool {
    let index = |name: &str| description.global_secondary_indexes.iter().find(|index| index.index_name == name);
    input.billing_mode.is_none_or(|mode| description.billing_mode == mode)
        && input.provisioned_throughput.is_none_or(|throughput| description.provisioned_throughput == Some(throughput))
        && input.on_demand_throughput.is_none_or(|limits| description.on_demand_throughput == Some(limits))
        && input.global_secondary_index_updates.iter().all(|update| match update {
            GlobalSecondaryIndexUpdate::Create(global) => index(&global.index_name).is_some_and(|index| {
                index.key_schema == global.key_schema && same_projection(&global.projection, &index.projection)
            }),
            GlobalSecondaryIndexUpdate::Update { index_name, provisioned_throughput, .. } => index(index_name)
                .is_some_and(|index| {
                    provisioned_throughput.is_none_or(|throughput| index.provisioned_throughput == Some(throughput))
                }),
            GlobalSecondaryIndexUpdate::Delete { index_name } => {
                index(index_name).is_none_or(|index| index.index_status == Some(IndexStatus::Deleting))
            }
        })
        && input.stream_specification.is_none_or(|stream| {
            let current = description.stream_specification.filter(|current| current.enabled);
            match current {
                Some(current) => stream.enabled && stream.view_type.is_none_or(|view| current.view_type == Some(view)),
                None => !stream.enabled,
            }
        })
        && input.sse_specification.is_none()
        && input.table_class.is_none_or(|class| description.table_class == Some(class))
        && input
            .deletion_protection_enabled
            .is_none_or(|protection| description.deletion_protection_enabled == protection)
}

/// Whether DynamoDB is still applying a change to the table or one of its global indexes.
fn changing(description: &TableDescription) -> bool {
    description.table_status != TableStatus::Active
        || description
            .global_secondary_indexes
            .iter()
            .any(|index| index.index_status.is_some_and(|status| status != IndexStatus::Active) || index.backfilling)
}

/// The [`Safety`] of a restore into `target`.
fn restore_safety(target: &str) -> Safety<TableDescription> {
    Safety::once(format!(
        "sending it again would fail on the table it may have created: describe table {target} to see whether the \
         restore started"
    ))
}

/// What a `DeleteTable` that an earlier attempt applied returns: the table's name and `DELETING`
/// (the table is gone, and so is its description).
fn deleted(table_name: &str) -> TableDescription {
    TableDescription {
        table_name: table_name.to_owned(),
        table_arn: None,
        table_id: None,
        table_status: TableStatus::Deleting,
        creation_date_time: None,
        key_schema: KeySchema { partition_key: String::new(), sort_key: None },
        attribute_definitions: Vec::new(),
        item_count: 0,
        table_size_bytes: 0,
        billing_mode: BillingMode::PayPerRequest,
        provisioned_throughput: None,
        on_demand_throughput: None,
        global_secondary_indexes: Vec::new(),
        local_secondary_indexes: Vec::new(),
        stream_specification: None,
        latest_stream_arn: None,
        latest_stream_label: None,
        sse_description: None,
        table_class: None,
        deletion_protection_enabled: false,
    }
}

impl Client {
    /// Creates a client on top of `backend` (validating `config` first).
    pub fn with_backend(backend: Arc<dyn Backend>, config: ClientConfig) -> Result<Self> {
        Self::build(backend, None, config)
    }

    fn build(backend: Arc<dyn Backend>, memory: Option<MemoryBackend>, config: ClientConfig) -> Result<Self> {
        config.validate()?;
        let events = EventBus::new(config.event_capacity);
        Ok(Self {
            inner: Arc::new(Inner {
                backend,
                memory,
                config,
                events,
                metrics: Metrics::default(),
                closed: AtomicBool::new(false),
            }),
        })
    }

    /// A client backed by an in-memory DynamoDB (`memory`, or a new one).
    pub fn memory(config: ClientConfig, memory: Option<MemoryBackend>) -> Result<Self> {
        let memory = memory.unwrap_or_default();
        Self::build(Arc::new(memory.clone()), Some(memory), config)
    }

    /// Connects to DynamoDB (or `endpoint_url`) through the AWS SDK.
    pub async fn connect(config: ClientConfig) -> Result<Self> {
        config.validate()?;
        let backend = AwsBackend::connect(&config).await?;
        Self::build(Arc::new(backend), None, config)
    }

    /// `"memory"` or `"aws"`.
    pub fn backend_name(&self) -> &'static str {
        self.inner.backend.name()
    }

    /// The event bus (drained by the Python layer).
    pub fn events(&self) -> &EventBus {
        &self.inner.events
    }

    /// The metrics.
    pub fn metrics(&self) -> &Metrics {
        &self.inner.metrics
    }

    /// The in-memory backend, for test helpers (`None` for AWS).
    pub fn memory_backend(&self) -> Option<&MemoryBackend> {
        self.inner.memory.as_ref()
    }

    /// The configuration.
    pub fn config(&self) -> &ClientConfig {
        &self.inner.config
    }

    /// Refuses new calls from now on (calls in progress finish, retries included).
    pub fn close(&self) {
        self.inner.closed.store(true, Ordering::Release);
    }

    /// Whether [`Self::close`] was called.
    pub fn is_closed(&self) -> bool {
        self.inner.closed.load(Ordering::Acquire)
    }

    fn emit(&self, kind: EventKind, build: impl FnOnce() -> Event) {
        self.inner.events.emit(kind, build);
    }

    /// Runs `attempt` with retries, timeouts, events and metrics; `safety` says what to do after
    /// an attempt that may have been applied.
    async fn run<T, F, Fut>(&self, operation: Operation, table: Option<&str>, safety: Safety<T>, attempt: F) -> Call<T>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T>>,
        T: ConsumedCapacityTotals,
    {
        self.run_settled(operation, table, safety, attempt, |result, _| std::future::ready(result)).await
    }

    /// [`Self::run`], and after an attempt that may have been applied, `settle` gets the call's
    /// result and what that attempt failed with, and returns the result to report: control-plane
    /// operations look at the table to tell an answer their own earlier attempt caused (a table
    /// that already exists) from a real refusal.
    async fn run_settled<T, F, Fut, S, SFut>(
        &self,
        operation: Operation,
        table: Option<&str>,
        safety: Safety<T>,
        mut attempt: F,
        settle: S,
    ) -> Call<T>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T>>,
        S: FnOnce(Result<T>, String) -> SFut,
        SFut: Future<Output = Result<T>>,
        T: ConsumedCapacityTotals,
    {
        let started = Instant::now();
        let _in_flight = self.inner.metrics.start();
        let config = &self.inner.config;
        let table = table.unwrap_or("");
        let mut attempts = 0;
        let mut throttles = 0;
        // What the last attempt that may have been applied failed with, and whether it was the
        // last attempt of the call.
        let mut uncertain: Option<(String, u32)> = None;
        let result = loop {
            // Calls in progress finish their retries after `close()`; new calls are refused.
            if attempts == 0 && !safety.lookup && self.is_closed() {
                break Err(Error::Shutdown);
            }
            attempts += 1;
            let result = match tokio::time::timeout(config.timeout(), attempt()).await {
                Ok(result) => result,
                Err(_) => Err(Error::Transport {
                    operation: operation.as_str(),
                    message: format!("no response within {} seconds (timeout)", config.timeout),
                    retryable: true,
                    sent: true,
                }),
            };
            let error = match result {
                Ok(output) => break Ok(output),
                Err(error) => error,
            };
            if error.kind() == Some(ErrorKind::Throttling) {
                throttles += 1;
                self.emit(EventKind::RequestThrottled, || {
                    Event::new(EventKind::RequestThrottled, format!("{} was throttled", operation.as_str()))
                        .field("operation", operation.as_str())
                        .field("table", table)
                        .field("code", error.as_service().map_or("", |service| service.code.as_str()))
                        .field("attempt", attempts)
                });
            }
            if !error.is_retryable() {
                break Err(error);
            }
            // Also on the last attempt: one that may have been applied leaves the outcome unknown
            // whether or not attempts are left.
            if error.may_have_been_applied() {
                if !safety.resend {
                    break Err(Error::OutcomeUnknown {
                        operation: operation.as_str(),
                        message: format!(
                            "the outcome is unknown: the attempt may have been applied ({}), and {}",
                            reason(&error),
                            safety.twice.as_deref().unwrap_or("sending the write again could apply it twice"),
                        ),
                        item: None,
                    });
                }
                uncertain = Some((reason(&error), attempts));
            }
            // `retry_timeouts=False`: the caller decides. A write that may have been applied is
            // settled below like one out of attempts, so it does not leave as a retryable error.
            if matches!(error, Error::Transport { .. }) && !config.retry.retry_timeouts {
                break Err(error);
            }
            if attempts >= config.retry.max_attempts {
                self.emit(EventKind::RequestGaveUp, || {
                    Event::new(
                        EventKind::RequestGaveUp,
                        format!("{} failed after {attempts} attempts: {error}", operation.as_str()),
                    )
                    .field("operation", operation.as_str())
                    .field("table", table)
                    .field("attempts", attempts)
                });
                break Err(error);
            }
            let delay = backoff(&config.retry, attempts);
            self.emit(EventKind::RequestRetry, || {
                Event::new(EventKind::RequestRetry, format!("retrying {} after: {error}", operation.as_str()))
                    .field("operation", operation.as_str())
                    .field("table", table)
                    .field("attempt", attempts)
                    .field("delay_ms", delay.as_millis())
            });
            tokio::time::sleep(delay).await;
        };
        let result = match &uncertain {
            Some((earlier, _)) => settle(result, earlier.clone()).await,
            None => result,
        };
        let writes = safety.writes;
        let unsettled = match (&uncertain, safety.settle) {
            (Some(_), Some(settle)) => settle(&result, safety.old_values),
            _ => None,
        };
        let result = match (unsettled, uncertain) {
            (Some(unsettled), Some((earlier, attempt))) => {
                let (message, item) = match unsettled {
                    Unsettled::Condition(item) => (
                        format!(
                            "the outcome is unknown: an attempt may have been applied ({earlier}), and the next \
                             attempt's condition may have failed because of it"
                        ),
                        item,
                    ),
                    Unsettled::OldValues(item) => (
                        format!(
                            "the outcome is unknown: an attempt may have been applied ({earlier}), so the old \
                             values the next attempt returned may be its own"
                        ),
                        item,
                    ),
                    Unsettled::Deleted => (
                        format!(
                            "the outcome is unknown: an attempt may have deleted the item ({earlier}), so what it \
                             held is not known"
                        ),
                        None,
                    ),
                    Unsettled::NoAnswer(_) if attempt == attempts => {
                        (format!("the outcome is unknown: the last attempt may have been applied ({earlier})"), None)
                    }
                    Unsettled::NoAnswer(last) => (
                        format!(
                            "the outcome is unknown: an attempt may have been applied ({earlier}), and no later \
                             attempt answered whether it was ({last})"
                        ),
                        None,
                    ),
                };
                Err(Error::OutcomeUnknown { operation: operation.as_str(), message, item })
            }
            _ => result,
        };
        if let Err(Error::OutcomeUnknown { message, .. }) = &result {
            self.emit(EventKind::RequestOutcomeUnknown, || {
                Event::new(EventKind::RequestOutcomeUnknown, format!("{}: {message}", operation.as_str()))
                    .field("operation", operation.as_str())
                    .field("table", table)
                    .field("attempts", attempts)
            });
        }
        let duration = started.elapsed();
        let metrics = &self.inner.metrics;
        metrics.record(operation, duration, attempts, throttles, result.is_err());
        if let Ok(output) = &result {
            let (read, write) = output.capacity(writes);
            if read > 0.0 || write > 0.0 {
                metrics.record_capacity(operation, read, write);
            }
        }
        if config.slow_operation_threshold > 0.0 && duration.as_secs_f64() > config.slow_operation_threshold {
            self.emit(EventKind::RequestSlow, || {
                Event::new(
                    EventKind::RequestSlow,
                    format!("{} took {:.0} ms", operation.as_str(), duration.as_secs_f64() * 1000.0),
                )
                .field("operation", operation.as_str())
                .field("table", table)
                .field("duration_ms", duration.as_millis())
                .field("attempts", attempts)
            });
        }
        // One request, however many attempts it took (managed batches add up their requests).
        Call { result, stats: OperationStats { operation, attempts, throttles, requests: 1, duration } }
    }

    /// `GetItem`.
    pub async fn get_item(&self, input: GetItemInput) -> Call<GetItemOutput> {
        let table = input.table_name.clone();
        call!(self, GetItem, Some(&table), get_item(input))
    }

    /// `PutItem`.
    pub async fn put_item(&self, input: PutItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        let old_values =
            if input.return_values == ReturnValues::AllOld { OldValues::Present } else { OldValues::Trusted };
        call!(self, PutItem, Some(&table), put_item(input); item_write(true, old_values))
    }

    /// `UpdateItem`. After an attempt that may have been applied, it is sent again only when it
    /// is idempotent ([`is_idempotent`]).
    pub async fn update_item(&self, input: UpdateItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        let resend = match &input.update_expression {
            None => true,
            Some(text) => {
                let mut placeholders =
                    Placeholders::new(&input.expression_attribute_names, &input.expression_attribute_values);
                parse_update(text, &mut placeholders).is_ok_and(|update| is_idempotent(&update))
            }
        };
        let old_values = match input.return_values {
            ReturnValues::AllOld | ReturnValues::UpdatedOld => OldValues::Present,
            _ => OldValues::Trusted,
        };
        call!(self, UpdateItem, Some(&table), update_item(input); item_write(resend, old_values))
    }

    /// `DeleteItem`.
    pub async fn delete_item(&self, input: DeleteItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        let old_values =
            if input.return_values == ReturnValues::AllOld { OldValues::Absent } else { OldValues::Trusted };
        call!(self, DeleteItem, Some(&table), delete_item(input); item_write(true, old_values))
    }

    /// `Query` (one page).
    pub async fn query(&self, input: QueryInput) -> Call<QueryOutput> {
        let table = input.table_name.clone();
        call!(self, Query, Some(&table), query(input))
    }

    /// `Scan` (one page).
    pub async fn scan(&self, input: ScanInput) -> Call<QueryOutput> {
        let table = input.table_name.clone();
        call!(self, Scan, Some(&table), scan(input))
    }

    /// `BatchGetItem` (one request; see [`Self::batch_get`] for any number of keys).
    pub async fn batch_get_item(&self, input: BatchGetItemInput) -> Call<BatchGetItemOutput> {
        self.send_batch_get(&input).await
    }

    /// `BatchGetItem`, keeping the input (a managed batch resends it when the request fails).
    async fn send_batch_get(&self, input: &BatchGetItemInput) -> Call<BatchGetItemOutput> {
        let table = input.request_items.keys().next().cloned();
        let backend = Arc::clone(&self.inner.backend);
        self.run(Operation::BatchGetItem, table.as_deref(), Safety::resendable(), || {
            let backend = Arc::clone(&backend);
            let input = input.clone();
            async move { backend.batch_get_item(input).await }
        })
        .await
    }

    /// `BatchWriteItem` (one request; see [`Self::batch_write`] for any number of writes).
    pub async fn batch_write_item(&self, input: BatchWriteItemInput) -> Call<BatchWriteItemOutput> {
        self.send_batch_write(&input).await
    }

    /// `BatchWriteItem`, keeping the input (a managed batch resends it when the request fails).
    async fn send_batch_write(&self, input: &BatchWriteItemInput) -> Call<BatchWriteItemOutput> {
        let table = input.request_items.keys().next().cloned();
        let backend = Arc::clone(&self.inner.backend);
        self.run(Operation::BatchWriteItem, table.as_deref(), Safety::resendable(), || {
            let backend = Arc::clone(&backend);
            let input = input.clone();
            async move { backend.batch_write_item(input).await }
        })
        .await
    }

    /// `TransactGetItems`.
    pub async fn transact_get_items(&self, input: TransactGetItemsInput) -> Call<TransactGetItemsOutput> {
        let table = input.transact_items.first().map(|get| get.table_name.clone());
        call!(self, TransactGetItems, table.as_deref(), transact_get_items(input))
    }

    /// `TransactWriteItems`. Without a client request token one is generated, so that retries
    /// are idempotent.
    pub async fn transact_write_items(&self, mut input: TransactWriteItemsInput) -> Call<TransactWriteItemsOutput> {
        if input.client_request_token.is_none() {
            input.client_request_token = Some(request_token());
        }
        let table = input.transact_items.first().map(|item| item.target().table_name.clone());
        call!(self, TransactWriteItems, table.as_deref(), transact_write_items(input); Safety::transaction())
    }

    /// `ExecuteStatement` (PartiQL). Writes are classified like item writes ([`partiql::effect`]).
    pub async fn execute_statement(&self, input: ExecuteStatementInput) -> Call<ExecuteStatementOutput> {
        let safety = match partiql::effect(&input.statement, &input.parameters) {
            Effect::Read => Safety::resendable(),
            Effect::Resendable { returns_old, delete } => {
                let old_values = match (returns_old, delete) {
                    (false, _) => OldValues::Trusted,
                    (true, false) => OldValues::Present,
                    (true, true) => OldValues::Absent,
                };
                Safety {
                    resend: true,
                    settle: Some(settle_statement),
                    old_values,
                    writes: Some(true),
                    twice: None,
                    lookup: false,
                }
            }
            Effect::Once => Safety {
                resend: false,
                settle: Some(settle_statement),
                old_values: OldValues::Trusted,
                writes: Some(true),
                twice: None,
                lookup: false,
            },
        };
        call!(self, ExecuteStatement, None, execute_statement(input); safety)
    }

    /// `BatchExecuteStatement` (PartiQL). With writes, it is not sent again after an attempt that
    /// may have been applied: their errors are part of the response.
    pub async fn batch_execute_statement(
        &self,
        input: BatchExecuteStatementInput,
    ) -> Call<BatchExecuteStatementOutput> {
        let reads = input
            .statements
            .iter()
            .all(|statement| partiql::effect(&statement.statement, &statement.parameters) == Effect::Read);
        let safety = if reads {
            Safety::resendable()
        } else {
            Safety { resend: false, writes: Some(true), ..Safety::transaction() }
        };
        call!(self, BatchExecuteStatement, None, batch_execute_statement(input); safety)
    }

    /// `ExecuteTransaction` (PartiQL). Without a client request token one is generated.
    pub async fn execute_transaction(&self, mut input: ExecuteTransactionInput) -> Call<ExecuteTransactionOutput> {
        let writes = input
            .transact_statements
            .iter()
            .any(|statement| !statement.statement.trim_start().to_ascii_uppercase().starts_with("SELECT"));
        if input.client_request_token.is_none() && writes {
            input.client_request_token = Some(request_token());
        }
        let safety = if writes { Safety { writes: Some(true), ..Safety::transaction() } } else { Safety::resendable() };
        call!(self, ExecuteTransaction, None, execute_transaction(input); safety)
    }

    fn table_event(&self, kind: EventKind, verb: &str, description: &TableDescription) {
        self.emit(kind, || {
            Event::new(kind, format!("table {} {verb}", description.table_name))
                .field("table", &description.table_name)
                .field("status", description.table_status.as_str())
        });
    }

    /// `CreateTable`. After an attempt that may have been applied, a table that already exists
    /// is described: when it is the one the request creates, the call succeeds with its
    /// description.
    pub async fn create_table(&self, input: CreateTableInput) -> Call<TableDescription> {
        let table = input.table_name.clone();
        let backend = Arc::clone(&self.inner.backend);
        let request = input.clone();
        let call = self
            .run_settled(
                Operation::CreateTable,
                Some(&table),
                Safety::resendable(),
                move || {
                    let backend = Arc::clone(&backend);
                    let input = input.clone();
                    async move { backend.create_table(input).await }
                },
                |result, earlier| self.settle_create_table(&request, result, earlier),
            )
            .await;
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "created", description);
        }
        call
    }

    /// `DeleteTable`. After an attempt that may have been applied, a table that no longer exists
    /// was deleted by it: the call succeeds, with only the table's name and `DELETING` in the
    /// description.
    pub async fn delete_table(&self, table_name: &str) -> Call<TableDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        let call = self
            .run_settled(
                Operation::DeleteTable,
                Some(table_name),
                Safety::resendable(),
                move || {
                    let backend = Arc::clone(&backend);
                    let name = name.clone();
                    async move { backend.delete_table(&name).await }
                },
                |result, earlier| self.settle_delete_table(table_name, result, earlier),
            )
            .await;
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableDeleted, "deleted", description);
        }
        call
    }

    /// `DescribeTable`.
    pub async fn describe_table(&self, table_name: &str) -> Call<TableDescription> {
        self.describe_table_as(table_name, Safety::resendable()).await
    }

    /// `DescribeTable` with `safety` (a lookup that settles a call, [`Safety::lookup`]).
    async fn describe_table_as(&self, table_name: &str, safety: Safety<TableDescription>) -> Call<TableDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::DescribeTable, Some(table_name), safety, move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.describe_table(&name).await }
        })
        .await
    }

    /// `UpdateTable`. After an attempt that may have been applied, a refusal (an index that
    /// already exists, a table still updating, ...) is settled by describing the table: when it
    /// shows every change of the request, the call succeeds with its description; when it shows
    /// none and nothing is in progress, the refusal stands; otherwise the outcome is unknown.
    pub async fn update_table(&self, input: UpdateTableInput) -> Call<TableDescription> {
        let table = input.table_name.clone();
        let backend = Arc::clone(&self.inner.backend);
        let request = input.clone();
        let call = self
            .run_settled(
                Operation::UpdateTable,
                Some(&table),
                Safety::resendable(),
                move || {
                    let backend = Arc::clone(&backend);
                    let input = input.clone();
                    async move { backend.update_table(input).await }
                },
                |result, earlier| self.settle_update_table(&request, result, earlier),
            )
            .await;
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableUpdated, "updated", description);
        }
        call
    }

    /// `ListTables` (one page).
    pub async fn list_tables(&self, input: ListTablesInput) -> Call<ListTablesOutput> {
        call!(self, ListTables, None, list_tables(input))
    }

    /// `UpdateTimeToLive`. After an attempt that may have been applied, a refusal ("TimeToLive is
    /// already enabled") is settled by describing Time to Live: when it is as the request sets it,
    /// the call succeeds.
    pub async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Call<TimeToLiveDescription> {
        let table = input.table_name.clone();
        let backend = Arc::clone(&self.inner.backend);
        let request = input.clone();
        self.run_settled(
            Operation::UpdateTimeToLive,
            Some(&table),
            Safety::resendable(),
            move || {
                let backend = Arc::clone(&backend);
                let input = input.clone();
                async move { backend.update_time_to_live(input).await }
            },
            |result, earlier| self.settle_time_to_live(&request, result, earlier),
        )
        .await
    }

    /// `DescribeTimeToLive`.
    pub async fn describe_time_to_live(&self, table_name: &str) -> Call<TimeToLiveDescription> {
        self.describe_time_to_live_as(table_name, Safety::resendable()).await
    }

    /// `DescribeTimeToLive` with `safety` ([`Safety::lookup`]).
    async fn describe_time_to_live_as(
        &self,
        table_name: &str,
        safety: Safety<TimeToLiveDescription>,
    ) -> Call<TimeToLiveDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::DescribeTimeToLive, Some(table_name), safety, move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.describe_time_to_live(&name).await }
        })
        .await
    }

    /// `TagResource`.
    pub async fn tag_resource(&self, resource_arn: &str, tags: BTreeMap<String, String>) -> Call<()> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = resource_arn.to_owned();
        self.run(Operation::TagResource, None, Safety::resendable(), move || {
            let backend = Arc::clone(&backend);
            let (arn, tags) = (arn.clone(), tags.clone());
            async move { backend.tag_resource(&arn, tags).await }
        })
        .await
    }

    /// `UntagResource`.
    pub async fn untag_resource(&self, resource_arn: &str, tag_keys: Vec<String>) -> Call<()> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = resource_arn.to_owned();
        self.run(Operation::UntagResource, None, Safety::resendable(), move || {
            let backend = Arc::clone(&backend);
            let (arn, keys) = (arn.clone(), tag_keys.clone());
            async move { backend.untag_resource(&arn, keys).await }
        })
        .await
    }

    /// `ListTagsOfResource`, following every page.
    pub async fn list_tags_of_resource(&self, resource_arn: &str) -> Call<ListTagsOutput> {
        let started = Instant::now();
        let mut tags = BTreeMap::new();
        let mut next_token = None;
        let mut stats = OperationStats {
            operation: Operation::ListTagsOfResource,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        loop {
            let backend = Arc::clone(&self.inner.backend);
            let arn = resource_arn.to_owned();
            let token = next_token.clone();
            let call = self
                .run(Operation::ListTagsOfResource, None, Safety::resendable(), move || {
                    let backend = Arc::clone(&backend);
                    let (arn, token) = (arn.clone(), token.clone());
                    async move { backend.list_tags_of_resource(&arn, token).await }
                })
                .await;
            stats.attempts += call.stats.attempts;
            stats.throttles += call.stats.throttles;
            stats.requests += 1;
            match call.result {
                Ok(page) => {
                    tags.extend(page.tags);
                    next_token = page.next_token;
                    if next_token.is_none() {
                        stats.duration = started.elapsed();
                        return Call { result: Ok(ListTagsOutput { tags, next_token: None }), stats };
                    }
                }
                Err(error) => {
                    stats.duration = started.elapsed();
                    return Call { result: Err(error), stats };
                }
            }
        }
    }

    /// `DescribeContinuousBackups`.
    pub async fn describe_continuous_backups(&self, table_name: &str) -> Call<ContinuousBackupsDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::DescribeContinuousBackups, Some(table_name), Safety::resendable(), move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.describe_continuous_backups(&name).await }
        })
        .await
    }

    /// `UpdateContinuousBackups` (point-in-time recovery).
    pub async fn update_continuous_backups(
        &self,
        table_name: &str,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> Call<ContinuousBackupsDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::UpdateContinuousBackups, Some(table_name), Safety::resendable(), move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.update_continuous_backups(&name, enabled, recovery_period_in_days).await }
        })
        .await
    }

    /// `CreateBackup`. Each request creates a backup, so it is not sent again after an attempt
    /// that may have been applied: the call fails with [`Error::OutcomeUnknown`].
    pub async fn create_backup(&self, table_name: &str, backup_name: &str) -> Call<BackupDetails> {
        let backend = Arc::clone(&self.inner.backend);
        let (name, backup) = (table_name.to_owned(), backup_name.to_owned());
        let safety = Safety::once(format!(
            "sending it again could create a second backup: look for backup {backup_name} in the backups of table \
             {table_name} (ListBackups) before creating it again"
        ));
        self.run(Operation::CreateBackup, Some(table_name), safety, move || {
            let backend = Arc::clone(&backend);
            let (name, backup) = (name.clone(), backup.clone());
            async move { backend.create_backup(&name, &backup).await }
        })
        .await
    }

    /// `DescribeBackup`.
    pub async fn describe_backup(&self, backup_arn: &str) -> Call<BackupDescription> {
        self.describe_backup_as(backup_arn, Safety::resendable()).await
    }

    /// `DescribeBackup` with `safety` ([`Safety::lookup`]).
    async fn describe_backup_as(&self, backup_arn: &str, safety: Safety<BackupDescription>) -> Call<BackupDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = backup_arn.to_owned();
        self.run(Operation::DescribeBackup, None, safety, move || {
            let backend = Arc::clone(&backend);
            let arn = arn.clone();
            async move { backend.describe_backup(&arn).await }
        })
        .await
    }

    /// `DeleteBackup`. After an attempt that may have been applied, a backup that no longer
    /// exists may have been deleted by it, and its description is gone with it: the call fails
    /// with [`Error::OutcomeUnknown`], not `BackupNotFoundException`.
    pub async fn delete_backup(&self, backup_arn: &str) -> Call<BackupDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = backup_arn.to_owned();
        self.run_settled(
            Operation::DeleteBackup,
            None,
            Safety::resendable(),
            move || {
                let backend = Arc::clone(&backend);
                let arn = arn.clone();
                async move { backend.delete_backup(&arn).await }
            },
            |result, earlier| self.settle_delete_backup(backup_arn, result, earlier),
        )
        .await
    }

    /// `ListBackups` (one page).
    pub async fn list_backups(&self, input: ListBackupsInput) -> Call<ListBackupsOutput> {
        let table = input.table_name.clone();
        call!(self, ListBackups, table.as_deref(), list_backups(input))
    }

    /// `RestoreTableFromBackup`. Not sent again after an attempt that may have been applied (the
    /// next attempt would fail on the table it created): the call fails with
    /// [`Error::OutcomeUnknown`].
    pub async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Call<TableDescription> {
        let table = input.target_table_name.clone();
        let safety = restore_safety(&table);
        let call = call!(self, RestoreTableFromBackup, Some(&table), restore_table_from_backup(input); safety);
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "restored from a backup", description);
        }
        call
    }

    /// `RestoreTableToPointInTime`. Not sent again after an attempt that may have been applied,
    /// like [`Self::restore_table_from_backup`].
    pub async fn restore_table_to_point_in_time(
        &self,
        input: RestoreTableToPointInTimeInput,
    ) -> Call<TableDescription> {
        let table = input.target_table_name.clone();
        let safety = restore_safety(&table);
        let call = call!(self, RestoreTableToPointInTime, Some(&table), restore_table_to_point_in_time(input); safety);
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "restored to a point in time", description);
        }
        call
    }

    /// Settles a `CreateTable` after an attempt that may have been applied: a table that exists
    /// (the next attempt's `ResourceInUseException`, or a table found when no attempt answered)
    /// is the call's when it is the one the request creates.
    async fn settle_create_table(
        &self,
        request: &CreateTableInput,
        result: Result<TableDescription>,
        earlier: String,
    ) -> Result<TableDescription> {
        let refused = refusal(&result).is_some_and(|refused| refused.kind == ErrorKind::ResourceInUse);
        if !refused && !no_answer(&result) {
            return result;
        }
        match self.describe_table_as(&request.table_name, Safety::lookup()).await.result {
            Ok(description) if creates(request, &description) => Ok(description),
            // Another table: the earlier attempt did not create it.
            Ok(_) => result,
            // No table is no proof either: DescribeTable is eventually consistent, and may not see a
            // table that was just created yet.
            Err(error) => Err(unsettled(Operation::CreateTable, &earlier, &result, "describing the table", &error)),
        }
    }

    /// Settles a `DeleteTable` after an attempt that may have been applied: a table that is gone,
    /// or being deleted when no attempt answered, was deleted by it.
    async fn settle_delete_table(
        &self,
        table_name: &str,
        result: Result<TableDescription>,
        earlier: String,
    ) -> Result<TableDescription> {
        if refusal(&result).is_some_and(|refused| refused.kind == ErrorKind::ResourceNotFound) {
            return Ok(deleted(table_name));
        }
        if !no_answer(&result) {
            return result;
        }
        match self.describe_table_as(table_name, Safety::lookup()).await.result {
            Ok(description) if description.table_status == TableStatus::Deleting => Ok(description),
            Ok(_) => result,
            Err(error) if error.kind() == Some(ErrorKind::ResourceNotFound) => Ok(deleted(table_name)),
            Err(error) => Err(unsettled(Operation::DeleteTable, &earlier, &result, "describing the table", &error)),
        }
    }

    /// Settles an `UpdateTable` after an attempt that may have been applied, when the next
    /// attempt was refused or none answered: a table that shows every change was updated by it;
    /// one that shows none and is not changing was not (the refusal or the retryable error
    /// stands); otherwise the outcome is unknown.
    async fn settle_update_table(
        &self,
        request: &UpdateTableInput,
        result: Result<TableDescription>,
        earlier: String,
    ) -> Result<TableDescription> {
        if refusal(&result).is_none() && !no_answer(&result) {
            return result;
        }
        match self.describe_table_as(&request.table_name, Safety::lookup()).await.result {
            Ok(description) if updated(request, &description) => Ok(description),
            Ok(description) if !changing(&description) => result,
            Ok(_) => Err(Error::OutcomeUnknown {
                operation: Operation::UpdateTable.as_str(),
                message: format!(
                    "the outcome is unknown: an attempt may have been applied ({earlier}), the call ended with {}, \
                     and the table is still changing: describe it once it is ACTIVE to see whether the change was \
                     applied",
                    result.as_ref().err().map(reason).unwrap_or_default()
                ),
                item: None,
            }),
            Err(error) => Err(unsettled(Operation::UpdateTable, &earlier, &result, "describing the table", &error)),
        }
    }

    /// Settles an `UpdateTimeToLive` after an attempt that may have been applied, when the next
    /// attempt was refused or none answered: Time to Live as the request sets it was set by it.
    async fn settle_time_to_live(
        &self,
        request: &UpdateTimeToLiveInput,
        result: Result<TimeToLiveDescription>,
        earlier: String,
    ) -> Result<TimeToLiveDescription> {
        if refusal(&result).is_none() && !no_answer(&result) {
            return result;
        }
        match self.describe_time_to_live_as(&request.table_name, Safety::lookup()).await.result {
            Ok(current) => {
                let applied = if request.enabled {
                    matches!(current.status, TimeToLiveStatus::Enabled | TimeToLiveStatus::Enabling)
                        && current.attribute_name.as_deref() == Some(request.attribute_name.as_str())
                } else {
                    matches!(current.status, TimeToLiveStatus::Disabled | TimeToLiveStatus::Disabling)
                };
                if !applied {
                    return result;
                }
                // What UpdateTimeToLive returns.
                Ok(TimeToLiveDescription {
                    status: if request.enabled { TimeToLiveStatus::Enabled } else { TimeToLiveStatus::Disabled },
                    attribute_name: Some(request.attribute_name.clone()),
                })
            }
            Err(error) => {
                Err(unsettled(Operation::UpdateTimeToLive, &earlier, &result, "describing Time to Live", &error))
            }
        }
    }

    /// Settles a `DeleteBackup` after an attempt that may have been applied: a backup that is gone
    /// may have been deleted by it, and its description went with it, so the outcome is unknown.
    async fn settle_delete_backup(
        &self,
        backup_arn: &str,
        result: Result<BackupDescription>,
        earlier: String,
    ) -> Result<BackupDescription> {
        let gone = || Error::OutcomeUnknown {
            operation: Operation::DeleteBackup.as_str(),
            message: format!(
                "the outcome is unknown: an attempt may have been applied ({earlier}), and backup {backup_arn} no \
                 longer exists: that attempt may have deleted it"
            ),
            item: None,
        };
        if refusal(&result).is_some_and(|refused| refused.code == "BackupNotFoundException") {
            return Err(gone());
        }
        if !no_answer(&result) {
            return result;
        }
        match self.describe_backup_as(backup_arn, Safety::lookup()).await.result {
            Ok(description) if description.details.backup_status == BackupStatus::Deleted => Err(gone()),
            // Still there: nothing was deleted, and sending the request again is safe.
            Ok(_) => result,
            Err(error) if error.as_service().is_some_and(|service| service.code == "BackupNotFoundException") => {
                Err(gone())
            }
            Err(error) => Err(unsettled(Operation::DeleteBackup, &earlier, &result, "describing the backup", &error)),
        }
    }

    /// Waits until a table is `ACTIVE` with every global secondary index `ACTIVE` (`exists`), or
    /// until it no longer exists (`!exists`). Returns the final description (`None` when deleted).
    pub async fn wait_for_table(
        &self,
        table_name: &str,
        exists: bool,
        timeout: Duration,
    ) -> Result<Option<TableDescription>> {
        let deadline = Instant::now() + timeout.min(crate::config::MAX_DURATION);
        let mut round = 0;
        loop {
            round += 1;
            match self.describe_table(table_name).await.result {
                Ok(description) if exists => {
                    let indexes_ready = description
                        .global_secondary_indexes
                        .iter()
                        .all(|index| index.index_status.is_none_or(|status| status == IndexStatus::Active));
                    if description.table_status == TableStatus::Active && indexes_ready {
                        return Ok(Some(description));
                    }
                }
                Ok(_) => {}
                Err(error) if error.kind() == Some(ErrorKind::ResourceNotFound) => {
                    if !exists {
                        return Ok(None);
                    }
                }
                Err(error) => return Err(error),
            }
            if Instant::now() >= deadline {
                let state = if exists { "become ACTIVE" } else { "be deleted" };
                return Err(Error::Transport {
                    operation: "DescribeTable",
                    message: format!("table {table_name} did not {state} within {} seconds", timeout.as_secs_f64()),
                    retryable: true,
                    sent: true,
                });
            }
            if self.inner.memory.is_some() && round < 3 {
                // The in-memory backend activates a table on the next poll (backend::memory::admin):
                // no sleep, unless a fault keeps it from answering.
                tokio::task::yield_now().await;
                continue;
            }
            let polling =
                crate::config::RetryConfig { base_delay: 0.05, max_delay: 2.0, ..self.inner.config.retry.clone() };
            let delay = backoff_at_least(&polling, round, Duration::from_millis(20));
            tokio::time::sleep(delay.min(deadline.saturating_duration_since(Instant::now()))).await;
        }
    }

    /// Writes any number of items with `BatchWriteItem`: chunks of 25, `batch_concurrency`
    /// requests in parallel, unprocessed items resent with backoff.
    ///
    /// Not atomic. Every write ends made or in [`BatchWriteResult::unprocessed`]: a request that
    /// fails with a retryable error after its attempts leaves its writes unprocessed for the next
    /// round, and one that fails with an error that is not retried stops the batch (requests in
    /// flight finish, no other is sent) with [`BatchWriteResult::error`] set. The call itself only
    /// fails when a chunk's task did not finish. Requests must not contain the same key twice
    /// (DynamoDB rejects such a chunk).
    pub async fn batch_write(
        &self,
        requests: Vec<(String, WriteRequest)>,
        return_consumed_capacity: ReturnConsumedCapacity,
    ) -> Call<BatchWriteResult> {
        let started = Instant::now();
        let mut chunks: Vec<Vec<(String, WriteRequest)>> = Vec::with_capacity(requests.len().div_ceil(25));
        let mut requests = requests.into_iter().peekable();
        while requests.peek().is_some() {
            chunks.push(requests.by_ref().take(25).collect());
        }
        let mut result = BatchWriteResult::default();
        let mut capacity = BTreeMap::new();
        let mut stats = OperationStats {
            operation: Operation::BatchWriteItem,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        let sent = self
            .run_chunks(
                "BatchWriteItem",
                chunks,
                move |client, chunk| async move { client.write_chunk(chunk, return_consumed_capacity).await },
                |(done, chunk_stats): ChunkOutcome<Vec<(String, WriteRequest)>>| {
                    add_stats(&mut stats, &chunk_stats);
                    result.unprocessed.extend(done.left);
                    merge_capacity(&mut capacity, done.consumed);
                    keep_error(&mut result.error, done.error);
                    result.error.as_ref().is_some_and(|error| !error.is_retryable())
                },
            )
            .await;
        stats.duration = started.elapsed();
        match sent {
            Ok(unsent) => {
                result.unprocessed.extend(unsent.into_iter().flatten());
                result.consumed_capacity = capacity.into_values().collect();
                Call { result: Ok(result), stats }
            }
            Err(error) => Call { result: Err(error), stats },
        }
    }

    /// Whether the chunks of a managed batch are sent one after the other from the calling task:
    /// a single chunk, or the in-memory backend, which applies one request at a time anyway (no
    /// task is spawned, so in-memory batches finish without a thread hop).
    fn sequential_chunks(&self, chunks: usize) -> bool {
        chunks <= 1 || self.inner.config.batch_concurrency <= 1 || self.inner.memory.is_some()
    }

    /// Sends the chunks of a managed batch with `send`, `batch_concurrency` at a time (or one
    /// after the other, see [`Self::sequential_chunks`]), and passes each one's outcome to
    /// `merge`. Once `merge` says stop (an error that is not retried), no other chunk is sent:
    /// the chunks in flight are awaited, so their outcome is known, and the chunks never sent are
    /// returned. Fails only when a chunk's task did not finish (it panicked or was cancelled),
    /// after the others did.
    async fn run_chunks<C, T, F, Fut>(
        &self,
        operation: &'static str,
        chunks: Vec<C>,
        send: F,
        mut merge: impl FnMut(ChunkOutcome<T>) -> bool,
    ) -> Result<Vec<C>>
    where
        C: Send + 'static,
        T: Send + 'static,
        F: Fn(Client, C) -> Fut,
        Fut: Future<Output = ChunkOutcome<T>> + Send + 'static,
    {
        let mut pending = chunks.into_iter();
        if self.sequential_chunks(pending.len()) {
            for chunk in pending.by_ref() {
                if merge(send(self.clone(), chunk).await) {
                    break;
                }
            }
            return Ok(pending.collect());
        }
        let mut tasks = JoinSet::new();
        let concurrency = self.inner.config.batch_concurrency;
        let mut stopped = false;
        let mut failure = None;
        loop {
            while !stopped
                && tasks.len() < concurrency
                && let Some(chunk) = pending.next()
            {
                tasks.spawn(send(self.clone(), chunk));
            }
            let Some(joined) = tasks.join_next().await else { break };
            match joined {
                Ok(done) => stopped |= merge(done),
                Err(error) => {
                    stopped = true;
                    failure.get_or_insert(Error::Transport {
                        operation,
                        message: error.to_string(),
                        retryable: false,
                        sent: true,
                    });
                }
            }
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(pending.collect()),
        }
    }

    /// Whether a managed batch sends a chunk again in its next round after its request failed
    /// with `error`: a retryable error, but no timeout or lost connection with `retry_timeouts`
    /// off (nothing is sent again after one; the chunk is reported with the error, and the other
    /// chunks go on).
    fn resends_chunk(&self, error: &Error) -> bool {
        error.is_retryable() && (self.inner.config.retry.retry_timeouts || !matches!(error, Error::Transport { .. }))
    }

    async fn write_chunk(
        &self,
        mut chunk: Vec<(String, WriteRequest)>,
        return_consumed_capacity: ReturnConsumedCapacity,
    ) -> ChunkOutcome<Vec<(String, WriteRequest)>> {
        let mut stats = OperationStats {
            operation: Operation::BatchWriteItem,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        let mut consumed = Vec::new();
        let mut error = None;
        let rounds = self.inner.config.batch_max_rounds;
        for round in 1..=rounds {
            let mut request_items: BTreeMap<String, Vec<WriteRequest>> = BTreeMap::new();
            for (table, request) in chunk.drain(..) {
                request_items.entry(table).or_default().push(request);
            }
            let input =
                BatchWriteItemInput { request_items, return_consumed_capacity, return_item_collection_metrics: false };
            let call = self.send_batch_write(&input).await;
            add_stats(&mut stats, &call.stats);
            match call.result {
                Ok(output) => {
                    consumed.extend(output.consumed_capacity);
                    chunk = flatten_writes(output.unprocessed_items);
                    error = None;
                }
                Err(failed) => {
                    // Nothing of a request DynamoDB rejected was applied; a request that timed
                    // out may have been, and writing its items again is harmless.
                    chunk = flatten_writes(input.request_items);
                    let resend = self.resends_chunk(&failed);
                    error = Some(failed);
                    if !resend {
                        return (ChunkDone { left: chunk, consumed, error }, stats);
                    }
                }
            }
            if chunk.is_empty() {
                return (ChunkDone { left: chunk, consumed, error: None }, stats);
            }
            if round == rounds {
                break;
            }
            let count = chunk.len();
            let failed = error.is_some();
            self.emit(EventKind::BatchUnprocessed, || {
                let message = if failed {
                    format!("a request of {count} writes failed, sending them again")
                } else {
                    format!("{count} unprocessed writes, sending them again")
                };
                Event::new(EventKind::BatchUnprocessed, message)
                    .field("operation", "BatchWriteItem")
                    .field("count", count)
                    .field("round", round)
            });
            tokio::time::sleep(backoff_at_least(&self.inner.config.retry, round, Duration::from_millis(10))).await;
        }
        let count = chunk.len();
        self.emit(EventKind::BatchIncomplete, || {
            Event::new(EventKind::BatchIncomplete, format!("{count} writes still unprocessed after {rounds} rounds"))
                .field("operation", "BatchWriteItem")
                .field("count", count)
        });
        (ChunkDone { left: chunk, consumed, error }, stats)
    }

    /// Reads any number of keys with `BatchGetItem`: chunks of 100 keys, `batch_concurrency`
    /// requests in parallel, unprocessed keys resent with backoff (at once when DynamoDB left
    /// them for its 16 MB response limit).
    ///
    /// Every key ends read or in [`BatchGetResult::unprocessed`], with the rules of
    /// [`Self::batch_write`].
    pub async fn batch_get(
        &self,
        request_items: BTreeMap<String, KeysAndAttributes>,
        return_consumed_capacity: ReturnConsumedCapacity,
    ) -> Call<BatchGetResult> {
        let started = Instant::now();
        let chunks = chunk_keys(request_items, 100);
        let mut result = BatchGetResult::default();
        let mut capacity = BTreeMap::new();
        let mut stats = OperationStats {
            operation: Operation::BatchGetItem,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        let sent = self
            .run_chunks(
                "BatchGetItem",
                chunks,
                move |client, chunk| async move { client.get_chunk(chunk, return_consumed_capacity).await },
                |(done, chunk_stats): ChunkOutcome<GotChunk>| {
                    add_stats(&mut stats, &chunk_stats);
                    let (items, unprocessed) = done.left;
                    for (table, found) in items {
                        result.items.entry(table).or_default().extend(found);
                    }
                    merge_keys(&mut result.unprocessed, unprocessed);
                    merge_capacity(&mut capacity, done.consumed);
                    keep_error(&mut result.error, done.error);
                    result.error.as_ref().is_some_and(|error| !error.is_retryable())
                },
            )
            .await;
        stats.duration = started.elapsed();
        match sent {
            Ok(unsent) => {
                for chunk in unsent {
                    merge_keys(&mut result.unprocessed, chunk);
                }
                result.consumed_capacity = capacity.into_values().collect();
                Call { result: Ok(result), stats }
            }
            Err(error) => Call { result: Err(error), stats },
        }
    }

    async fn get_chunk(
        &self,
        mut chunk: BTreeMap<String, KeysAndAttributes>,
        return_consumed_capacity: ReturnConsumedCapacity,
    ) -> ChunkOutcome<GotChunk> {
        let mut stats = OperationStats {
            operation: Operation::BatchGetItem,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        let mut consumed = Vec::new();
        let mut items: BTreeMap<String, Vec<Item>> = BTreeMap::new();
        let mut error;
        let rounds = self.inner.config.batch_max_rounds;
        let mut round = 1;
        loop {
            let input = BatchGetItemInput { request_items: std::mem::take(&mut chunk), return_consumed_capacity };
            let call = self.send_batch_get(&input).await;
            add_stats(&mut stats, &call.stats);
            let mut returned = None;
            match call.result {
                Ok(output) => {
                    consumed.extend(output.consumed_capacity);
                    chunk = output.unprocessed_keys.into_iter().filter(|(_, keys)| !keys.keys.is_empty()).collect();
                    if !chunk.is_empty() {
                        returned = Some(output.responses.values().flatten().map(item_size).sum::<usize>());
                    }
                    for (table, found) in output.responses {
                        items.entry(table).or_default().extend(found);
                    }
                    error = None;
                }
                Err(failed) => {
                    chunk = input.request_items.into_iter().filter(|(_, keys)| !keys.keys.is_empty()).collect();
                    let resend = self.resends_chunk(&failed);
                    error = Some(failed);
                    if !resend {
                        return (ChunkDone { left: (items, chunk), consumed, error }, stats);
                    }
                }
            }
            if chunk.is_empty() {
                return (ChunkDone { left: (items, chunk), consumed, error: None }, stats);
            }
            // A response that holds more than the limit less one item stopped at DynamoDB's
            // 16 MB response limit, not because of throttling: the rest is requested at once,
            // in no new round (each such response returns more than 15 MB, so this ends).
            if returned.is_some_and(|size| size + MAX_ITEM_SIZE > BATCH_GET_RESPONSE_LIMIT) {
                continue;
            }
            if round == rounds {
                break;
            }
            let count: usize = chunk.values().map(|keys| keys.keys.len()).sum();
            let failed = error.is_some();
            self.emit(EventKind::BatchUnprocessed, || {
                let message = if failed {
                    format!("a request of {count} keys failed, requesting them again")
                } else {
                    format!("{count} unprocessed keys, requesting them again")
                };
                Event::new(EventKind::BatchUnprocessed, message)
                    .field("operation", "BatchGetItem")
                    .field("count", count)
                    .field("round", round)
            });
            tokio::time::sleep(backoff_at_least(&self.inner.config.retry, round, Duration::from_millis(10))).await;
            round += 1;
        }
        let count: usize = chunk.values().map(|keys| keys.keys.len()).sum();
        self.emit(EventKind::BatchIncomplete, || {
            Event::new(EventKind::BatchIncomplete, format!("{count} keys still unprocessed after {rounds} rounds"))
                .field("operation", "BatchGetItem")
                .field("count", count)
        });
        (ChunkDone { left: (items, chunk), consumed, error }, stats)
    }
}

#[cfg(test)]
mod tests;
