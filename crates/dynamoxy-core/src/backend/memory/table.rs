//! Storage of one in-memory table: items ordered like DynamoDB orders them, secondary indexes
//! maintained on every write, and the validation DynamoDB applies to items and keys.

use std::borrow::Cow;
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use crate::error::{Error, Result};
use crate::expr::Path;
use crate::types::{
    AttributeDefinition, BillingMode, Capacity, ConsumedCapacity, IndexDescription, IndexStatus, ItemCollectionMetrics,
    KeySchema, OnDemandThroughput, Projection, ProjectionType, ProvisionedThroughput, ReturnConsumedCapacity,
    ScalarType, SseDescription, StreamSpecification, TableClass, TableDescription, TableStatus,
};
use crate::value::{AttributeValue, Item, MAX_ITEM_SIZE, item_size, value_size};

/// Largest partition key value (bytes).
const MAX_PARTITION_KEY_SIZE: usize = 2048;
/// Largest sort key value (bytes).
const MAX_SORT_KEY_SIZE: usize = 1024;

/// DynamoDB's refusal of a partition key value over [`MAX_PARTITION_KEY_SIZE`].
const HASH_KEY_TOO_LARGE: &str =
    "One or more parameter values were invalid: Size of hashkey has exceeded the maximum size limit of2048 bytes";

/// DynamoDB's refusal of a sort key value over [`MAX_SORT_KEY_SIZE`].
const RANGE_KEY_TOO_LARGE: &str = "One or more parameter values were invalid: Aggregated size of all range keys has exceeded the size limit of 1024 bytes";

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

/// A key attribute value with DynamoDB's ordering (strings and binaries by bytes, numbers
/// numerically). All values of one key attribute have the same type.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyValue(pub AttributeValue);

impl KeyValue {
    /// A value that sorts after every key value (an upper bound for ranges).
    const AFTER_ALL: KeyValue = KeyValue(AttributeValue::Null);

    fn rank(&self) -> u8 {
        match self.0 {
            AttributeValue::S(_) => 0,
            AttributeValue::N(_) => 1,
            AttributeValue::B(_) => 2,
            // Not a key type: only `AFTER_ALL`.
            _ => 3,
        }
    }

    fn hash_into(&self, hash: &mut u64) {
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut feed = |byte: u8| {
            *hash ^= u64::from(byte);
            *hash = hash.wrapping_mul(PRIME);
        };
        feed(self.rank());
        match &self.0 {
            AttributeValue::S(text) => text.bytes().for_each(&mut feed),
            AttributeValue::N(number) => number.as_str().bytes().for_each(&mut feed),
            AttributeValue::B(bytes) => bytes.iter().copied().for_each(&mut feed),
            _ => {}
        }
    }
}

impl PartialOrd for KeyValue {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for KeyValue {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.cmp_scalar(&other.0).unwrap_or_else(|| self.rank().cmp(&other.rank()))
    }
}

/// Where an item lives: partitions are ordered by a stable hash (like DynamoDB, scans are not
/// in key order), items within a partition by sort key.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct StoredKey {
    /// FNV-1a hash of the partition key value.
    pub hash: u64,
    /// Partition key value.
    pub partition: KeyValue,
    /// Sort key value.
    pub sort: Option<KeyValue>,
}

impl StoredKey {
    /// A key from its partition and sort values.
    pub fn new(partition: AttributeValue, sort: Option<AttributeValue>) -> Self {
        let partition = KeyValue(partition);
        let mut hash = 0xcbf2_9ce4_8422_2325;
        partition.hash_into(&mut hash);
        Self { hash, partition, sort: sort.map(KeyValue) }
    }

    /// A key that sorts before every real key (lower bound for range scans).
    pub fn min() -> Self {
        Self { hash: 0, partition: KeyValue(AttributeValue::S(String::new())), sort: None }
    }

    /// A key that sorts after every real key.
    pub fn max() -> Self {
        Self { hash: u64::MAX, partition: KeyValue::AFTER_ALL, sort: Some(KeyValue::AFTER_ALL) }
    }

    /// A key that sorts after every key of this key's partition, and before the next partition.
    pub fn partition_end(&self) -> Self {
        Self { hash: self.hash, partition: self.partition.clone(), sort: Some(KeyValue::AFTER_ALL) }
    }

