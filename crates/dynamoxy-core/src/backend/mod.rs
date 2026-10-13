//! Ports: the operations the core needs from DynamoDB, in dynamoxy's own types (SDK types
//! never cross this boundary).
//!
//! * [`memory`]: an in-process DynamoDB with the same semantics, limits, error codes and
//!   messages, for fast deterministic tests (Rust and Python) and for users' tests.
//! * [`aws`]: the real service through the AWS SDK for Rust (also used for DynamoDB-compatible
//!   endpoints such as MiniStack or DynamoDB Local).
//!
//! Adding an operation: add it to [`Backend`], implement it in both adapters, test it against
//! both (`backend/memory/tests.rs`, `tests/emulator.rs`, and the Python integration suite).

pub mod aws;
pub mod memory;

use std::collections::BTreeMap;

use async_trait::async_trait;

use crate::error::Result;
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

/// Every DynamoDB operation dynamoxy uses.
///
/// Implementations return [`crate::Error::Service`] for errors the service reports (with the
/// DynamoDB error code), and never retry: retries, timeouts, events and metrics are the
/// [`crate::Client`]'s job.
#[async_trait]
pub trait Backend: Send + Sync + std::fmt::Debug {
    /// Short name for diagnostics (`"memory"`, `"aws"`).
    fn name(&self) -> &'static str;

    /// `GetItem`.
    async fn get_item(&self, input: GetItemInput) -> Result<GetItemOutput>;
    /// `PutItem`.
    async fn put_item(&self, input: PutItemInput) -> Result<WriteItemOutput>;
    /// `UpdateItem`.
    async fn update_item(&self, input: UpdateItemInput) -> Result<WriteItemOutput>;
    /// `DeleteItem`.
    async fn delete_item(&self, input: DeleteItemInput) -> Result<WriteItemOutput>;
    /// `Query` (one page).
    async fn query(&self, input: QueryInput) -> Result<QueryOutput>;
    /// `Scan` (one page).
    async fn scan(&self, input: ScanInput) -> Result<QueryOutput>;
    /// `BatchGetItem` (one request: at most 100 keys).
    async fn batch_get_item(&self, input: BatchGetItemInput) -> Result<BatchGetItemOutput>;
    /// `BatchWriteItem` (one request: at most 25 writes).
    async fn batch_write_item(&self, input: BatchWriteItemInput) -> Result<BatchWriteItemOutput>;
    /// `TransactGetItems`.
    async fn transact_get_items(&self, input: TransactGetItemsInput) -> Result<TransactGetItemsOutput>;
    /// `TransactWriteItems`.
    async fn transact_write_items(&self, input: TransactWriteItemsInput) -> Result<TransactWriteItemsOutput>;
    /// `ExecuteStatement` (PartiQL).
    async fn execute_statement(&self, input: ExecuteStatementInput) -> Result<ExecuteStatementOutput>;
    /// `BatchExecuteStatement` (PartiQL).
    async fn batch_execute_statement(&self, input: BatchExecuteStatementInput) -> Result<BatchExecuteStatementOutput>;
    /// `ExecuteTransaction` (PartiQL).
    async fn execute_transaction(&self, input: ExecuteTransactionInput) -> Result<ExecuteTransactionOutput>;

    /// `CreateTable`.
    async fn create_table(&self, input: CreateTableInput) -> Result<TableDescription>;
    /// `DeleteTable`.
    async fn delete_table(&self, table_name: &str) -> Result<TableDescription>;
    /// `DescribeTable`.
    async fn describe_table(&self, table_name: &str) -> Result<TableDescription>;
    /// `UpdateTable`.
    async fn update_table(&self, input: UpdateTableInput) -> Result<TableDescription>;
    /// `ListTables` (one page).
    async fn list_tables(&self, input: ListTablesInput) -> Result<ListTablesOutput>;
    /// `UpdateTimeToLive`; returns the new state.
    async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Result<TimeToLiveDescription>;
    /// `DescribeTimeToLive`.
    async fn describe_time_to_live(&self, table_name: &str) -> Result<TimeToLiveDescription>;
    /// `TagResource`.
    async fn tag_resource(&self, resource_arn: &str, tags: BTreeMap<String, String>) -> Result<()>;
    /// `UntagResource`.
    async fn untag_resource(&self, resource_arn: &str, tag_keys: Vec<String>) -> Result<()>;
    /// `ListTagsOfResource` (one page).
    async fn list_tags_of_resource(&self, resource_arn: &str, next_token: Option<String>) -> Result<ListTagsOutput>;
    /// `DescribeContinuousBackups`.
    async fn describe_continuous_backups(&self, table_name: &str) -> Result<ContinuousBackupsDescription>;
    /// `UpdateContinuousBackups` (point-in-time recovery).
    async fn update_continuous_backups(
        &self,
        table_name: &str,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> Result<ContinuousBackupsDescription>;
    /// `CreateBackup`.
    async fn create_backup(&self, table_name: &str, backup_name: &str) -> Result<BackupDetails>;
    /// `DescribeBackup`.
    async fn describe_backup(&self, backup_arn: &str) -> Result<BackupDescription>;
    /// `DeleteBackup`.
    async fn delete_backup(&self, backup_arn: &str) -> Result<BackupDescription>;
    /// `ListBackups` (one page).
    async fn list_backups(&self, input: ListBackupsInput) -> Result<ListBackupsOutput>;
    /// `RestoreTableFromBackup`.
    async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Result<TableDescription>;
    /// `RestoreTableToPointInTime`.
    async fn restore_table_to_point_in_time(&self, input: RestoreTableToPointInTimeInput) -> Result<TableDescription>;
}
