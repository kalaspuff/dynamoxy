//! [`Client`]: validated configuration, a backend, retries, timeouts, events and metrics.
//!
//! Every operation returns a [`Call`]: the result plus [`OperationStats`] (attempts, throttles,
//! duration), so callers can trace failed calls too. Retries follow [`crate::config::RetryConfig`]
//! with exponential backoff and full jitter; every attempt has its own timeout.
//!
//! Managed batches ([`Client::batch_write`], [`Client::batch_get`]) split any number of requests
//! into DynamoDB-sized chunks, send them with bounded concurrency and resend unprocessed items
//! with backoff for up to `batch_max_rounds` rounds.

use std::collections::BTreeMap;
use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::task::JoinSet;
use tokio::time::Instant;

use crate::backend::Backend;
use crate::backend::aws::AwsBackend;
use crate::backend::memory::MemoryBackend;
use crate::config::ClientConfig;
use crate::error::{Error, ErrorKind, Result};
use crate::events::{Event, EventBus, EventKind};
use crate::metrics::{Metrics, Operation};
use crate::retry::{backoff, backoff_at_least};
use crate::types::{
    BackupDescription, BackupDetails, BatchExecuteStatementInput, BatchExecuteStatementOutput, BatchGetItemInput,
    BatchGetItemOutput, BatchWriteItemInput, BatchWriteItemOutput, ConsumedCapacity, ContinuousBackupsDescription,
    CreateTableInput, DeleteItemInput, ExecuteStatementInput, ExecuteStatementOutput, ExecuteTransactionInput,
    ExecuteTransactionOutput, GetItemInput, GetItemOutput, IndexStatus, KeysAndAttributes, ListBackupsInput,
    ListBackupsOutput, ListTablesInput, ListTablesOutput, ListTagsOutput, PutItemInput, QueryInput, QueryOutput,
    RestoreTableFromBackupInput, RestoreTableToPointInTimeInput, ReturnConsumedCapacity, ScanInput, TableDescription,
    TableStatus, TimeToLiveDescription, TransactGetItemsInput, TransactGetItemsOutput, TransactWriteItemsInput,
    TransactWriteItemsOutput, UpdateItemInput, UpdateTableInput, UpdateTimeToLiveInput, WriteItemOutput, WriteRequest,
};
use crate::value::Item;

/// What one call cost.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct OperationStats {
    /// The operation.
    pub operation: Operation,
    /// Attempts sent (1 when the first attempt succeeded).
    pub attempts: u32,
    /// Attempts that were throttled.
    pub throttles: u32,
    /// Requests sent (several for managed batches).
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
    fn capacity(&self) -> (f64, f64);
}

/// Capacity units of a list of reports (DynamoDB often fills only `capacity_units`).
fn units<'a>(capacities: impl IntoIterator<Item = &'a ConsumedCapacity>) -> f64 {
    capacities
        .into_iter()
        .map(|capacity| {
            if capacity.capacity_units > 0.0 {
                capacity.capacity_units
            } else {
                capacity.read_capacity_units + capacity.write_capacity_units
            }
        })
        .sum()
}

