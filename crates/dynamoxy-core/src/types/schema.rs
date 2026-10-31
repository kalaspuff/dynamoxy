//! Table, index, TTL, backup and capacity types (DynamoDB's control-plane shapes).

use std::collections::BTreeMap;

use crate::value::{Item, ValueType};

/// Defines an enum of DynamoDB string constants with `ALL`, `as_str` and `parse`.
macro_rules! string_enum {
    ($(#[$meta:meta])* pub enum $name:ident { $($(#[$vmeta:meta])* $variant:ident => $text:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum $name { $($(#[$vmeta])* $variant),+ }

        impl $name {
            /// Every value, in declaration order.
            pub const ALL: &'static [$name] = &[$($name::$variant),+];

            /// DynamoDB's spelling (`"PAY_PER_REQUEST"`, ...).
            pub fn as_str(self) -> &'static str {
                match self { $($name::$variant => $text),+ }
            }

            /// Parses DynamoDB's spelling.
            pub fn parse(text: &str) -> Option<Self> {
                match text { $($text => Some($name::$variant),)+ _ => None }
            }
        }
    };
}
pub(crate) use string_enum;

string_enum! {
    /// Type of a key attribute.
    pub enum ScalarType {
        /// String.
        S => "S",
        /// Number.
        N => "N",
        /// Binary.
        B => "B",
    }
}

impl ScalarType {
    /// The matching [`ValueType`].
    pub fn value_type(self) -> ValueType {
        match self {
            Self::S => ValueType::S,
            Self::N => ValueType::N,
            Self::B => ValueType::B,
        }
    }
}

string_enum! {
    /// Which attributes an index copies from the table.
    pub enum ProjectionType {
        /// Every attribute.
        All => "ALL",
        /// Only the table and index keys.
        KeysOnly => "KEYS_ONLY",
        /// Keys plus `non_key_attributes`.
        Include => "INCLUDE",
    }
}

string_enum! {
    /// How the table is billed.
    pub enum BillingMode {
        /// On-demand.
        PayPerRequest => "PAY_PER_REQUEST",
        /// Provisioned capacity.
        Provisioned => "PROVISIONED",
    }
}

string_enum! {
    /// What a DynamoDB stream record contains.
    pub enum StreamViewType {
        /// Only the key attributes.
        KeysOnly => "KEYS_ONLY",
        /// The item after the change.
        NewImage => "NEW_IMAGE",
        /// The item before the change.
        OldImage => "OLD_IMAGE",
        /// Both images.
        NewAndOldImages => "NEW_AND_OLD_IMAGES",
    }
}

string_enum! {
    /// Storage class of a table.
    pub enum TableClass {
        /// DynamoDB Standard.
        Standard => "STANDARD",
        /// DynamoDB Standard-Infrequent Access.
        StandardInfrequentAccess => "STANDARD_INFREQUENT_ACCESS",
    }
}

string_enum! {
    /// Lifecycle state of a table.
    pub enum TableStatus {
        /// Being created.
        Creating => "CREATING",
        /// Being updated.
        Updating => "UPDATING",
        /// Being deleted.
        Deleting => "DELETING",
        /// Ready.
        Active => "ACTIVE",
        /// The KMS key is not accessible.
        InaccessibleEncryptionCredentials => "INACCESSIBLE_ENCRYPTION_CREDENTIALS",
        /// Being archived.
        Archiving => "ARCHIVING",
        /// Archived.
        Archived => "ARCHIVED",
        /// A replica's region is not authorized (global tables).
        ReplicationNotAuthorized => "REPLICATION_NOT_AUTHORIZED",
        /// A status this version of dynamoxy does not know (DynamoDB added it later): not ready.
        Unknown => "UNKNOWN",
    }
}

string_enum! {
    /// Lifecycle state of a global secondary index.
    pub enum IndexStatus {
        /// Being created (and backfilled).
        Creating => "CREATING",
        /// Being updated.
        Updating => "UPDATING",
        /// Being deleted.
        Deleting => "DELETING",
        /// Ready.
        Active => "ACTIVE",
        /// A status this version of dynamoxy does not know (DynamoDB added it later): not ready.
        Unknown => "UNKNOWN",
    }
}

string_enum! {
    /// State of Time to Live on a table.
    pub enum TimeToLiveStatus {
        /// Being enabled.
        Enabling => "ENABLING",
        /// Being disabled.
        Disabling => "DISABLING",
        /// Enabled.
        Enabled => "ENABLED",
        /// Disabled.
        Disabled => "DISABLED",
    }
}

string_enum! {
    /// State of a backup.
    pub enum BackupStatus {
        /// Being created.
        Creating => "CREATING",
        /// Deleted.
        Deleted => "DELETED",
        /// Ready to restore.
        Available => "AVAILABLE",
    }
}

