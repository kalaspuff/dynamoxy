//! Inputs and outputs of every operation, mirroring the DynamoDB API in snake_case.
//!
//! Optional expressions are `None` when absent; empty placeholder maps mean "not given" (the AWS
//! backend omits them, because DynamoDB rejects empty maps).

use std::collections::BTreeMap;

use super::schema::{
    AttributeDefinition, BackupDetails, BackupTypeFilter, BillingMode, ConsumedCapacity, GlobalSecondaryIndex,
    ItemCollectionMetrics, KeySchema, LocalSecondaryIndex, OnDemandThroughput, ProvisionedThroughput, SseSpecification,
    StreamSpecification, TableClass, string_enum,
};
use crate::expr::{Names, Values};
use crate::value::{AttributeValue, Item};

string_enum! {
    /// Whether to return consumed capacity.
    #[derive(Default)]
    pub enum ReturnConsumedCapacity {
        /// Do not return it.
        #[default]
        None => "NONE",
        /// Total per table.
        Total => "TOTAL",
        /// Total plus per index.
        Indexes => "INDEXES",
    }
}

string_enum! {
    /// Which item attributes a write returns.
    #[derive(Default)]
    pub enum ReturnValues {
        /// Nothing.
        #[default]
        None => "NONE",
        /// The whole item before the write.
        AllOld => "ALL_OLD",
        /// Updated attributes before the write (`UpdateItem`).
        UpdatedOld => "UPDATED_OLD",
        /// The whole item after the write (`UpdateItem`).
        AllNew => "ALL_NEW",
        /// Updated attributes after the write (`UpdateItem`).
        UpdatedNew => "UPDATED_NEW",
    }
}

string_enum! {
    /// What a failed condition returns.
    #[derive(Default)]
    pub enum ReturnValuesOnConditionCheckFailure {
        /// Nothing.
        #[default]
        None => "NONE",
        /// The item as it was.
        AllOld => "ALL_OLD",
    }
}

string_enum! {
    /// Which attributes `Query` and `Scan` return.
    pub enum Select {
        /// Every attribute of the item.
        AllAttributes => "ALL_ATTRIBUTES",
        /// Every attribute projected into the index.
        AllProjectedAttributes => "ALL_PROJECTED_ATTRIBUTES",
        /// The attributes of the projection expression.
        SpecificAttributes => "SPECIFIC_ATTRIBUTES",
        /// Only the count.
        Count => "COUNT",
    }
}

/// `GetItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GetItemInput {
    /// Table name.
    pub table_name: String,
    /// Primary key.
    pub key: Item,
    /// Strongly consistent read.
    pub consistent_read: bool,
    /// Attributes to return.
    pub projection_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// `GetItem` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GetItemOutput {
    /// The item, if it exists.
    pub item: Option<Item>,
    /// Consumed capacity.
    pub consumed_capacity: Option<ConsumedCapacity>,
}

/// `PutItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PutItemInput {
    /// Table name.
    pub table_name: String,
    /// The item (including its key).
    pub item: Item,
    /// Condition that must hold.
    pub condition_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// `NONE` or `ALL_OLD`.
    pub return_values: ReturnValues,
    /// What a failed condition returns.
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// Return item collection metrics (`SIZE`).
    pub return_item_collection_metrics: bool,
}

/// Output of `PutItem`, `UpdateItem` and `DeleteItem`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WriteItemOutput {
    /// Returned attributes (`ReturnValues`).
    pub attributes: Option<Item>,
    /// Consumed capacity.
    pub consumed_capacity: Option<ConsumedCapacity>,
    /// Item collection metrics.
    pub item_collection_metrics: Option<ItemCollectionMetrics>,
}

/// `UpdateItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct UpdateItemInput {
    /// Table name.
    pub table_name: String,
    /// Primary key.
    pub key: Item,
    /// The update.
    pub update_expression: Option<String>,
    /// Condition that must hold.
    pub condition_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// What to return.
    pub return_values: ReturnValues,
    /// What a failed condition returns.
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// Return item collection metrics (`SIZE`).
    pub return_item_collection_metrics: bool,
}

