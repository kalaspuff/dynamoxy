//! An in-memory DynamoDB.
//!
//! [`MemoryBackend`] implements every [`Backend`] operation with DynamoDB's semantics: key and
//! item validation, the expression language, conditional writes, `ReturnValues`, secondary
//! indexes (sparse, projected), `Query`/`Scan` paging (`Limit`, the 1 MB page cap,
//! `LastEvaluatedKey`), parallel scans, batches, transactions with cancellation reasons and
//! idempotency tokens, consumed capacity, TTL settings, tags, backups and a PartiQL subset.
//! Errors carry DynamoDB's error codes and, where practical, its messages.
//!
//! It powers the Rust and Python test suites and `dynamoxy.testing.memory_client()`.
//! Test helpers: [`MemoryBackend::inject_fault`] (throttling, unprocessed batch items, latency)
//! and [`MemoryBackend::purge_expired`] (TTL deletion on demand).
//!
//! State lives behind one synchronous lock, held only inside each (synchronous) operation and
//! never across an `.await`: operations are atomic, which also makes transactions trivially
//! isolated.

mod admin;
mod batch;
mod items;
mod partiql;
mod query;
mod table;
#[cfg(test)]
mod tests;
mod transact;

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use parking_lot::Mutex;

use self::table::Table;
use super::Backend;
use crate::error::{Error, Result, ServiceError};
use crate::types::{
    BackupDescription, BackupDetails, BatchExecuteStatementInput, BatchExecuteStatementOutput, BatchGetItemInput,
    BatchGetItemOutput, BatchWriteItemInput, BatchWriteItemOutput, ContinuousBackupsDescription, CreateTableInput,
    DeleteItemInput, ExecuteStatementInput, ExecuteStatementOutput, ExecuteTransactionInput, ExecuteTransactionOutput,
    GetItemInput, GetItemOutput, ListBackupsInput, ListBackupsOutput, ListTablesInput, ListTablesOutput,
    ListTagsOutput, PutItemInput, QueryInput, QueryOutput, RestoreTableFromBackupInput, RestoreTableToPointInTimeInput,
    ScanInput, TableDescription, TimeToLiveDescription, TransactGetItemsInput, TransactGetItemsOutput,
    TransactWriteItemsInput, TransactWriteItemsOutput, UpdateItemInput, UpdateTableInput, UpdateTimeToLiveInput,
    WriteItemOutput,
};
use crate::value::AttributeValue;

/// Seconds since the Unix epoch.
pub(crate) fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// `length` random lowercase hex digits (ids and ARNs).
pub fn random_hex(length: usize) -> String {
    (0..length).map(|_| char::from_digit(fastrand::u32(0..16), 16).unwrap_or('0')).collect()
}

/// A snapshot of a table taken by `CreateBackup`.
#[derive(Clone, Debug)]
pub(crate) struct Backup {
    details: BackupDetails,
    table: Table,
}

/// A transaction token seen recently.
#[derive(Clone, Debug)]
pub(crate) struct TokenRecord {
    fingerprint: u64,
    at: f64,
}

/// What an injected fault does.
#[derive(Clone, Debug, PartialEq)]
pub enum FaultKind {
    /// Fail with this DynamoDB error code and message (e.g. `ProvisionedThroughputExceededException`).
    Error {
        /// Error code.
        code: String,
        /// Message.
        message: String,
    },
    /// `BatchGetItem` / `BatchWriteItem` return every request unprocessed (nothing is written).
    Unprocessed,
    /// Delay the operation.
    Latency(Duration),
}

/// A fault injected into the in-memory backend, for testing retries and error handling.
#[derive(Clone, Debug, PartialEq)]
pub struct Fault {
    /// Operation it applies to (`"PutItem"`, ...); `None` for every operation.
    pub operation: Option<String>,
    /// Table it applies to; `None` for every table.
    pub table: Option<String>,
    /// What happens.
    pub kind: FaultKind,
    /// How many matching calls are affected.
    pub times: u32,
}

/// Everything the backend stores.
#[derive(Debug)]
pub(crate) struct State {
    tables: BTreeMap<String, Table>,
    backups: BTreeMap<String, Backup>,
    tokens: HashMap<String, TokenRecord>,
    faults: Vec<Fault>,
    region: String,
    account_id: String,
}