    /// The key of this key's partition with the sort key value `sort`.
    pub fn with_sort(&self, sort: AttributeValue) -> Self {
        Self { hash: self.hash, partition: self.partition.clone(), sort: Some(KeyValue(sort)) }
    }

    /// The parallel scan segment of this key.
    pub fn segment(&self, total_segments: u32) -> u32 {
        (self.hash % u64::from(total_segments.max(1))) as u32
    }
}

/// One key attribute.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyAttribute {
    /// Attribute name.
    pub name: String,
    /// Attribute type.
    pub kind: ScalarType,
}

/// A table's or index's key.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyInfo {
    /// Partition key.
    pub partition: KeyAttribute,
    /// Sort key.
    pub sort: Option<KeyAttribute>,
}

impl KeyInfo {
    /// Resolves a key schema against attribute definitions.
    pub fn new(schema: &KeySchema, definitions: &[AttributeDefinition]) -> Result<Self> {
        let lookup = |name: &str| {
            definitions.iter().find(|definition| definition.name == name).map(|definition| definition.attribute_type).ok_or_else(|| {
                invalid(format!(
                    "One or more parameter values were invalid: Some index key attributes are not defined in AttributeDefinitions. Keys: [{}], AttributeDefinitions: [{}]",
                    schema.names().join(", "),
                    definitions.iter().map(|definition| definition.name.as_str()).collect::<Vec<_>>().join(", ")
                ))
            })
        };
        Ok(Self {
            partition: KeyAttribute { name: schema.partition_key.clone(), kind: lookup(&schema.partition_key)? },
            sort: match &schema.sort_key {
                Some(name) => Some(KeyAttribute { name: name.clone(), kind: lookup(name)? }),
                None => None,
            },
        })
    }

    /// The schema (names only).
    pub fn schema(&self) -> KeySchema {
        KeySchema {
            partition_key: self.partition.name.clone(),
            sort_key: self.sort.as_ref().map(|sort| sort.name.clone()),
        }
    }

    /// Key attribute names, partition key first.
    pub fn names(&self) -> Vec<&str> {
        std::iter::once(self.partition.name.as_str()).chain(self.sort.as_ref().map(|sort| sort.name.as_str())).collect()
    }

    /// Whether `name` is a key attribute.
    pub fn has(&self, name: &str) -> bool {
        self.partition.name == name || self.sort.as_ref().is_some_and(|sort| sort.name == name)
    }

    pub(crate) fn attributes(&self) -> impl Iterator<Item = &KeyAttribute> {
        std::iter::once(&self.partition).chain(&self.sort)
    }

    /// Validates the key attributes of a full item (`PutItem`) and returns its key.
    pub fn item_key(&self, item: &Item) -> Result<StoredKey> {
        for attribute in self.attributes() {
            let Some(value) = item.get(&attribute.name) else {
                return Err(invalid(format!(
                    "One or more parameter values were invalid: Missing the key {} in the item",
                    attribute.name
                )));
            };
            check_key_value(&attribute.name, attribute.kind, value)?;
        }
        Ok(self.stored(item))
    }

    /// Validates a key (`GetItem`, `DeleteItem`, ...): exactly the key attributes, right types.
    pub fn exact_key(&self, key: &Item) -> Result<StoredKey> {
        let mismatch = || invalid("The provided key element does not match the schema");
        if key.len() != self.names().len() {
            return Err(mismatch());
        }
        for attribute in self.attributes() {
            let value = key.get(&attribute.name).ok_or_else(mismatch)?;
            if value.value_type() != attribute.kind.value_type() {
                return Err(mismatch());
            }
            check_key_value(&attribute.name, attribute.kind, value)?;
        }
        Ok(self.stored(key))
    }

    /// The key of an item known to be valid.
    pub fn stored(&self, item: &Item) -> StoredKey {
        let value = |attribute: &KeyAttribute| item.get(&attribute.name).cloned();
        StoredKey::new(value(&self.partition).unwrap_or(AttributeValue::Null), self.sort.as_ref().and_then(value))
    }