/// `DeleteItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DeleteItemInput {
    /// Table name.
    pub table_name: String,
    /// Primary key.
    pub key: Item,
    /// Condition that must hold.
    pub condition_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// `NONE` or `ALL_OLD`.
    pub return_values: ReturnValues,
    /// What a failed condition returns.
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// Return item collection metrics (`SIZE`).
    pub return_item_collection_metrics: bool,
}

/// `Query` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct QueryInput {
    /// Table name.
    pub table_name: String,
    /// Index to query instead of the table.
    pub index_name: Option<String>,
    /// Partition key equality and optional sort key condition.
    pub key_condition_expression: Option<String>,
    /// Filter applied after reading.
    pub filter_expression: Option<String>,
    /// Attributes to return.
    pub projection_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// Which attributes to return.
    pub select: Option<Select>,
    /// Maximum number of items to evaluate.
    pub limit: Option<u32>,
    /// Strongly consistent read (not on global secondary indexes).
    pub consistent_read: bool,
    /// Ascending sort key order (default) or descending.
    pub scan_index_forward: bool,
    /// Continue after this key (`LastEvaluatedKey` of the previous page).
    pub exclusive_start_key: Option<Item>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// `Scan` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ScanInput {
    /// Table name.
    pub table_name: String,
    /// Index to scan instead of the table.
    pub index_name: Option<String>,
    /// Filter applied after reading.
    pub filter_expression: Option<String>,
    /// Attributes to return.
    pub projection_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// Which attributes to return.
    pub select: Option<Select>,
    /// Maximum number of items to evaluate.
    pub limit: Option<u32>,
    /// Strongly consistent read (not on global secondary indexes).
    pub consistent_read: bool,
    /// Continue after this key.
    pub exclusive_start_key: Option<Item>,
    /// Segment of a parallel scan.
    pub segment: Option<u32>,
    /// Number of segments of a parallel scan.
    pub total_segments: Option<u32>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// Output of `Query` and `Scan`: one page.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct QueryOutput {
    /// Items that matched (empty with `Select=COUNT`).
    pub items: Vec<Item>,
    /// Number of items that matched the filter.
    pub count: u32,
    /// Number of items evaluated before the filter.
    pub scanned_count: u32,
    /// Where the next page starts; `None` on the last page.
    pub last_evaluated_key: Option<Item>,
    /// Consumed capacity.
    pub consumed_capacity: Option<ConsumedCapacity>,
}

/// Keys to read from one table with `BatchGetItem`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct KeysAndAttributes {
    /// Primary keys.
    pub keys: Vec<Item>,
    /// Strongly consistent reads.
    pub consistent_read: bool,
    /// Attributes to return.
    pub projection_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
}

/// `BatchGetItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchGetItemInput {
    /// Keys per table (at most 100 keys in total).
    pub request_items: BTreeMap<String, KeysAndAttributes>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// `BatchGetItem` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchGetItemOutput {
    /// Items found, per table (in no particular order).
    pub responses: BTreeMap<String, Vec<Item>>,
    /// Keys that were not processed (retry them).
    pub unprocessed_keys: BTreeMap<String, KeysAndAttributes>,
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// One write of `BatchWriteItem`.
#[derive(Clone, Debug, PartialEq)]
pub enum WriteRequest {
    /// Put an item.
    Put(Item),
    /// Delete the item with this key.
    Delete(Item),
}

/// `BatchWriteItem` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchWriteItemInput {
    /// Writes per table (at most 25 in total).
    pub request_items: BTreeMap<String, Vec<WriteRequest>>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// Return item collection metrics (`SIZE`).
    pub return_item_collection_metrics: bool,
}