string_enum! {
    /// Who created a backup.
    pub enum BackupType {
        /// Created by a user (`CreateBackup`).
        User => "USER",
        /// Created by DynamoDB (for example when a table is deleted).
        System => "SYSTEM",
        /// Created by AWS Backup.
        AwsBackup => "AWS_BACKUP",
    }
}

string_enum! {
    /// Which backups `ListBackups` returns.
    pub enum BackupTypeFilter {
        /// User backups.
        User => "USER",
        /// System backups.
        System => "SYSTEM",
        /// AWS Backup backups.
        AwsBackup => "AWS_BACKUP",
        /// Every backup.
        All => "ALL",
    }
}

string_enum! {
    /// State of continuous backups or point-in-time recovery.
    pub enum ContinuousBackupsStatus {
        /// Enabled.
        Enabled => "ENABLED",
        /// Disabled.
        Disabled => "DISABLED",
    }
}

/// The primary key (or an index key) of a table: attribute names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeySchema {
    /// Partition (hash) key attribute.
    pub partition_key: String,
    /// Sort (range) key attribute, if any.
    pub sort_key: Option<String>,
}

impl KeySchema {
    /// The key attribute names, partition key first.
    pub fn names(&self) -> Vec<&str> {
        std::iter::once(self.partition_key.as_str()).chain(self.sort_key.as_deref()).collect()
    }
}

/// Type of an attribute used in a key schema.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttributeDefinition {
    /// Attribute name.
    pub name: String,
    /// Attribute type.
    pub attribute_type: ScalarType,
}

/// Attributes copied into an index.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Projection {
    /// Projection type.
    pub projection_type: ProjectionType,
    /// Non-key attributes copied with `INCLUDE`.
    pub non_key_attributes: Vec<String>,
}

impl Projection {
    /// Every attribute.
    pub fn all() -> Self {
        Self { projection_type: ProjectionType::All, non_key_attributes: Vec::new() }
    }
}

/// Provisioned read and write capacity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProvisionedThroughput {
    /// Read capacity units.
    pub read_capacity_units: i64,
    /// Write capacity units.
    pub write_capacity_units: i64,
}

/// Maximum on-demand throughput (`-1` or `None` for no limit).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct OnDemandThroughput {
    /// Maximum read request units per second.
    pub max_read_request_units: Option<i64>,
    /// Maximum write request units per second.
    pub max_write_request_units: Option<i64>,
}

/// A global secondary index to create.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GlobalSecondaryIndex {
    /// Index name.
    pub index_name: String,
    /// Index key.
    pub key_schema: KeySchema,
    /// Copied attributes.
    pub projection: Projection,
    /// Capacity, for provisioned tables.
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// Maximum on-demand throughput.
    pub on_demand_throughput: Option<OnDemandThroughput>,
}

/// A local secondary index to create.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalSecondaryIndex {
    /// Index name.
    pub index_name: String,
    /// Index key (same partition key as the table).
    pub key_schema: KeySchema,
    /// Copied attributes.
    pub projection: Projection,
}

/// DynamoDB Streams settings.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StreamSpecification {
    /// Whether the stream is enabled.
    pub enabled: bool,
    /// What records contain (required when enabling).
    pub view_type: Option<StreamViewType>,
}

/// Server-side encryption settings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SseSpecification {
    /// `true` for a KMS key (AWS managed or customer managed), `false` for an AWS owned key.
    pub enabled: bool,
    /// `"KMS"` (the only value DynamoDB accepts when enabled).
    pub sse_type: Option<String>,
    /// Customer managed KMS key id, ARN or alias.
    pub kms_master_key_id: Option<String>,
}

/// Server-side encryption state of a table.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SseDescription {
    /// `ENABLING`, `ENABLED`, `DISABLING`, `DISABLED`, `UPDATING`.
    pub status: Option<String>,
    /// `AES256` or `KMS`.
    pub sse_type: Option<String>,
    /// KMS key ARN.
    pub kms_master_key_arn: Option<String>,
}

/// A secondary index as described by `DescribeTable`.
#[derive(Clone, Debug, PartialEq)]
pub struct IndexDescription {
    /// Index name.
    pub index_name: String,
    /// Index key.
    pub key_schema: KeySchema,
    /// Copied attributes.
    pub projection: Projection,
    /// State (global secondary indexes only).
    pub index_status: Option<IndexStatus>,
    /// Approximate number of items.
    pub item_count: i64,
    /// Approximate size in bytes.
    pub index_size_bytes: i64,
    /// ARN.
    pub index_arn: Option<String>,
    /// Capacity (provisioned tables).
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// Whether DynamoDB is still backfilling the index.
    pub backfilling: bool,
}