    /// Just the key attributes of an item.
    pub fn key_of(&self, item: &Item) -> Item {
        self.attributes()
            .filter_map(|attribute| item.get(&attribute.name).map(|value| (attribute.name.clone(), value.clone())))
            .collect()
    }
}

fn check_key_value(name: &str, kind: ScalarType, value: &AttributeValue) -> Result<()> {
    if value.value_type() != kind.value_type() {
        return Err(invalid(format!(
            "One or more parameter values were invalid: Type mismatch for key {name} expected: {} actual: {}",
            kind.as_str(),
            value.value_type().as_str()
        )));
    }
    let empty = match value {
        AttributeValue::S(text) => text.is_empty(),
        AttributeValue::B(bytes) => bytes.is_empty(),
        _ => false,
    };
    if empty {
        return Err(invalid(format!(
            "One or more parameter values are not valid. The AttributeValue for a key attribute cannot contain an empty {} value. Key: {name}",
            if kind == ScalarType::S { "string" } else { "binary" }
        )));
    }
    Ok(())
}

/// A secondary index.
#[derive(Clone, Debug)]
pub struct Index {
    /// Index name.
    pub name: String,
    /// Global (`true`) or local.
    pub global: bool,
    /// Index key.
    pub key: KeyInfo,
    /// Copied attributes.
    pub projection: Projection,
    /// Entries: index key, then table key.
    pub entries: BTreeSet<(StoredKey, StoredKey)>,
    /// State (global indexes).
    pub status: IndexStatus,
    /// Capacity.
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// On-demand limits.
    pub on_demand_throughput: Option<OnDemandThroughput>,
}

impl Index {
    /// The index key of an item, if the item has every index key attribute (sparse indexes).
    pub fn entry_key(&self, item: &Item) -> Option<StoredKey> {
        let partition = item.get(&self.key.partition.name)?;
        let sort = match &self.key.sort {
            Some(sort) => Some(item.get(&sort.name)?.clone()),
            None => None,
        };
        Some(StoredKey::new(partition.clone(), sort))
    }

    /// Validates the index key attributes an item has. `update`: the item is the result of an
    /// update expression, whose refusal of an empty key value has its own wording.
    pub fn check_item(&self, item: &Item, update: bool) -> Result<()> {
        for attribute in self.key.attributes() {
            let Some(value) = item.get(&attribute.name) else { continue };
            if value.value_type() != attribute.kind.value_type() {
                return Err(invalid(format!(
                    "One or more parameter values were invalid: Type mismatch for Index Key {} Expected: {} Actual: {} IndexName: {}",
                    attribute.name,
                    attribute.kind.as_str(),
                    value.value_type().as_str(),
                    self.name
                )));
            }
            let empty = match value {
                AttributeValue::S(text) if text.is_empty() => Some("string"),
                AttributeValue::B(bytes) if bytes.is_empty() => Some("binary"),
                _ => None,
            };
            if let Some(kind) = empty {
                return Err(invalid(if update {
                    format!(
                        "One or more parameter values are not valid. The update expression attempted to update a secondary index key to a value that is not supported. The AttributeValue for a key attribute cannot contain an empty {kind} value."
                    )
                } else {
                    format!(
                        "One or more parameter values are not valid. A value specified for a secondary index key is not supported. The AttributeValue for a key attribute cannot contain an empty {kind} value. IndexName: {}, IndexKey: {}",
                        self.name, attribute.name
                    )
                }));
            }
            // Index keys have the limits of table keys, with the same messages.
            if attribute.name == self.key.partition.name {
                if value_size(value) > MAX_PARTITION_KEY_SIZE {
                    return Err(invalid(HASH_KEY_TOO_LARGE));
                }
            } else if value_size(value) > MAX_SORT_KEY_SIZE {
                return Err(invalid(RANGE_KEY_TOO_LARGE));
            }
        }
        Ok(())
    }

    /// The attributes of `item` copied into this index.
    pub fn project(&self, item: &Item, table_key: &KeyInfo) -> Item {
        self.projected(item, table_key).into_owned()
    }