/// `BatchWriteItem` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchWriteItemOutput {
    /// Writes that were not processed (retry them).
    pub unprocessed_items: BTreeMap<String, Vec<WriteRequest>>,
    /// Item collection metrics per table.
    pub item_collection_metrics: BTreeMap<String, Vec<ItemCollectionMetrics>>,
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// One read of `TransactGetItems`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactGet {
    /// Table name.
    pub table_name: String,
    /// Primary key.
    pub key: Item,
    /// Attributes to return.
    pub projection_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
}

/// `TransactGetItems` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactGetItemsInput {
    /// Reads (at most 100).
    pub transact_items: Vec<TransactGet>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// `TransactGetItems` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactGetItemsOutput {
    /// One entry per read, in order (`None` when the item does not exist).
    pub responses: Vec<Option<Item>>,
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// Fields shared by every operation of `TransactWriteItems`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactTarget {
    /// Table name.
    pub table_name: String,
    /// Condition that must hold (required for condition checks).
    pub condition_expression: Option<String>,
    /// Name placeholders.
    pub expression_attribute_names: Names,
    /// Value placeholders.
    pub expression_attribute_values: Values,
    /// What a failed condition returns (in the cancellation reason).
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
}

/// One operation of `TransactWriteItems`.
#[derive(Clone, Debug, PartialEq)]
pub enum TransactWriteItem {
    /// Check a condition without writing.
    ConditionCheck {
        /// Table, condition and placeholders.
        target: TransactTarget,
        /// Primary key.
        key: Item,
    },
    /// Put an item.
    Put {
        /// Table, condition and placeholders.
        target: TransactTarget,
        /// The item.
        item: Item,
    },
    /// Delete an item.
    Delete {
        /// Table, condition and placeholders.
        target: TransactTarget,
        /// Primary key.
        key: Item,
    },
    /// Update an item.
    Update {
        /// Table, condition and placeholders.
        target: TransactTarget,
        /// Primary key.
        key: Item,
        /// The update.
        update_expression: String,
    },
}

impl TransactWriteItem {
    /// Table, condition and placeholders.
    pub fn target(&self) -> &TransactTarget {
        match self {
            Self::ConditionCheck { target, .. }
            | Self::Put { target, .. }
            | Self::Delete { target, .. }
            | Self::Update { target, .. } => target,
        }
    }

    /// The operation name (`Put`, `Update`, `Delete`, `ConditionCheck`).
    pub fn kind(&self) -> &'static str {
        match self {
            Self::ConditionCheck { .. } => "ConditionCheck",
            Self::Put { .. } => "Put",
            Self::Delete { .. } => "Delete",
            Self::Update { .. } => "Update",
        }
    }
}

/// `TransactWriteItems` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactWriteItemsInput {
    /// Operations (at most 100), applied all or nothing.
    pub transact_items: Vec<TransactWriteItem>,
    /// Idempotency token (valid for 10 minutes).
    pub client_request_token: Option<String>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// Return item collection metrics (`SIZE`).
    pub return_item_collection_metrics: bool,
}

/// `TransactWriteItems` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TransactWriteItemsOutput {
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
    /// Item collection metrics per table.
    pub item_collection_metrics: BTreeMap<String, Vec<ItemCollectionMetrics>>,
}

/// `ExecuteStatement` (PartiQL) input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExecuteStatementInput {
    /// The PartiQL statement.
    pub statement: String,
    /// Values for `?` parameters, in order.
    pub parameters: Vec<AttributeValue>,
    /// Strongly consistent read.
    pub consistent_read: bool,
    /// Continue a previous `SELECT`.
    pub next_token: Option<String>,
    /// Maximum number of items to evaluate.
    pub limit: Option<u32>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
    /// What a failed condition returns.
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
}

/// `ExecuteStatement` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExecuteStatementOutput {
    /// Items read (for `SELECT`).
    pub items: Vec<Item>,
    /// Token for the next page.
    pub next_token: Option<String>,
    /// Last key evaluated.
    pub last_evaluated_key: Option<Item>,
    /// Consumed capacity.
    pub consumed_capacity: Option<ConsumedCapacity>,
}