impl State {
    /// The table for a data-plane operation.
    fn table(&self, name: &str) -> Result<&Table> {
        self.tables.get(name).ok_or_else(|| ServiceError::resource_not_found("Requested resource not found").into())
    }
}

/// The in-memory DynamoDB. Clones share the same data.
#[derive(Clone, Debug)]
pub struct MemoryBackend {
    state: Arc<Mutex<State>>,
}

impl Default for MemoryBackend {
    fn default() -> Self {
        Self::new("us-east-1", "000000000000")
    }
}

impl MemoryBackend {
    /// An empty backend; `region` and `account_id` appear in ARNs.
    pub fn new(region: &str, account_id: &str) -> Self {
        Self {
            state: Arc::new(Mutex::new(State {
                tables: BTreeMap::new(),
                backups: BTreeMap::new(),
                tokens: HashMap::new(),
                faults: Vec::new(),
                region: region.to_owned(),
                account_id: account_id.to_owned(),
            })),
        }
    }

    /// Adds a fault; it affects the next `fault.times` matching calls.
    pub fn inject_fault(&self, fault: Fault) {
        self.state.lock().faults.push(fault);
    }

    /// Removes every pending fault.
    pub fn clear_faults(&self) {
        self.state.lock().faults.clear();
    }

    /// Deletes items whose TTL attribute (a number of seconds since the Unix epoch) is at or
    /// before `at` (default: now), in tables with TTL enabled. Returns how many were deleted.
    ///
    /// DynamoDB deletes expired items in the background, typically within a few days; tests call
    /// this to make that happen deterministically.
    pub fn purge_expired(&self, at: Option<f64>) -> usize {
        let at = at.unwrap_or_else(now);
        let mut state = self.state.lock();
        let mut deleted = 0;
        for table in state.tables.values_mut() {
            let Some(attribute) = table.ttl_attribute.clone() else { continue };
            let expired: Vec<_> = table
                .items
                .iter()
                .filter(|(_, item)| match item.get(&attribute) {
                    Some(AttributeValue::N(number)) => number.as_str().parse::<f64>().is_ok_and(|expiry| expiry <= at),
                    _ => false,
                })
                .map(|(key, _)| key.clone())
                .collect();
            for key in expired {
                table.write(key, None);
                deleted += 1;
            }
        }
        deleted
    }

    /// Deletes every table and backup.
    pub fn reset(&self) {
        let mut state = self.state.lock();
        state.tables.clear();
        state.backups.clear();
        state.tokens.clear();
        state.faults.clear();
    }

    /// Applies a matching fault: fails, sleeps, or reports that batches return unprocessed items.
    async fn fault(&self, operation: &'static str, table: Option<&str>) -> Result<bool> {
        let triggered = {
            let mut state = self.state.lock();
            let position = state.faults.iter().position(|fault| {
                fault.times > 0
                    && fault.operation.as_deref().is_none_or(|name| name == operation)
                    && fault.table.as_deref().is_none_or(|name| Some(name) == table)
            });
            position.map(|position| {
                let fault = &mut state.faults[position];
                fault.times -= 1;
                let kind = fault.kind.clone();
                if fault.times == 0 {
                    state.faults.remove(position);
                }
                kind
            })
        };
        match triggered {
            None => Ok(false),
            Some(FaultKind::Error { code, message }) => {
                let mut error = ServiceError::new(code, message).with_operation(operation);
                if error.kind.is_retryable() {
                    error.status = Some(if error.kind == crate::error::ErrorKind::Internal { 500 } else { 400 });
                }
                Err(error.into())
            }
            Some(FaultKind::Unprocessed) => Ok(true),
            Some(FaultKind::Latency(delay)) => {
                tokio::time::sleep(delay).await;
                Ok(false)
            }
        }
    }
}

fn named<T>(operation: &'static str, result: Result<T>) -> Result<T> {
    result.map_err(|error: Error| error.with_operation(operation))
}