    /// [`Self::project`], borrowing the item when the index copies every attribute.
    pub fn projected<'a>(&self, item: &'a Item, table_key: &KeyInfo) -> Cow<'a, Item> {
        match self.projection.projection_type {
            ProjectionType::All => Cow::Borrowed(item),
            ProjectionType::KeysOnly | ProjectionType::Include => Cow::Owned(
                item.iter()
                    .filter(|(name, _)| self.holds(name, table_key))
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect(),
            ),
        }
    }

    /// Whether the entries of this index hold the attribute `name`: the table and index keys,
    /// and what the projection copies.
    pub fn holds(&self, name: &str, table_key: &KeyInfo) -> bool {
        let projection = &self.projection;
        projection.projection_type == ProjectionType::All
            || table_key.has(name)
            || self.key.has(name)
            || (projection.projection_type == ProjectionType::Include
                && projection.non_key_attributes.iter().any(|attribute| attribute == name))
    }

    /// Whether a read of this index that returns `paths` reads its items from the table: a local
    /// index fetches the attributes its entries do not hold (a global index cannot).
    pub fn fetches(&self, paths: &[Path], table_key: &KeyInfo) -> bool {
        !self.global && paths.iter().any(|path| !self.holds(path.root(), table_key))
    }

    /// Describes the index.
    pub fn describe(
        &self,
        table_arn: &str,
        items: &BTreeMap<StoredKey, Item>,
        table_key: &KeyInfo,
    ) -> IndexDescription {
        let size: usize = self
            .entries
            .iter()
            .filter_map(|(_, table_key_value)| items.get(table_key_value))
            .map(|item| item_size(&self.projected(item, table_key)))
            .sum();
        IndexDescription {
            index_name: self.name.clone(),
            key_schema: self.key.schema(),
            projection: self.projection.clone(),
            index_status: self.global.then_some(self.status),
            item_count: self.entries.len() as i64,
            index_size_bytes: size as i64,
            index_arn: Some(format!("{table_arn}/index/{}", self.name)),
            provisioned_throughput: self.provisioned_throughput,
            backfilling: self.global && self.status == IndexStatus::Creating,
        }
    }
}

/// One in-memory table.
#[derive(Clone, Debug)]
pub struct Table {
    /// Table name.
    pub name: String,
    /// ARN.
    pub arn: String,
    /// Unique id.
    pub id: String,
    /// Creation time (seconds since the Unix epoch).
    pub created: f64,
    /// Primary key.
    pub key: KeyInfo,
    /// Key attribute types (table and indexes).
    pub attribute_definitions: Vec<AttributeDefinition>,
    /// Items by key.
    pub items: BTreeMap<StoredKey, Item>,
    /// Secondary indexes (global and local).
    pub indexes: Vec<Index>,
    /// Billing mode.
    pub billing_mode: BillingMode,
    /// Capacity.
    pub provisioned_throughput: Option<ProvisionedThroughput>,
    /// On-demand limits.
    pub on_demand_throughput: Option<OnDemandThroughput>,
    /// Stream settings.
    pub stream_specification: Option<StreamSpecification>,
    /// Stream label (set when a stream is enabled).
    pub stream_label: Option<String>,
    /// Encryption.
    pub sse: Option<SseDescription>,
    /// Storage class.
    pub table_class: Option<TableClass>,
    /// Deletion protection.
    pub deletion_protection: bool,
    /// TTL attribute when TTL is enabled.
    pub ttl_attribute: Option<String>,
    /// Tags.
    pub tags: BTreeMap<String, String>,
    /// Point-in-time recovery period in days, when enabled.
    pub pitr_days: Option<i64>,
    /// `DescribeTable` calls until the table being created is `ACTIVE` (0: it is), like a
    /// caller that polls until it is (see `admin`).
    pub describes_until_active: u8,
    /// `DescribeTable` calls until the global index being created is `ACTIVE` (0: none is).
    pub index_describes_until_active: u8,
}

impl Table {
    /// Keeps only the attribute definitions that the table's key or an index key uses.
    pub fn retain_used_definitions(&mut self) {
        let used: BTreeSet<&str> = std::iter::once(&self.key)
            .chain(self.indexes.iter().map(|index| &index.key))
            .flat_map(KeyInfo::names)
            .collect();
        self.attribute_definitions.retain(|definition| used.contains(definition.name.as_str()));
    }