/// One statement of `BatchExecuteStatement` or `ExecuteTransaction`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ParameterizedStatement {
    /// The PartiQL statement.
    pub statement: String,
    /// Values for `?` parameters.
    pub parameters: Vec<AttributeValue>,
    /// Strongly consistent read (`BatchExecuteStatement` only).
    pub consistent_read: bool,
    /// What a failed condition returns.
    pub return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure,
}

/// `BatchExecuteStatement` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchExecuteStatementInput {
    /// Statements (at most 25; all reads or all writes).
    pub statements: Vec<ParameterizedStatement>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// The error of one statement of `BatchExecuteStatement`.
#[derive(Clone, Debug, PartialEq)]
pub struct BatchStatementError {
    /// `ConditionalCheckFailed`, `DuplicateItem`, `ValidationError`, ...
    pub code: String,
    /// Details.
    pub message: Option<String>,
    /// The item, when a condition failed and `ALL_OLD` was requested.
    pub item: Option<Item>,
}

/// The result of one statement of `BatchExecuteStatement`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchStatementResponse {
    /// The error, if the statement failed.
    pub error: Option<BatchStatementError>,
    /// Table the statement addressed.
    pub table_name: Option<String>,
    /// Item read (for `SELECT`).
    pub item: Option<Item>,
}

/// `BatchExecuteStatement` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatchExecuteStatementOutput {
    /// One response per statement, in order.
    pub responses: Vec<BatchStatementResponse>,
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// `ExecuteTransaction` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExecuteTransactionInput {
    /// Statements (at most 100; all reads or all writes), applied all or nothing.
    pub transact_statements: Vec<ParameterizedStatement>,
    /// Idempotency token.
    pub client_request_token: Option<String>,
    /// Consumed capacity reporting.
    pub return_consumed_capacity: ReturnConsumedCapacity,
}

/// `ExecuteTransaction` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExecuteTransactionOutput {
    /// Items read, one per statement (reads only).
    pub responses: Vec<Option<Item>>,
    /// Consumed capacity per table.
    pub consumed_capacity: Vec<ConsumedCapacity>,
}

/// `CreateTable` input.
#[derive(Clone, Debug, PartialEq)]
pub struct CreateTableInput {
    /// Table name (3-255 characters: letters, digits, `_`, `-`, `.`).
    pub table_name: String,
    /// Types of every key attribute of the table and its indexes (and nothing else).
    pub attribute_definitions: Vec<AttributeDefinition>,
    /// Primary key.
    pub key_schema: KeySchema,
    /// Local secondary indexes (only at creation; at most 5).
    pub local_secondary_indexes: Vec<LocalSecondaryIndex>,
    /// Global secondary indexes (at most 20).
    pub global_secondary_indexes: Vec<GlobalSecondaryIndex>,
    /// Billing mode.
    pub billing_mode: BillingMode,
    /// Capacity (required for provisioned tables).
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// Maximum on-demand throughput.
    pub on_demand_throughput: Option<OnDemandThroughput>,
    /// DynamoDB Streams.
    pub stream_specification: Option<StreamSpecification>,
    /// Encryption.
    pub sse_specification: Option<SseSpecification>,
    /// Tags.
    pub tags: BTreeMap<String, String>,
    /// Storage class.
    pub table_class: Option<TableClass>,
    /// Deletion protection.
    pub deletion_protection_enabled: bool,
}

/// A change to the global secondary indexes of a table (`UpdateTable`).
#[derive(Clone, Debug, PartialEq)]
pub enum GlobalSecondaryIndexUpdate {
    /// Add an index (needs attribute definitions for its keys).
    Create(GlobalSecondaryIndex),
    /// Change an index's throughput.
    Update {
        /// Index name.
        index_name: String,
        /// New capacity.
        provisioned_throughput: Option<ProvisionedThroughput>,
        /// New on-demand limits.
        on_demand_throughput: Option<OnDemandThroughput>,
    },
    /// Remove an index.
    Delete {
        /// Index name.
        index_name: String,
    },
}

