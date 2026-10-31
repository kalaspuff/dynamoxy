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
pub(crate) mod partiql;
mod query;
mod table;
#[cfg(test)]
mod tests;
mod transact;

use std::collections::{BTreeMap, HashMap, VecDeque};
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
    /// The table and item size each operation's capacity was computed from, which a replay
    /// with the token is billed for as a transactional read (`TransactWriteItems`).
    sizes: Vec<(String, usize)>,
}

/// How far in the past a TTL may be for DynamoDB to delete the item: five years (of 365 days).
pub(crate) const TTL_MAX_AGE: f64 = 5.0 * 365.0 * 86_400.0;

/// How long a client request token is remembered (10 minutes).
pub(crate) const TOKEN_LIFETIME: f64 = 600.0;

/// Client request tokens of the last [`TOKEN_LIFETIME`] seconds. Expired tokens are forgotten in
/// arrival order, so each transaction does work proportional to what expired, not to every
/// token remembered.
#[derive(Debug, Default)]
pub(crate) struct Tokens {
    records: HashMap<String, TokenRecord>,
    arrivals: VecDeque<(f64, String)>,
}

impl Tokens {
    /// Forgets the tokens that expired at `now`.
    fn expire(&mut self, now: f64) {
        while let Some((at, _)) = self.arrivals.front()
            && now - *at >= TOKEN_LIFETIME
        {
            if let Some((at, token)) = self.arrivals.pop_front()
                && self.records.get(&token).is_some_and(|record| record.at == at)
            {
                self.records.remove(&token);
            }
        }
    }

    /// The record of `token` if it has not expired at `now`.
    fn get(&mut self, token: &str, now: f64) -> Option<&TokenRecord> {
        self.expire(now);
        // Arrivals are in clock order unless the clock went back: check the age too.
        self.records.get(token).filter(|record| now - record.at < TOKEN_LIFETIME)
    }

    fn insert(&mut self, token: String, record: TokenRecord) {
        self.arrivals.push_back((record.at, token.clone()));
        self.records.insert(token, record);
    }

    fn remove(&mut self, token: &str) {
        self.records.remove(token);
    }

    fn clear(&mut self) {
        self.records.clear();
        self.arrivals.clear();
    }
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
    /// `BatchGetItem` / `BatchWriteItem` return requests unprocessed, and do not write them: those of
    /// the fault's table when it has one (the others are processed), else every request.
    Unprocessed,
    /// Delay the operation.
    Latency(Duration),
    /// Run the operation, then lose its response: the caller sees a transport error (as when a
    /// connection drops), although the operation was applied.
    LoseResponse,
}

/// What a triggered fault leaves to do once the operation runs.
#[derive(Clone, Debug, Default)]
struct Triggered {
    /// Batch operations return requests unprocessed (nothing of them is written): those of this
    /// table (`Some(Some(table))`), or every one (`Some(None)`).
    unprocessed: Option<Option<String>>,
    /// The operation runs, and its response is lost.
    lose_response: bool,
}