/// Implements [`ConsumedCapacityTotals`] for outputs of read (`read`) or write (`write`) operations.
macro_rules! capacity {
    ($($output:ty => $kind:ident),+ $(,)?) => {$(
        impl ConsumedCapacityTotals for $output {
            fn capacity(&self) -> (f64, f64) {
                let units = units(self.consumed_capacity.iter());
                capacity!(@split $kind units)
            }
        }
    )+};
    (@split read $units:ident) => { ($units, 0.0) };
    (@split write $units:ident) => { (0.0, $units) };
}

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
            fn capacity(&self) -> (f64, f64) {
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
    /// Writes still unprocessed after `batch_max_rounds` rounds (normally empty).
    pub unprocessed: Vec<(String, WriteRequest)>,
    /// Consumed capacity, summed per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// The result of [`Client::batch_get`].
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchGetResult {
    /// Items found, per table (in no particular order).
    pub items: BTreeMap<String, Vec<Item>>,
    /// Keys still unprocessed after `batch_max_rounds` rounds (normally empty).
    pub unprocessed: BTreeMap<String, KeysAndAttributes>,
    /// Consumed capacity, summed per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
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

/// What one chunk of a managed batch did: what remains (or was found) and the consumed capacity,
/// or the error, with the chunk's stats.
type ChunkOutcome<T> = (Result<(T, Vec<ConsumedCapacity>)>, OperationStats);

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

/// Runs one backend method with the client's retry policy.
macro_rules! call {
    ($self:ident, $operation:ident, $table:expr, $method:ident($($arg:ident),*)) => {{
        let backend = Arc::clone(&$self.inner.backend);
        $self
            .run(Operation::$operation, $table, move || {
                let backend = Arc::clone(&backend);
                $(let $arg = $arg.clone();)*
                async move { backend.$method($($arg),*).await }
            })
            .await
    }};
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

    /// Refuses new calls from now on (calls in progress finish).
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

    /// Runs `attempt` with retries, timeouts, events and metrics.
    async fn run<T, F, Fut>(&self, operation: Operation, table: Option<&str>, mut attempt: F) -> Call<T>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T>>,
        T: ConsumedCapacityTotals,
    {
        let started = Instant::now();
        let _in_flight = self.inner.metrics.start();
        let config = &self.inner.config;
        let table = table.unwrap_or("");
        let mut attempts = 0;
        let mut throttles = 0;
        let result = loop {
            if self.is_closed() {
                break Err(Error::Shutdown);
            }
            attempts += 1;
            let result = match tokio::time::timeout(config.timeout(), attempt()).await {
                Ok(result) => result,
                Err(_) => Err(Error::Transport {
                    operation: operation.as_str(),
                    message: format!("no response within {} seconds (timeout)", config.timeout),
                    retryable: true,
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
            let transport = matches!(error, Error::Transport { .. });
            let retryable = error.is_retryable() && (!transport || config.retry.retry_timeouts);
            if !retryable {
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
        let duration = started.elapsed();
        let metrics = &self.inner.metrics;
        metrics.record(operation, duration, attempts, throttles, result.is_err());
        if let Ok(output) = &result {
            let (read, write) = output.capacity();
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
        Call { result, stats: OperationStats { operation, attempts, throttles, requests: attempts, duration } }
    }

    /// `GetItem`.
    pub async fn get_item(&self, input: GetItemInput) -> Call<GetItemOutput> {
        let table = input.table_name.clone();
        call!(self, GetItem, Some(&table), get_item(input))
    }

    /// `PutItem`.
    pub async fn put_item(&self, input: PutItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        call!(self, PutItem, Some(&table), put_item(input))
    }

    /// `UpdateItem`.
    pub async fn update_item(&self, input: UpdateItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        call!(self, UpdateItem, Some(&table), update_item(input))
    }

    /// `DeleteItem`.
    pub async fn delete_item(&self, input: DeleteItemInput) -> Call<WriteItemOutput> {
        let table = input.table_name.clone();
        call!(self, DeleteItem, Some(&table), delete_item(input))
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
        let table = input.request_items.keys().next().cloned();
        call!(self, BatchGetItem, table.as_deref(), batch_get_item(input))
    }

    /// `BatchWriteItem` (one request; see [`Self::batch_write`] for any number of writes).
    pub async fn batch_write_item(&self, input: BatchWriteItemInput) -> Call<BatchWriteItemOutput> {
        let table = input.request_items.keys().next().cloned();
        call!(self, BatchWriteItem, table.as_deref(), batch_write_item(input))
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
        call!(self, TransactWriteItems, table.as_deref(), transact_write_items(input))
    }

    /// `ExecuteStatement` (PartiQL).
    pub async fn execute_statement(&self, input: ExecuteStatementInput) -> Call<ExecuteStatementOutput> {
        call!(self, ExecuteStatement, None, execute_statement(input))
    }

    /// `BatchExecuteStatement` (PartiQL).
    pub async fn batch_execute_statement(
        &self,
        input: BatchExecuteStatementInput,
    ) -> Call<BatchExecuteStatementOutput> {
        call!(self, BatchExecuteStatement, None, batch_execute_statement(input))
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
        call!(self, ExecuteTransaction, None, execute_transaction(input))
    }

    fn table_event(&self, kind: EventKind, verb: &str, description: &TableDescription) {
        self.emit(kind, || {
            Event::new(kind, format!("table {} {verb}", description.table_name))
                .field("table", &description.table_name)
                .field("status", description.table_status.as_str())
        });
    }

    /// `CreateTable`.
    pub async fn create_table(&self, input: CreateTableInput) -> Call<TableDescription> {
        let table = input.table_name.clone();
        let call = call!(self, CreateTable, Some(&table), create_table(input));
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "created", description);
        }
        call
    }

    /// `DeleteTable`.
    pub async fn delete_table(&self, table_name: &str) -> Call<TableDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        let call = self
            .run(Operation::DeleteTable, Some(table_name), move || {
                let backend = Arc::clone(&backend);
                let name = name.clone();
                async move { backend.delete_table(&name).await }
            })
            .await;
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableDeleted, "deleted", description);
        }
        call
    }

    /// `DescribeTable`.
    pub async fn describe_table(&self, table_name: &str) -> Call<TableDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::DescribeTable, Some(table_name), move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.describe_table(&name).await }
        })
        .await
    }

    /// `UpdateTable`.
    pub async fn update_table(&self, input: UpdateTableInput) -> Call<TableDescription> {
        let table = input.table_name.clone();
        let call = call!(self, UpdateTable, Some(&table), update_table(input));
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableUpdated, "updated", description);
        }
        call
    }

    /// `ListTables` (one page).
    pub async fn list_tables(&self, input: ListTablesInput) -> Call<ListTablesOutput> {
        call!(self, ListTables, None, list_tables(input))
    }

    /// `UpdateTimeToLive`.
    pub async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Call<TimeToLiveDescription> {
        let table = input.table_name.clone();
        call!(self, UpdateTimeToLive, Some(&table), update_time_to_live(input))
    }

    /// `DescribeTimeToLive`.
    pub async fn describe_time_to_live(&self, table_name: &str) -> Call<TimeToLiveDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let name = table_name.to_owned();
        self.run(Operation::DescribeTimeToLive, Some(table_name), move || {
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
        self.run(Operation::TagResource, None, move || {
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
        self.run(Operation::UntagResource, None, move || {
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
                .run(Operation::ListTagsOfResource, None, move || {
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
        self.run(Operation::DescribeContinuousBackups, Some(table_name), move || {
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
        self.run(Operation::UpdateContinuousBackups, Some(table_name), move || {
            let backend = Arc::clone(&backend);
            let name = name.clone();
            async move { backend.update_continuous_backups(&name, enabled, recovery_period_in_days).await }
        })
        .await
    }

    /// `CreateBackup`.
    pub async fn create_backup(&self, table_name: &str, backup_name: &str) -> Call<BackupDetails> {
        let backend = Arc::clone(&self.inner.backend);
        let (name, backup) = (table_name.to_owned(), backup_name.to_owned());
        self.run(Operation::CreateBackup, Some(table_name), move || {
            let backend = Arc::clone(&backend);
            let (name, backup) = (name.clone(), backup.clone());
            async move { backend.create_backup(&name, &backup).await }
        })
        .await
    }

    /// `DescribeBackup`.
    pub async fn describe_backup(&self, backup_arn: &str) -> Call<BackupDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = backup_arn.to_owned();
        self.run(Operation::DescribeBackup, None, move || {
            let backend = Arc::clone(&backend);
            let arn = arn.clone();
            async move { backend.describe_backup(&arn).await }
        })
        .await
    }

    /// `DeleteBackup`.
    pub async fn delete_backup(&self, backup_arn: &str) -> Call<BackupDescription> {
        let backend = Arc::clone(&self.inner.backend);
        let arn = backup_arn.to_owned();
        self.run(Operation::DeleteBackup, None, move || {
            let backend = Arc::clone(&backend);
            let arn = arn.clone();
            async move { backend.delete_backup(&arn).await }
        })
        .await
    }

    /// `ListBackups` (one page).
    pub async fn list_backups(&self, input: ListBackupsInput) -> Call<ListBackupsOutput> {
        let table = input.table_name.clone();
        call!(self, ListBackups, table.as_deref(), list_backups(input))
    }

    /// `RestoreTableFromBackup`.
    pub async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Call<TableDescription> {
        let table = input.target_table_name.clone();
        let call = call!(self, RestoreTableFromBackup, Some(&table), restore_table_from_backup(input));
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "restored from a backup", description);
        }
        call
    }

    /// `RestoreTableToPointInTime`.
    pub async fn restore_table_to_point_in_time(
        &self,
        input: RestoreTableToPointInTimeInput,
    ) -> Call<TableDescription> {
        let table = input.target_table_name.clone();
        let call = call!(self, RestoreTableToPointInTime, Some(&table), restore_table_to_point_in_time(input));
        if let Ok(description) = &call.result {
            self.table_event(EventKind::TableCreated, "restored to a point in time", description);
        }
        call
    }

    /// Waits until a table is `ACTIVE` with every global secondary index `ACTIVE` (`exists`), or
    /// until it no longer exists (`!exists`). Returns the final description (`None` when deleted).
    pub async fn wait_for_table(
        &self,
        table_name: &str,
        exists: bool,
        timeout: Duration,
    ) -> Result<Option<TableDescription>> {
        let deadline = Instant::now() + timeout;
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
                });
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
    /// Not atomic: on error, some chunks may have been written. Requests must not contain the
    /// same key twice (DynamoDB rejects such a chunk).
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
        let mut merge = |(outcome, chunk_stats): ChunkOutcome<Vec<(String, WriteRequest)>>,
                         stats: &mut OperationStats| {
            stats.attempts += chunk_stats.attempts;
            stats.throttles += chunk_stats.throttles;
            stats.requests += chunk_stats.requests;
            outcome.map(|(unprocessed, consumed)| {
                result.unprocessed.extend(unprocessed);
                merge_capacity(&mut capacity, consumed);
            })
        };
        if self.sequential_chunks(chunks.len()) {
            for chunk in chunks {
                let done = self.write_chunk(chunk, return_consumed_capacity).await;
                if let Err(error) = merge(done, &mut stats) {
                    stats.duration = started.elapsed();
                    return Call { result: Err(error), stats };
                }
            }
        } else {
            let mut tasks = JoinSet::new();
            let mut pending = chunks.into_iter();
            let concurrency = self.inner.config.batch_concurrency;
            loop {
                while tasks.len() < concurrency
                    && let Some(chunk) = pending.next()
                {
                    let client = self.clone();
                    tasks.spawn(async move { client.write_chunk(chunk, return_consumed_capacity).await });
                }
                let Some(joined) = tasks.join_next().await else { break };
                let done = joined.unwrap_or_else(|error| {
                    let error =
                        Error::Transport { operation: "BatchWriteItem", message: error.to_string(), retryable: false };
                    (Err(error), OperationStats { attempts: 0, throttles: 0, requests: 0, ..stats })
                });
                if let Err(error) = merge(done, &mut stats) {
                    tasks.abort_all();
                    stats.duration = started.elapsed();
                    return Call { result: Err(error), stats };
                }
            }
        }
        drop(merge);
        result.consumed_capacity = capacity.into_values().collect();
        stats.duration = started.elapsed();
        Call { result: Ok(result), stats }
    }

    /// Whether the chunks of a managed batch are sent one after the other from the calling task:
    /// a single chunk, or the in-memory backend, which applies one request at a time anyway (no
    /// task is spawned, so in-memory batches finish without a thread hop).
    fn sequential_chunks(&self, chunks: usize) -> bool {
        chunks <= 1 || self.inner.config.batch_concurrency <= 1 || self.inner.memory.is_some()
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
        let rounds = self.inner.config.batch_max_rounds;
        for round in 1..=rounds {
            let mut request_items: BTreeMap<String, Vec<WriteRequest>> = BTreeMap::new();
            for (table, request) in chunk.drain(..) {
                request_items.entry(table).or_default().push(request);
            }
            let input =
                BatchWriteItemInput { request_items, return_consumed_capacity, return_item_collection_metrics: false };
            let call = self.batch_write_item(input).await;
            stats.attempts += call.stats.attempts;
            stats.throttles += call.stats.throttles;
            stats.requests += 1;
            let output = match call.result {
                Ok(output) => output,
                Err(error) => return (Err(error), stats),
            };
            consumed.extend(output.consumed_capacity);
            chunk = output
                .unprocessed_items
                .into_iter()
                .flat_map(|(table, requests)| requests.into_iter().map(move |request| (table.clone(), request)))
                .collect();
            if chunk.is_empty() {
                return (Ok((Vec::new(), consumed)), stats);
            }
            if round == rounds {
                break;
            }
            let count = chunk.len();
            self.emit(EventKind::BatchUnprocessed, || {
                Event::new(EventKind::BatchUnprocessed, format!("{count} unprocessed writes, sending them again"))
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
        (Ok((chunk, consumed)), stats)
    }

    /// Reads any number of keys with `BatchGetItem`: chunks of 100 keys, `batch_concurrency`
    /// requests in parallel, unprocessed keys resent with backoff.
    pub async fn batch_get(
        &self,
        request_items: BTreeMap<String, KeysAndAttributes>,
        return_consumed_capacity: ReturnConsumedCapacity,
    ) -> Call<BatchGetResult> {
        let started = Instant::now();
        // Split into chunks of at most 100 keys, keeping each table's read settings.
        let mut chunks: Vec<BTreeMap<String, KeysAndAttributes>> = Vec::new();
        let mut current: BTreeMap<String, KeysAndAttributes> = BTreeMap::new();
        let mut current_size = 0;
        for (table, request) in request_items {
            for key in &request.keys {
                if current_size == 100 {
                    chunks.push(std::mem::take(&mut current));
                    current_size = 0;
                }
                current
                    .entry(table.clone())
                    .or_insert_with(|| KeysAndAttributes { keys: Vec::new(), ..request.clone() })
                    .keys
                    .push(key.clone());
                current_size += 1;
            }
        }
        if current_size > 0 {
            chunks.push(current);
        }
        let mut result = BatchGetResult::default();
        let mut capacity = BTreeMap::new();
        let mut stats = OperationStats {
            operation: Operation::BatchGetItem,
            attempts: 0,
            throttles: 0,
            requests: 0,
            duration: Duration::ZERO,
        };
        let mut merge = |(outcome, chunk_stats): ChunkOutcome<GotChunk>, stats: &mut OperationStats| {
            stats.attempts += chunk_stats.attempts;
            stats.throttles += chunk_stats.throttles;
            stats.requests += chunk_stats.requests;
            outcome.map(|((items, unprocessed), consumed)| {
                for (table, found) in items {
                    result.items.entry(table).or_default().extend(found);
                }
                for (table, keys) in unprocessed {
                    let entry = result
                        .unprocessed
                        .entry(table)
                        .or_insert_with(|| KeysAndAttributes { keys: Vec::new(), ..keys.clone() });
                    entry.keys.extend(keys.keys);
                }
                merge_capacity(&mut capacity, consumed);
            })
        };
        if self.sequential_chunks(chunks.len()) {
            for chunk in chunks {
                let done = self.get_chunk(chunk, return_consumed_capacity).await;
                if let Err(error) = merge(done, &mut stats) {
                    stats.duration = started.elapsed();
                    return Call { result: Err(error), stats };
                }
            }
        } else {
            let mut tasks = JoinSet::new();
            let mut pending = chunks.into_iter();
            let concurrency = self.inner.config.batch_concurrency;
            loop {
                while tasks.len() < concurrency
                    && let Some(chunk) = pending.next()
                {
                    let client = self.clone();
                    tasks.spawn(async move { client.get_chunk(chunk, return_consumed_capacity).await });
                }
                let Some(joined) = tasks.join_next().await else { break };
                let done = joined.unwrap_or_else(|error| {
                    let error =
                        Error::Transport { operation: "BatchGetItem", message: error.to_string(), retryable: false };
                    (Err(error), OperationStats { attempts: 0, throttles: 0, requests: 0, ..stats })
                });
                if let Err(error) = merge(done, &mut stats) {
                    tasks.abort_all();
                    stats.duration = started.elapsed();
                    return Call { result: Err(error), stats };
                }
            }
        }
        drop(merge);
        result.consumed_capacity = capacity.into_values().collect();
        stats.duration = started.elapsed();
        Call { result: Ok(result), stats }
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
        let rounds = self.inner.config.batch_max_rounds;
        for round in 1..=rounds {
            let input = BatchGetItemInput { request_items: std::mem::take(&mut chunk), return_consumed_capacity };
            let call = self.batch_get_item(input).await;
            stats.attempts += call.stats.attempts;
            stats.throttles += call.stats.throttles;
            stats.requests += 1;
            let output = match call.result {
                Ok(output) => output,
                Err(error) => return (Err(error), stats),
            };
            consumed.extend(output.consumed_capacity);
            for (table, found) in output.responses {
                items.entry(table).or_default().extend(found);
            }
            chunk = output.unprocessed_keys.into_iter().filter(|(_, keys)| !keys.keys.is_empty()).collect();
            if chunk.is_empty() {
                return (Ok(((items, BTreeMap::new()), consumed)), stats);
            }
            if round == rounds {
                break;
            }
            let count: usize = chunk.values().map(|keys| keys.keys.len()).sum();
            self.emit(EventKind::BatchUnprocessed, || {
                Event::new(EventKind::BatchUnprocessed, format!("{count} unprocessed keys, requesting them again"))
                    .field("operation", "BatchGetItem")
                    .field("count", count)
                    .field("round", round)
            });
            tokio::time::sleep(backoff_at_least(&self.inner.config.retry, round, Duration::from_millis(10))).await;
        }
        let count: usize = chunk.values().map(|keys| keys.keys.len()).sum();
        self.emit(EventKind::BatchIncomplete, || {
            Event::new(EventKind::BatchIncomplete, format!("{count} keys still unprocessed after {rounds} rounds"))
                .field("operation", "BatchGetItem")
                .field("count", count)
        });
        (Ok(((items, chunk), consumed)), stats)
    }
}

#[cfg(test)]
mod tests;