/// `UpdateTable` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct UpdateTableInput {
    /// Table name.
    pub table_name: String,
    /// Key attribute types for indexes being created.
    pub attribute_definitions: Vec<AttributeDefinition>,
    /// New billing mode.
    pub billing_mode: Option<BillingMode>,
    /// New capacity.
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// New on-demand limits.
    pub on_demand_throughput: Option<OnDemandThroughput>,
    /// Index changes (one per request in DynamoDB).
    pub global_secondary_index_updates: Vec<GlobalSecondaryIndexUpdate>,
    /// New stream settings.
    pub stream_specification: Option<StreamSpecification>,
    /// New encryption settings.
    pub sse_specification: Option<SseSpecification>,
    /// New storage class.
    pub table_class: Option<TableClass>,
    /// New deletion protection setting.
    pub deletion_protection_enabled: Option<bool>,
}

/// `ListTables` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ListTablesInput {
    /// Continue after this table name.
    pub exclusive_start_table_name: Option<String>,
    /// Maximum number of names (1-100).
    pub limit: Option<u32>,
}

/// `ListTables` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ListTablesOutput {
    /// Table names, sorted.
    pub table_names: Vec<String>,
    /// Where the next page starts.
    pub last_evaluated_table_name: Option<String>,
}

/// `UpdateTimeToLive` input.
#[derive(Clone, Debug, PartialEq)]
pub struct UpdateTimeToLiveInput {
    /// Table name.
    pub table_name: String,
    /// Enable or disable.
    pub enabled: bool,
    /// The TTL attribute (a number of seconds since the Unix epoch).
    pub attribute_name: String,
}

/// `ListTagsOfResource` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ListTagsOutput {
    /// Tags.
    pub tags: BTreeMap<String, String>,
    /// Token for the next page.
    pub next_token: Option<String>,
}

/// `ListBackups` input.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ListBackupsInput {
    /// Only backups of this table.
    pub table_name: Option<String>,
    /// Maximum number of backups.
    pub limit: Option<u32>,
    /// Continue after this backup ARN.
    pub exclusive_start_backup_arn: Option<String>,
    /// Which backups.
    pub backup_type: Option<BackupTypeFilter>,
    /// Created at or after (seconds since the Unix epoch).
    pub time_range_lower_bound: Option<f64>,
    /// Created before (seconds since the Unix epoch).
    pub time_range_upper_bound: Option<f64>,
}

/// One backup in `ListBackups`.
#[derive(Clone, Debug, PartialEq)]
pub struct BackupSummary {
    /// The backup.
    pub details: BackupDetails,
    /// Source table name.
    pub table_name: String,
    /// Source table ARN.
    pub table_arn: Option<String>,
}

/// `ListBackups` output.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ListBackupsOutput {
    /// Backups.
    pub backups: Vec<BackupSummary>,
    /// Where the next page starts.
    pub last_evaluated_backup_arn: Option<String>,
}

/// Settings of a table restored from a backup or a point in time.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RestoreOverrides {
    /// Billing mode of the new table.
    pub billing_mode: Option<BillingMode>,
    /// Capacity of the new table.
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// Encryption of the new table.
    pub sse_specification: Option<SseSpecification>,
}

/// `RestoreTableFromBackup` input.
#[derive(Clone, Debug, PartialEq)]
pub struct RestoreTableFromBackupInput {
    /// Name of the new table.
    pub target_table_name: String,
    /// Backup to restore.
    pub backup_arn: String,
    /// Settings of the new table.
    pub overrides: RestoreOverrides,
}

/// `RestoreTableToPointInTime` input.
#[derive(Clone, Debug, PartialEq)]
pub struct RestoreTableToPointInTimeInput {
    /// Table to restore.
    pub source_table_name: String,
    /// Name of the new table.
    pub target_table_name: String,
    /// Restore to the latest restorable time.
    pub use_latest_restorable_time: bool,
    /// Restore to this time (seconds since the Unix epoch).
    pub restore_date_time: Option<f64>,
    /// Settings of the new table.
    pub overrides: RestoreOverrides,
}