impl Triggered {
    /// The operation's result, or a lost response.
    fn deliver<T>(&self, operation: &'static str, result: Result<T>) -> Result<T> {
        if self.lose_response {
            return Err(Error::Transport {
                operation,
                message: "the connection closed before the response arrived (injected fault)".into(),
                retryable: true,
            });
        }
        result
    }
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
    tokens: Tokens,
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
                tokens: Tokens::default(),
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
    /// this to make that happen deterministically. Like DynamoDB, it leaves items whose TTL is
    /// more than five years before `at` (`0`, or a date decades ago): "the TTL can't be more than
    /// five years in the past" to be considered for expiry.
    pub fn purge_expired(&self, at: Option<f64>) -> usize {
        let at = at.unwrap_or_else(now);
        let oldest = at - TTL_MAX_AGE;
        let mut state = self.state.lock();
        let mut deleted = 0;
        for table in state.tables.values_mut() {
            let Some(attribute) = table.ttl_attribute.clone() else { continue };
            let expired: Vec<_> = table
                .items
                .iter()
                .filter(|(_, item)| match item.get(&attribute) {
                    Some(AttributeValue::N(number)) => {
                        number.as_str().parse::<f64>().is_ok_and(|expiry| oldest <= expiry && expiry <= at)
                    }
                    _ => false,
                })
                .map(|(key, _)| key.clone())
                .collect();
            for key in expired {
                table.write(&key, None);
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

    /// Applies a matching fault: fails or sleeps now, or returns what to do once the operation ran.
    /// A fault for a table matches a call that reads or writes that table, among others.
    async fn fault(&self, operation: &'static str, tables: &[&str]) -> Result<Triggered> {
        let triggered = {
            let mut state = self.state.lock();
            let position = state.faults.iter().position(|fault| {
                fault.times > 0
                    && fault.operation.as_deref().is_none_or(|name| name == operation)
                    && fault.table.as_deref().is_none_or(|name| tables.contains(&name))
            });
            position.map(|position| {
                let fault = &mut state.faults[position];
                fault.times -= 1;
                let triggered = (fault.kind.clone(), fault.table.clone());
                if fault.times == 0 {
                    state.faults.remove(position);
                }
                triggered
            })
        };
        let Some((kind, table)) = triggered else { return Ok(Triggered::default()) };
        match kind {
            FaultKind::Error { code, message } => {
                let mut error = ServiceError::new(code, message).with_operation(operation);
                if error.kind.is_retryable() {
                    error.status = Some(if error.kind == crate::error::ErrorKind::Internal { 500 } else { 400 });
                }
                Err(error.into())
            }
            // Only the requests of the fault's table, when it has one.
            FaultKind::Unprocessed => Ok(Triggered { unprocessed: Some(table), ..Triggered::default() }),
            FaultKind::Latency(delay) => {
                tokio::time::sleep(delay).await;
                Ok(Triggered::default())
            }
            FaultKind::LoseResponse => Ok(Triggered { lose_response: true, ..Triggered::default() }),
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
        let fault = self.fault("GetItem", &[&input.table_name]).await?;
        fault.deliver("GetItem", named("GetItem", items::get_item(&self.state.lock(), &input)))
    }

    async fn put_item(&self, input: PutItemInput) -> Result<WriteItemOutput> {
        let fault = self.fault("PutItem", &[&input.table_name]).await?;
        fault.deliver("PutItem", named("PutItem", items::put_item(&mut self.state.lock(), input)))
    }

    async fn update_item(&self, input: UpdateItemInput) -> Result<WriteItemOutput> {
        let fault = self.fault("UpdateItem", &[&input.table_name]).await?;
        fault.deliver("UpdateItem", named("UpdateItem", items::update_item(&mut self.state.lock(), input)))
    }

    async fn delete_item(&self, input: DeleteItemInput) -> Result<WriteItemOutput> {
        let fault = self.fault("DeleteItem", &[&input.table_name]).await?;
        fault.deliver("DeleteItem", named("DeleteItem", items::delete_item(&mut self.state.lock(), input)))
    }

    async fn query(&self, input: QueryInput) -> Result<QueryOutput> {
        let fault = self.fault("Query", &[&input.table_name]).await?;
        fault.deliver("Query", named("Query", query::query(&self.state.lock(), &input)))
    }

    async fn scan(&self, input: ScanInput) -> Result<QueryOutput> {
        let fault = self.fault("Scan", &[&input.table_name]).await?;
        fault.deliver("Scan", named("Scan", query::scan(&self.state.lock(), &input)))
    }

    async fn batch_get_item(&self, input: BatchGetItemInput) -> Result<BatchGetItemOutput> {
        let tables: Vec<&str> = input.request_items.keys().map(String::as_str).collect();
        let fault = self.fault("BatchGetItem", &tables).await?;
        let unprocessed = fault.unprocessed.as_ref().map(Option::as_deref);
        fault.deliver(
            "BatchGetItem",
            named("BatchGetItem", batch::batch_get_item(&self.state.lock(), &input, unprocessed)),
        )
    }

    async fn batch_write_item(&self, input: BatchWriteItemInput) -> Result<BatchWriteItemOutput> {
        let tables: Vec<&str> = input.request_items.keys().map(String::as_str).collect();
        let fault = self.fault("BatchWriteItem", &tables).await?;
        let unprocessed = fault.unprocessed.clone();
        fault.deliver(
            "BatchWriteItem",
            named(
                "BatchWriteItem",
                batch::batch_write_item(&mut self.state.lock(), input, unprocessed.as_ref().map(Option::as_deref)),
            ),
        )
    }

    async fn transact_get_items(&self, input: TransactGetItemsInput) -> Result<TransactGetItemsOutput> {
        let tables: Vec<&str> = input.transact_items.iter().map(|get| get.table_name.as_str()).collect();
        let fault = self.fault("TransactGetItems", &tables).await?;
        fault.deliver(
            "TransactGetItems",
            named("TransactGetItems", transact::transact_get_items(&self.state.lock(), &input)),
        )
    }

    async fn transact_write_items(&self, input: TransactWriteItemsInput) -> Result<TransactWriteItemsOutput> {
        let tables: Vec<&str> = input.transact_items.iter().map(|item| item.target().table_name.as_str()).collect();
        let fault = self.fault("TransactWriteItems", &tables).await?;
        fault.deliver(
            "TransactWriteItems",
            named("TransactWriteItems", transact::transact_write_items(&mut self.state.lock(), input)),
        )
    }

    async fn execute_statement(&self, input: ExecuteStatementInput) -> Result<ExecuteStatementOutput> {
        let tables = partiql::statement_tables([(&input.statement, &input.parameters)]);
        let fault = self.fault("ExecuteStatement", &tables.iter().map(String::as_str).collect::<Vec<_>>()).await?;
        fault.deliver(
            "ExecuteStatement",
            named("ExecuteStatement", partiql::execute_statement(&mut self.state.lock(), &input)),
        )
    }

    async fn batch_execute_statement(&self, input: BatchExecuteStatementInput) -> Result<BatchExecuteStatementOutput> {
        let tables = partiql::statement_tables(
            input.statements.iter().map(|statement| (&statement.statement, &statement.parameters)),
        );
        let fault = self.fault("BatchExecuteStatement", &tables.iter().map(String::as_str).collect::<Vec<_>>()).await?;
        fault.deliver(
            "BatchExecuteStatement",
            named("BatchExecuteStatement", partiql::batch_execute_statement(&mut self.state.lock(), &input)),
        )
    }

    async fn execute_transaction(&self, input: ExecuteTransactionInput) -> Result<ExecuteTransactionOutput> {
        let tables = partiql::statement_tables(
            input.transact_statements.iter().map(|statement| (&statement.statement, &statement.parameters)),
        );
        let fault = self.fault("ExecuteTransaction", &tables.iter().map(String::as_str).collect::<Vec<_>>()).await?;
        fault.deliver(
            "ExecuteTransaction",
            named("ExecuteTransaction", partiql::execute_transaction(&mut self.state.lock(), &input)),
        )
    }

    async fn create_table(&self, input: CreateTableInput) -> Result<TableDescription> {
        let fault = self.fault("CreateTable", &[&input.table_name]).await?;
        fault.deliver("CreateTable", named("CreateTable", admin::create_table(&mut self.state.lock(), input)))
    }

    async fn delete_table(&self, table_name: &str) -> Result<TableDescription> {
        let fault = self.fault("DeleteTable", &[table_name]).await?;
        fault.deliver("DeleteTable", named("DeleteTable", admin::delete_table(&mut self.state.lock(), table_name)))
    }

    async fn describe_table(&self, table_name: &str) -> Result<TableDescription> {
        let fault = self.fault("DescribeTable", &[table_name]).await?;
        fault.deliver("DescribeTable", named("DescribeTable", admin::describe_table(&self.state.lock(), table_name)))
    }

    async fn update_table(&self, input: UpdateTableInput) -> Result<TableDescription> {
        let fault = self.fault("UpdateTable", &[&input.table_name]).await?;
        fault.deliver("UpdateTable", named("UpdateTable", admin::update_table(&mut self.state.lock(), input)))
    }

    async fn list_tables(&self, input: ListTablesInput) -> Result<ListTablesOutput> {
        let fault = self.fault("ListTables", &[]).await?;
        fault.deliver("ListTables", named("ListTables", admin::list_tables(&self.state.lock(), &input)))
    }

    async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Result<TimeToLiveDescription> {
        let fault = self.fault("UpdateTimeToLive", &[&input.table_name]).await?;
        fault.deliver(
            "UpdateTimeToLive",
            named("UpdateTimeToLive", admin::update_time_to_live(&mut self.state.lock(), &input)),
        )
    }

    async fn describe_time_to_live(&self, table_name: &str) -> Result<TimeToLiveDescription> {
        let fault = self.fault("DescribeTimeToLive", &[table_name]).await?;
        fault.deliver(
            "DescribeTimeToLive",
            named("DescribeTimeToLive", admin::describe_time_to_live(&self.state.lock(), table_name)),
        )
    }

    async fn tag_resource(&self, resource_arn: &str, tags: BTreeMap<String, String>) -> Result<()> {
        let fault = self.fault("TagResource", &[]).await?;
        fault.deliver(
            "TagResource",
            named("TagResource", admin::tag_resource(&mut self.state.lock(), resource_arn, tags)),
        )
    }

    async fn untag_resource(&self, resource_arn: &str, tag_keys: Vec<String>) -> Result<()> {
        let fault = self.fault("UntagResource", &[]).await?;
        fault.deliver(
            "UntagResource",
            named("UntagResource", admin::untag_resource(&mut self.state.lock(), resource_arn, &tag_keys)),
        )
    }

    async fn list_tags_of_resource(&self, resource_arn: &str, _next_token: Option<String>) -> Result<ListTagsOutput> {
        let fault = self.fault("ListTagsOfResource", &[]).await?;
        fault.deliver(
            "ListTagsOfResource",
            named("ListTagsOfResource", admin::list_tags(&mut self.state.lock(), resource_arn)),
        )
    }

    async fn describe_continuous_backups(&self, table_name: &str) -> Result<ContinuousBackupsDescription> {
        let fault = self.fault("DescribeContinuousBackups", &[table_name]).await?;
        fault.deliver(
            "DescribeContinuousBackups",
            named("DescribeContinuousBackups", admin::describe_continuous_backups(&self.state.lock(), table_name)),
        )
    }

    async fn update_continuous_backups(
        &self,
        table_name: &str,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> Result<ContinuousBackupsDescription> {
        let fault = self.fault("UpdateContinuousBackups", &[table_name]).await?;
        fault.deliver(
            "UpdateContinuousBackups",
            named(
                "UpdateContinuousBackups",
                admin::update_continuous_backups(&mut self.state.lock(), table_name, enabled, recovery_period_in_days),
            ),
        )
    }

    async fn create_backup(&self, table_name: &str, backup_name: &str) -> Result<BackupDetails> {
        let fault = self.fault("CreateBackup", &[table_name]).await?;
        fault.deliver(
            "CreateBackup",
            named("CreateBackup", admin::create_backup(&mut self.state.lock(), table_name, backup_name)),
        )
    }

    async fn describe_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        let fault = self.fault("DescribeBackup", &[]).await?;
        fault.deliver("DescribeBackup", named("DescribeBackup", admin::describe_backup(&self.state.lock(), backup_arn)))
    }

    async fn delete_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        let fault = self.fault("DeleteBackup", &[]).await?;
        fault.deliver("DeleteBackup", named("DeleteBackup", admin::delete_backup(&mut self.state.lock(), backup_arn)))
    }

    async fn list_backups(&self, input: ListBackupsInput) -> Result<ListBackupsOutput> {
        let tables: Vec<&str> = input.table_name.iter().map(String::as_str).collect();
        let fault = self.fault("ListBackups", &tables).await?;
        fault.deliver("ListBackups", named("ListBackups", admin::list_backups(&self.state.lock(), &input)))
    }

    async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Result<TableDescription> {
        let fault = self.fault("RestoreTableFromBackup", &[&input.target_table_name]).await?;
        fault.deliver(
            "RestoreTableFromBackup",
            named("RestoreTableFromBackup", admin::restore_table_from_backup(&mut self.state.lock(), input)),
        )
    }

    async fn restore_table_to_point_in_time(&self, input: RestoreTableToPointInTimeInput) -> Result<TableDescription> {
        let fault = self.fault("RestoreTableToPointInTime", &[&input.target_table_name]).await?;
        fault.deliver(
            "RestoreTableToPointInTime",
            named("RestoreTableToPointInTime", admin::restore_table_to_point_in_time(&mut self.state.lock(), input)),
        )
    }
}