    /// The index with this name.
    pub fn index(&self, name: &str) -> Result<&Index> {
        self.indexes
            .iter()
            .find(|index| index.name == name)
            .ok_or_else(|| invalid(format!("The table does not have the specified index: {name}")))
    }

    /// Validates a full item for a write and returns its key.
    pub fn validate_item(&self, item: &Item) -> Result<StoredKey> {
        self.validate_written(item, false)
    }

    /// [`Self::validate_item`] for the item an update expression leaves, with DynamoDB's messages
    /// for updates ("Item size to update has exceeded the maximum allowed size", ...).
    pub fn validate_updated(&self, item: &Item) -> Result<StoredKey> {
        self.validate_written(item, true)
    }

    fn validate_written(&self, item: &Item, update: bool) -> Result<StoredKey> {
        if item.keys().any(String::is_empty) {
            return Err(invalid(crate::expr::EMPTY_NAME));
        }
        let key = self.key.item_key(item)?;
        if let Some(partition) = item.get(&self.key.partition.name)
            && value_size(partition) > MAX_PARTITION_KEY_SIZE
        {
            return Err(invalid(HASH_KEY_TOO_LARGE));
        }
        if let Some(sort) = self.key.sort.as_ref().and_then(|sort| item.get(&sort.name))
            && value_size(sort) > MAX_SORT_KEY_SIZE
        {
            return Err(invalid(RANGE_KEY_TOO_LARGE));
        }
        for value in item.values() {
            value.validate().map_err(invalid)?;
        }
        if item_size(item) > MAX_ITEM_SIZE {
            return Err(invalid(if update {
                "Item size to update has exceeded the maximum allowed size"
            } else {
                "Item size has exceeded the maximum allowed size"
            }));
        }
        for index in &self.indexes {
            index.check_item(item, update)?;
        }
        Ok(key)
    }

    /// Replaces (or with `None` deletes) the item at `key`, keeping the indexes current.
    /// Returns the previous item.
    pub fn write(&mut self, key: &StoredKey, item: Option<Item>) -> Option<Item> {
        let old = match item {
            Some(item) => {
                for index in &mut self.indexes {
                    if let Some(entry) = index.entry_key(&item) {
                        index.entries.insert((entry, key.clone()));
                    }
                }
                self.items.insert(key.clone(), item)
            }
            None => self.items.remove(key),
        };
        if let Some(old) = &old {
            let new = self.items.get(key);
            for index in &mut self.indexes {
                if let Some(entry) = index.entry_key(old)
                    && new.and_then(|new| index.entry_key(new)).as_ref() != Some(&entry)
                {
                    index.entries.remove(&(entry, key.clone()));
                }
            }
        }
        old
    }

    /// Item collection metrics for the partition of `item` (tables with local secondary indexes).
    pub fn item_collection_metrics(&self, item: &Item) -> Option<ItemCollectionMetrics> {
        if !self.indexes.iter().any(|index| !index.global) {
            return None;
        }
        let partition = item.get(&self.key.partition.name)?.clone();
        let key = StoredKey::new(partition.clone(), None);
        let size: usize = self
            .items
            .iter()
            .filter(|(stored, _)| stored.hash == key.hash && stored.partition == key.partition)
            .map(|(_, item)| item_size(item))
            .sum();
        let gigabytes = size as f64 / (1024.0 * 1024.0 * 1024.0);
        Some(ItemCollectionMetrics {
            item_collection_key: [(self.key.partition.name.clone(), partition)].into_iter().collect(),
            size_estimate_range_gb: vec![gigabytes, gigabytes.max(1.0)],
        })
    }