/// A table as described by `DescribeTable`, `CreateTable`, `UpdateTable` and `DeleteTable`.
#[derive(Clone, Debug, PartialEq)]
pub struct TableDescription {
    /// Table name.
    pub table_name: String,
    /// ARN.
    pub table_arn: Option<String>,
    /// Unique id.
    pub table_id: Option<String>,
    /// State.
    pub table_status: TableStatus,
    /// Creation time, as seconds since the Unix epoch.
    pub creation_date_time: Option<f64>,
    /// Primary key.
    pub key_schema: KeySchema,
    /// Types of key attributes (table and indexes).
    pub attribute_definitions: Vec<AttributeDefinition>,
    /// Approximate number of items (updated about every six hours by DynamoDB).
    pub item_count: i64,
    /// Approximate size in bytes.
    pub table_size_bytes: i64,
    /// Billing mode.
    pub billing_mode: BillingMode,
    /// Capacity (provisioned tables).
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// Maximum on-demand throughput.
    pub on_demand_throughput: Option<OnDemandThroughput>,
    /// Global secondary indexes.
    pub global_secondary_indexes: Vec<IndexDescription>,
    /// Local secondary indexes.
    pub local_secondary_indexes: Vec<IndexDescription>,
    /// Stream settings.
    pub stream_specification: Option<StreamSpecification>,
    /// ARN of the latest stream.
    pub latest_stream_arn: Option<String>,
    /// Label of the latest stream.
    pub latest_stream_label: Option<String>,
    /// Encryption state.
    pub sse_description: Option<SseDescription>,
    /// Storage class.
    pub table_class: Option<TableClass>,
    /// Whether deletion protection is on.
    pub deletion_protection_enabled: bool,
}

impl TableDescription {
    /// The type of a key attribute, from the attribute definitions.
    pub fn attribute_type(&self, name: &str) -> Option<ScalarType> {
        self.attribute_definitions
            .iter()
            .find(|definition| definition.name == name)
            .map(|definition| definition.attribute_type)
    }
}

/// Time to Live state of a table.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TimeToLiveDescription {
    /// State.
    pub status: TimeToLiveStatus,
    /// The TTL attribute, when enabled.
    pub attribute_name: Option<String>,
}

/// Capacity consumed on a table or index.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Capacity {
    /// Read capacity units.
    pub read_capacity_units: f64,
    /// Write capacity units.
    pub write_capacity_units: f64,
    /// Total capacity units.
    pub capacity_units: f64,
}

/// Capacity consumed by one request on one table (`ReturnConsumedCapacity`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ConsumedCapacity {
    /// Table name.
    pub table_name: String,
    /// Total capacity units.
    pub capacity_units: f64,
    /// Read capacity units.
    pub read_capacity_units: f64,
    /// Write capacity units.
    pub write_capacity_units: f64,
    /// Capacity consumed on the table itself (`INDEXES` only).
    pub table: Option<Capacity>,
    /// Capacity consumed per local secondary index (`INDEXES` only).
    pub local_secondary_indexes: BTreeMap<String, Capacity>,
    /// Capacity consumed per global secondary index (`INDEXES` only).
    pub global_secondary_indexes: BTreeMap<String, Capacity>,
}

/// Size of the item collection an item belongs to (tables with local secondary indexes).
#[derive(Clone, Debug, PartialEq)]
pub struct ItemCollectionMetrics {
    /// Partition key of the collection.
    pub item_collection_key: Item,
    /// Estimated size range in GB.
    pub size_estimate_range_gb: Vec<f64>,
}

/// A backup.
#[derive(Clone, Debug, PartialEq)]
pub struct BackupDetails {
    /// ARN.
    pub backup_arn: String,
    /// Name.
    pub backup_name: String,
    /// Size in bytes.
    pub backup_size_bytes: Option<i64>,
    /// State.
    pub backup_status: BackupStatus,
    /// Who created it.
    pub backup_type: BackupType,
    /// Creation time, as seconds since the Unix epoch.
    pub backup_creation_date_time: f64,
    /// Expiry time (system backups).
    pub backup_expiry_date_time: Option<f64>,
}

/// A backup and the table it was taken from.
#[derive(Clone, Debug, PartialEq)]
pub struct BackupDescription {
    /// The backup.
    pub details: BackupDetails,
    /// Source table name.
    pub table_name: String,
    /// Source table ARN.
    pub table_arn: Option<String>,
    /// Source table id.
    pub table_id: Option<String>,
    /// Source table key schema.
    pub key_schema: Option<KeySchema>,
    /// Number of items in the source table when the backup was taken.
    pub item_count: Option<i64>,
}

/// Continuous backups and point-in-time recovery state of a table.
#[derive(Clone, Debug, PartialEq)]
pub struct ContinuousBackupsDescription {
    /// Continuous backups state.
    pub continuous_backups_status: ContinuousBackupsStatus,
    /// Point-in-time recovery state.
    pub point_in_time_recovery_status: ContinuousBackupsStatus,
    /// Number of days of recovery history (1-35).
    pub recovery_period_in_days: Option<i64>,
    /// Earliest restorable time, as seconds since the Unix epoch.
    pub earliest_restorable_date_time: Option<f64>,
    /// Latest restorable time, as seconds since the Unix epoch.
    pub latest_restorable_date_time: Option<f64>,
}