#[async_trait]
impl Backend for MemoryBackend {
    fn name(&self) -> &'static str {
        "memory"
    }

    async fn get_item(&self, input: GetItemInput) -> Result<GetItemOutput> {
        self.fault("GetItem", Some(&input.table_name)).await?;
        named("GetItem", items::get_item(&self.state.lock(), &input))
    }

    async fn put_item(&self, input: PutItemInput) -> Result<WriteItemOutput> {
        self.fault("PutItem", Some(&input.table_name)).await?;
        named("PutItem", items::put_item(&mut self.state.lock(), input))
    }

    async fn update_item(&self, input: UpdateItemInput) -> Result<WriteItemOutput> {
        self.fault("UpdateItem", Some(&input.table_name)).await?;
        named("UpdateItem", items::update_item(&mut self.state.lock(), input))
    }

    async fn delete_item(&self, input: DeleteItemInput) -> Result<WriteItemOutput> {
        self.fault("DeleteItem", Some(&input.table_name)).await?;
        named("DeleteItem", items::delete_item(&mut self.state.lock(), input))
    }

    async fn query(&self, input: QueryInput) -> Result<QueryOutput> {
        self.fault("Query", Some(&input.table_name)).await?;
        named("Query", query::query(&self.state.lock(), &input))
    }

    async fn scan(&self, input: ScanInput) -> Result<QueryOutput> {
        self.fault("Scan", Some(&input.table_name)).await?;
        named("Scan", query::scan(&self.state.lock(), &input))
    }

    async fn batch_get_item(&self, input: BatchGetItemInput) -> Result<BatchGetItemOutput> {
        let unprocessed = self.fault("BatchGetItem", input.request_items.keys().next().map(String::as_str)).await?;
        named("BatchGetItem", batch::batch_get_item(&self.state.lock(), &input, unprocessed))
    }

    async fn batch_write_item(&self, input: BatchWriteItemInput) -> Result<BatchWriteItemOutput> {
        let unprocessed = self.fault("BatchWriteItem", input.request_items.keys().next().map(String::as_str)).await?;
        named("BatchWriteItem", batch::batch_write_item(&mut self.state.lock(), input, unprocessed))
    }

    async fn transact_get_items(&self, input: TransactGetItemsInput) -> Result<TransactGetItemsOutput> {
        self.fault("TransactGetItems", input.transact_items.first().map(|get| get.table_name.as_str())).await?;
        named("TransactGetItems", transact::transact_get_items(&self.state.lock(), &input))
    }

    async fn transact_write_items(&self, input: TransactWriteItemsInput) -> Result<TransactWriteItemsOutput> {
        let table = input.transact_items.first().map(|item| item.target().table_name.clone());
        self.fault("TransactWriteItems", table.as_deref()).await?;
        named("TransactWriteItems", transact::transact_write_items(&mut self.state.lock(), input))
    }

    async fn execute_statement(&self, input: ExecuteStatementInput) -> Result<ExecuteStatementOutput> {
        self.fault("ExecuteStatement", None).await?;
        named("ExecuteStatement", partiql::execute_statement(&mut self.state.lock(), &input))
    }

    async fn batch_execute_statement(&self, input: BatchExecuteStatementInput) -> Result<BatchExecuteStatementOutput> {
        self.fault("BatchExecuteStatement", None).await?;
        named("BatchExecuteStatement", partiql::batch_execute_statement(&mut self.state.lock(), &input))
    }

    async fn execute_transaction(&self, input: ExecuteTransactionInput) -> Result<ExecuteTransactionOutput> {
        self.fault("ExecuteTransaction", None).await?;
        named("ExecuteTransaction", partiql::execute_transaction(&mut self.state.lock(), &input))
    }

    async fn create_table(&self, input: CreateTableInput) -> Result<TableDescription> {
        self.fault("CreateTable", Some(&input.table_name)).await?;
        named("CreateTable", admin::create_table(&mut self.state.lock(), input))
    }

    async fn delete_table(&self, table_name: &str) -> Result<TableDescription> {
        self.fault("DeleteTable", Some(table_name)).await?;
        named("DeleteTable", admin::delete_table(&mut self.state.lock(), table_name))
    }

    async fn describe_table(&self, table_name: &str) -> Result<TableDescription> {
        self.fault("DescribeTable", Some(table_name)).await?;
        named("DescribeTable", admin::describe_table(&self.state.lock(), table_name))
    }

    async fn update_table(&self, input: UpdateTableInput) -> Result<TableDescription> {
        self.fault("UpdateTable", Some(&input.table_name)).await?;
        named("UpdateTable", admin::update_table(&mut self.state.lock(), input))
    }

    async fn list_tables(&self, input: ListTablesInput) -> Result<ListTablesOutput> {
        self.fault("ListTables", None).await?;
        named("ListTables", admin::list_tables(&self.state.lock(), &input))
    }

    async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Result<TimeToLiveDescription> {
        self.fault("UpdateTimeToLive", Some(&input.table_name)).await?;
        named("UpdateTimeToLive", admin::update_time_to_live(&mut self.state.lock(), &input))
    }

    async fn describe_time_to_live(&self, table_name: &str) -> Result<TimeToLiveDescription> {
        self.fault("DescribeTimeToLive", Some(table_name)).await?;
        named("DescribeTimeToLive", admin::describe_time_to_live(&self.state.lock(), table_name))
    }

    async fn tag_resource(&self, resource_arn: &str, tags: BTreeMap<String, String>) -> Result<()> {
        self.fault("TagResource", None).await?;
        named("TagResource", admin::tag_resource(&mut self.state.lock(), resource_arn, tags))
    }

    async fn untag_resource(&self, resource_arn: &str, tag_keys: Vec<String>) -> Result<()> {
        self.fault("UntagResource", None).await?;
        named("UntagResource", admin::untag_resource(&mut self.state.lock(), resource_arn, &tag_keys))
    }

    async fn list_tags_of_resource(&self, resource_arn: &str, _next_token: Option<String>) -> Result<ListTagsOutput> {
        self.fault("ListTagsOfResource", None).await?;
        named("ListTagsOfResource", admin::list_tags(&mut self.state.lock(), resource_arn))
    }

    async fn describe_continuous_backups(&self, table_name: &str) -> Result<ContinuousBackupsDescription> {
        self.fault("DescribeContinuousBackups", Some(table_name)).await?;
        named("DescribeContinuousBackups", admin::describe_continuous_backups(&self.state.lock(), table_name))
    }

    async fn update_continuous_backups(
        &self,
        table_name: &str,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> Result<ContinuousBackupsDescription> {
        self.fault("UpdateContinuousBackups", Some(table_name)).await?;
        named(
            "UpdateContinuousBackups",
            admin::update_continuous_backups(&mut self.state.lock(), table_name, enabled, recovery_period_in_days),
        )
    }

    async fn create_backup(&self, table_name: &str, backup_name: &str) -> Result<BackupDetails> {
        self.fault("CreateBackup", Some(table_name)).await?;
        named("CreateBackup", admin::create_backup(&mut self.state.lock(), table_name, backup_name))
    }

    async fn describe_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        self.fault("DescribeBackup", None).await?;
        named("DescribeBackup", admin::describe_backup(&self.state.lock(), backup_arn))
    }

    async fn delete_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        self.fault("DeleteBackup", None).await?;
        named("DeleteBackup", admin::delete_backup(&mut self.state.lock(), backup_arn))
    }

    async fn list_backups(&self, input: ListBackupsInput) -> Result<ListBackupsOutput> {
        self.fault("ListBackups", input.table_name.as_deref()).await?;
        named("ListBackups", admin::list_backups(&self.state.lock(), &input))
    }

    async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Result<TableDescription> {
        self.fault("RestoreTableFromBackup", Some(&input.target_table_name)).await?;
        named("RestoreTableFromBackup", admin::restore_table_from_backup(&mut self.state.lock(), input))
    }

    async fn restore_table_to_point_in_time(&self, input: RestoreTableToPointInTimeInput) -> Result<TableDescription> {
        self.fault("RestoreTableToPointInTime", Some(&input.target_table_name)).await?;
        named("RestoreTableToPointInTime", admin::restore_table_to_point_in_time(&mut self.state.lock(), input))
    }
}