    /// Describes the table with `status`.
    pub fn describe(&self, status: TableStatus) -> TableDescription {
        let size: usize = self.items.values().map(item_size).sum();
        let (global, local): (Vec<&Index>, Vec<&Index>) = self.indexes.iter().partition(|index| index.global);
        TableDescription {
            table_name: self.name.clone(),
            table_arn: Some(self.arn.clone()),
            table_id: Some(self.id.clone()),
            table_status: status,
            creation_date_time: Some(self.created),
            key_schema: self.key.schema(),
            attribute_definitions: self.attribute_definitions.clone(),
            item_count: self.items.len() as i64,
            table_size_bytes: size as i64,
            billing_mode: self.billing_mode,
            provisioned_throughput: self.provisioned_throughput,
            on_demand_throughput: self.on_demand_throughput,
            global_secondary_indexes: global
                .into_iter()
                .map(|index| index.describe(&self.arn, &self.items, &self.key))
                .collect(),
            local_secondary_indexes: local
                .into_iter()
                .map(|index| index.describe(&self.arn, &self.items, &self.key))
                .collect(),
            stream_specification: self.stream_specification,
            latest_stream_arn: self.stream_label.as_ref().map(|label| format!("{}/stream/{label}", self.arn)),
            latest_stream_label: self.stream_label.clone(),
            sse_description: self.sse.clone(),
            table_class: self.table_class,
            deletion_protection_enabled: self.deletion_protection,
        }
    }
}

/// Accumulates consumed capacity for one request, per table and index.
#[derive(Debug, Default)]
pub struct CapacityMeter {
    tables: BTreeMap<String, TableCapacity>,
}

#[derive(Debug, Default)]
struct TableCapacity {
    table: Capacity,
    global: BTreeMap<String, Capacity>,
    local: BTreeMap<String, Capacity>,
}

fn add_capacity(capacity: &mut Capacity, read: f64, write: f64) {
    capacity.read_capacity_units += read;
    capacity.write_capacity_units += write;
    capacity.capacity_units += read + write;
}

/// Read units for `bytes` read (4 KB units; eventually consistent reads cost half).
pub fn read_units(bytes: usize, consistent: bool) -> f64 {
    let units = bytes.div_ceil(4096).max(1) as f64;
    if consistent { units } else { units / 2.0 }
}

/// Write units for an item of `bytes` (1 KB units).
pub fn write_units(bytes: usize) -> f64 {
    bytes.div_ceil(1024).max(1) as f64
}

impl CapacityMeter {
    /// Records reads on a table (or on one of its indexes).
    pub fn read(&mut self, table: &str, index: Option<(&str, bool)>, units: f64) {
        let entry = self.tables.entry(table.to_owned()).or_default();
        match index {
            None => add_capacity(&mut entry.table, units, 0.0),
            Some((name, true)) => add_capacity(entry.global.entry(name.to_owned()).or_default(), units, 0.0),
            Some((name, false)) => add_capacity(entry.local.entry(name.to_owned()).or_default(), units, 0.0),
        }
    }

    /// Records a write of an item (old and new versions) on a table and its affected indexes.
    /// `multiplier` is the transactional factor (2) of the table's units.
    pub fn write(&mut self, table: &Table, old: Option<&Item>, new: Option<&Item>, multiplier: f64) {
        let size = old.map_or(0, item_size).max(new.map_or(0, item_size));
        let entry = self.tables.entry(table.name.clone()).or_default();
        add_capacity(&mut entry.table, 0.0, write_units(size) * multiplier);
        for index in &table.indexes {
            let before = old.and_then(|item| index.entry_key(item).map(|_| index.projected(item, &table.key)));
            let after = new.and_then(|item| index.entry_key(item).map(|_| index.projected(item, &table.key)));
            let key_changed = match (old, new) {
                (Some(old), Some(new)) => index.entry_key(old) != index.entry_key(new),
                _ => false,
            };
            // Index writes: adding or removing an entry costs one write, changing its key two
            // (delete and put), changing projected attributes one, no change nothing.
            let units = match (&before, &after) {
                (None, None) => continue,
                (Some(before), None) => write_units(item_size(before)),
                (None, Some(after)) => write_units(item_size(after)),
                (Some(before), Some(after)) if key_changed => {
                    write_units(item_size(before)) + write_units(item_size(after))
                }
                (Some(before), Some(after)) if before == after => continue,
                (Some(_), Some(after)) => write_units(item_size(after)),
            };
            let target = if index.global {
                entry.global.entry(index.name.clone())
            } else {
                entry.local.entry(index.name.clone())
            };
            // The transactional factor applies to the table alone: an index entry costs what it
            // costs outside a transaction (captured against DynamoDB by dynoxide-rs, a put of an
            // indexed item in a transaction is table 2 and index 1).
            add_capacity(target.or_default(), 0.0, units);
        }
    }

    /// The consumed capacity of one table, in the requested detail.
    pub fn table_report(&self, table: &str, mode: ReturnConsumedCapacity) -> Option<ConsumedCapacity> {
        if mode == ReturnConsumedCapacity::None {
            return None;
        }
        let entry = self.tables.get(table)?;
        let mut total = entry.table;
        for capacity in entry.global.values().chain(entry.local.values()) {
            add_capacity(&mut total, capacity.read_capacity_units, capacity.write_capacity_units);
        }
        let detailed = mode == ReturnConsumedCapacity::Indexes;
        Some(ConsumedCapacity {
            table_name: table.to_owned(),
            capacity_units: total.capacity_units,
            read_capacity_units: total.read_capacity_units,
            write_capacity_units: total.write_capacity_units,
            table: detailed.then_some(entry.table),
            local_secondary_indexes: if detailed { entry.local.clone() } else { BTreeMap::new() },
            global_secondary_indexes: if detailed { entry.global.clone() } else { BTreeMap::new() },
        })
    }

    /// Reports for every table touched, in table name order.
    pub fn reports(&self, mode: ReturnConsumedCapacity) -> Vec<ConsumedCapacity> {
        self.tables.keys().filter_map(|table| self.table_report(table, mode)).collect()
    }
}

/// Data a page of a `Query`, `Scan` or PartiQL `SELECT` may read before it stops (1 MB).
const PAGE_SIZE_LIMIT: usize = 1024 * 1024;

/// What one page of a `Query`, `Scan` or PartiQL `SELECT` has read, for its size and read units.
#[derive(Debug)]
pub struct PageRead {
    consistent: bool,
    /// Size of the items, or index entries, read.
    entries: usize,
    /// Size of the items a read of a local index fetched from the table, each rounded up to 4 KB.
    fetched: usize,
    /// Read units of the fetched items, each charged like a read of that item alone.
    fetched_units: f64,
}

impl PageRead {
    /// An empty page of a strongly consistent read or not.
    pub fn new(consistent: bool) -> Self {
        Self { consistent, entries: 0, fetched: 0, fetched_units: 0.0 }
    }

    /// Records an item, or index entry, read.
    pub fn entry(&mut self, entry: &Item) {
        self.entries += item_size(entry);
    }

    /// Records an item a read of a local index fetched from the table.
    pub fn fetch(&mut self, item: &Item) {
        let size = item_size(item);
        self.fetched += size.next_multiple_of(4096);
        self.fetched_units += read_units(size, self.consistent);
    }

    /// Whether the page has read its 1 MB: the entries, and once it fetched items, the entries
    /// rounded up to 4 KB and the fetched items, each rounded up to 4 KB.
    pub fn is_full(&self) -> bool {
        let size = if self.fetched == 0 { self.entries } else { self.entries.next_multiple_of(4096) + self.fetched };
        size >= PAGE_SIZE_LIMIT
    }

    /// Records the read units on `meter`: the entries on the table or index read, the fetched
    /// items on the table.
    pub fn charge(&self, meter: &mut CapacityMeter, table: &str, index: Option<(&str, bool)>) {
        meter.read(table, index, read_units(self.entries, self.consistent));
        if self.fetched > 0 {
            meter.read(table, None, self.fetched_units);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numeric_sort_keys_order_numerically() {
        let mut keys: Vec<StoredKey> = [10, 9, -1, 100]
            .into_iter()
            .map(|sort| StoredKey::new(AttributeValue::s("p"), Some(AttributeValue::n(sort))))
            .collect();
        keys.sort();
        let sorted: Vec<String> = keys.iter().map(|key| format!("{:?}", key.sort.as_ref().map(|k| &k.0))).collect();
        assert_eq!(sorted, ["Some({N: -1})", "Some({N: 9})", "Some({N: 10})", "Some({N: 100})"]);
    }

    #[test]
    fn capacity_units_round_up() {
        assert_eq!(read_units(1, true), 1.0);
        assert_eq!(read_units(4097, false), 1.0);
        assert_eq!(write_units(1025), 2.0);
    }
}
