//! Table administration: create, update, delete, describe and list tables; Time to Live; tags;
//! point-in-time recovery; backups and restores.
//!
//! Tables hold items immediately. Like DynamoDB, `CreateTable` reports `CREATING`, `UpdateTable`
//! `UPDATING` and `DeleteTable` `DELETING`, while `DescribeTable` afterwards reports `ACTIVE`
//! (or `ResourceNotFoundException`), so code that waits for a table works unchanged.
//!
//! A created table, and a global index added to one, is `CREATING` in the first `DescribeTable`
//! after it and `ACTIVE` from the second on, the way a caller that polls until it is active sees
//! it (the client's waiter polls the in-memory backend without sleeping). Until then DynamoDB
//! refuses to change the table (`UpdateTable`, `UpdateTimeToLive`) or to add another index, with
//! its messages (measured in eu-north-1 on 2026-10-31, docs/parity), and the in-memory backend a
//! deletion of an index too (as DynamoDB's message says, not measured). Reads and writes of its
//! items are not refused.

use std::collections::{BTreeMap, BTreeSet};

use super::table::{Index, KeyInfo, Table};
use super::{Backup, State, now, random_hex};
use crate::error::{Error, Result, ServiceError};
use crate::types::{
    AttributeDefinition, BackupDescription, BackupDetails, BackupStatus, BackupType, BackupTypeFilter, BillingMode,
    ContinuousBackupsDescription, ContinuousBackupsStatus, CreateTableInput, GlobalSecondaryIndexUpdate, IndexStatus,
    ListBackupsInput, ListBackupsOutput, ListTablesInput, ListTablesOutput, ListTagsOutput, Projection, ProjectionType,
    RestoreOverrides, RestoreTableFromBackupInput, RestoreTableToPointInTimeInput, SseDescription, SseSpecification,
    TableDescription, TableStatus, TimeToLiveDescription, TimeToLiveStatus, UpdateTableInput, UpdateTimeToLiveInput,
};

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

fn parameter_error(message: impl std::fmt::Display) -> Error {
    invalid(format!("One or more parameter values were invalid: {message}"))
}

/// Checks a table or index name (3-255 characters: letters, digits, `_`, `-`, `.`).
pub fn check_name(name: &str, field: &str) -> Result<()> {
    if name.len() < 3 || name.len() > 255 {
        let bound = if name.len() < 3 { "greater than or equal to 3" } else { "less than or equal to 255" };
        return Err(invalid(format!(
            "1 validation error detected: Value '{name}' at '{field}' failed to satisfy constraint: Member must have length {bound}"
        )));
    }
    if !name.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')) {
        return Err(invalid(format!(
            "1 validation error detected: Value '{name}' at '{field}' failed to satisfy constraint: Member must satisfy regular expression pattern: [a-zA-Z0-9_.-]+"
        )));
    }
    Ok(())
}

fn check_projection(projection: &Projection) -> Result<()> {
    match projection.projection_type {
        ProjectionType::Include if projection.non_key_attributes.is_empty() => {
            Err(parameter_error("NonKeyAttributes must be specified when ProjectionType is INCLUDE"))
        }
        ProjectionType::All | ProjectionType::KeysOnly if !projection.non_key_attributes.is_empty() => {
            Err(parameter_error(format!(
                "ProjectionType is {}, but NonKeyAttributes is specified",
                projection.projection_type.as_str()
            )))
        }
        _ => Ok(()),
    }
}

fn sse_description(specification: Option<&SseSpecification>) -> Option<SseDescription> {
    specification.filter(|specification| specification.enabled).map(|specification| SseDescription {
        status: Some("ENABLED".into()),
        sse_type: Some(specification.sse_type.clone().unwrap_or_else(|| "KMS".into())),
        kms_master_key_arn: Some(
            specification
                .kms_master_key_id
                .clone()
                .unwrap_or_else(|| "arn:aws:kms:us-east-1:000000000000:key/aws-managed-dynamodb".into()),
        ),
    })
}

/// `CreateTable`.
pub fn create_table(state: &mut State, input: CreateTableInput) -> Result<TableDescription> {
    check_name(&input.table_name, "tableName")?;
    let mut defined = BTreeSet::new();
    for definition in &input.attribute_definitions {
        if !defined.insert(definition.name.as_str()) {
            return Err(parameter_error(format!("Duplicate AttributeName: {}", definition.name)));
        }
    }
    let key = KeyInfo::new(&input.key_schema, &input.attribute_definitions)?;
    match (input.billing_mode, input.provisioned_throughput) {
        (BillingMode::Provisioned, None) => {
            return Err(parameter_error(
                "ReadCapacityUnits and WriteCapacityUnits must both be specified when BillingMode is PROVISIONED",
            ));
        }
        (BillingMode::PayPerRequest, Some(_)) => {
            return Err(parameter_error(
                "Neither ReadCapacityUnits nor WriteCapacityUnits can be specified when BillingMode is PAY_PER_REQUEST",
            ));
        }
        _ => {}
    }
    if input.global_secondary_indexes.len() > 20 {
        return Err(parameter_error("GlobalSecondaryIndex count exceeds the per-table limit of 20"));
    }
    if input.local_secondary_indexes.len() > 5 {
        return Err(parameter_error("Table has more than 5 LocalSecondaryIndexes"));
    }
    let mut used: BTreeSet<String> = key.names().into_iter().map(str::to_owned).collect();
    let mut index_names = BTreeSet::new();
    let mut indexes = Vec::new();
    for local in &input.local_secondary_indexes {
        check_name(&local.index_name, "indexName")?;
        check_projection(&local.projection)?;
        if key.sort.is_none() {
            return Err(parameter_error(
                "Table KeySchema does not have a range key, which is required when specifying a LocalSecondaryIndex",
            ));
        }
        if local.key_schema.sort_key.is_none() {
            return Err(parameter_error(format!(
                "Index KeySchema does not have a range key for index: {}",
                local.index_name
            )));
        }
        if local.key_schema.partition_key != key.partition.name {
            return Err(parameter_error(format!(
                "Index KeySchema does not have the same leading hash key as table KeySchema for index: {}. index hash key: {}, table hash key: {}",
                local.index_name, local.key_schema.partition_key, key.partition.name
            )));
        }
        if !index_names.insert(local.index_name.clone()) {
            return Err(parameter_error(format!("Duplicate index name: {}", local.index_name)));
        }
        let index_key = KeyInfo::new(&local.key_schema, &input.attribute_definitions)?;
        used.extend(index_key.names().into_iter().map(str::to_owned));
        indexes.push(Index {
            name: local.index_name.clone(),
            global: false,
            key: index_key,
            projection: local.projection.clone(),
            entries: BTreeSet::new(),
            status: IndexStatus::Active,
            provisioned_throughput: None,
            on_demand_throughput: None,
        });
    }
    for global in &input.global_secondary_indexes {
        check_name(&global.index_name, "indexName")?;
        check_projection(&global.projection)?;
        if !index_names.insert(global.index_name.clone()) {
            return Err(parameter_error(format!("Duplicate index name: {}", global.index_name)));
        }
        match (input.billing_mode, global.provisioned_throughput) {
            (BillingMode::Provisioned, None) => {
                return Err(parameter_error(format!(
                    "ProvisionedThroughput is not specified for index: {}",
                    global.index_name
                )));
            }
            (BillingMode::PayPerRequest, Some(_)) => {
                return Err(parameter_error(
                    "ProvisionedThroughput should not be specified for index when BillingMode is PAY_PER_REQUEST",
                ));
            }
            _ => {}
        }
        let index_key = KeyInfo::new(&global.key_schema, &input.attribute_definitions)?;
        used.extend(index_key.names().into_iter().map(str::to_owned));
        indexes.push(Index {
            name: global.index_name.clone(),
            global: true,
            key: index_key,
            projection: global.projection.clone(),
            entries: BTreeSet::new(),
            status: IndexStatus::Active,
            provisioned_throughput: global.provisioned_throughput,
            on_demand_throughput: global.on_demand_throughput,
        });
    }
    let unused: Vec<&str> = defined.iter().copied().filter(|name| !used.contains(*name)).collect();
    if !unused.is_empty() {
        let mut all: Vec<&str> = defined.iter().copied().collect();
        all.sort_unstable();
        return Err(parameter_error(format!(
            "Some AttributeDefinitions are not used. AttributeDefinitions: [{}], keys used: [{}]",
            all.join(", "),
            used.iter().map(String::as_str).collect::<Vec<_>>().join(", ")
        )));
    }
    if let Some(stream) = &input.stream_specification
        && stream.enabled
        && stream.view_type.is_none()
    {
        return Err(parameter_error("StreamViewType is required when StreamEnabled is true"));
    }
    if state.tables.contains_key(&input.table_name) {
        return Err(ServiceError::resource_in_use(format!("Table already exists: {}", input.table_name)).into());
    }
    let created = now();
    let arn = format!("arn:aws:dynamodb:{}:{}:table/{}", state.region, state.account_id, input.table_name);
    let stream_enabled = input.stream_specification.is_some_and(|stream| stream.enabled);
    let table = Table {
        name: input.table_name.clone(),
        arn,
        id: format!("{}-{}-{}-{}-{}", random_hex(8), random_hex(4), random_hex(4), random_hex(4), random_hex(12)),
        created,
        key,
        attribute_definitions: input.attribute_definitions,
        items: BTreeMap::new(),
        indexes,
        billing_mode: input.billing_mode,
        provisioned_throughput: input.provisioned_throughput,
        on_demand_throughput: input.on_demand_throughput,
        stream_specification: input.stream_specification,
        stream_label: stream_enabled.then(|| stream_label(created)),
        sse: sse_description(input.sse_specification.as_ref()),
        table_class: input.table_class,
        deletion_protection: input.deletion_protection_enabled,
        ttl_attribute: None,
        tags: input.tags,
        pitr_days: None,
        describes_until_active: CREATING_DESCRIBES,
        index_describes_until_active: 0,
    };
    let description = table.describe(TableStatus::Creating);
    state.tables.insert(input.table_name, table);
    Ok(description)
}

fn stream_label(at: f64) -> String {
    let seconds = at as i64;
    let millis = ((at - seconds as f64) * 1000.0) as i64;
    format!("{seconds}.{millis:03}")
}

fn not_found(table_name: &str) -> Error {
    ServiceError::resource_not_found(format!("Requested resource not found: Table: {table_name} not found")).into()
}

/// `DescribeTable` calls that see a table or global index being created: the first sees it
/// `CREATING`, the next one `ACTIVE`.
const CREATING_DESCRIBES: u8 = 2;

/// `DescribeTable`: each call counts towards a table or index being created becoming active.
pub fn describe_table(state: &mut State, table_name: &str) -> Result<TableDescription> {
    let table = state.tables.get_mut(table_name).ok_or_else(|| not_found(table_name))?;
    table.describes_until_active = table.describes_until_active.saturating_sub(1);
    if table.index_describes_until_active > 0 {
        table.index_describes_until_active -= 1;
        if table.index_describes_until_active == 0 {
            for index in &mut table.indexes {
                index.status = IndexStatus::Active;
            }
        }
    }
    Ok(table.describe(if table.describes_until_active > 0 { TableStatus::Creating } else { TableStatus::Active }))
}

/// DynamoDB's refusal to change a table it is still creating (measured, docs/parity).
fn being_created(table: &Table) -> Error {
    ServiceError::new(
        "ResourceInUseException",
        format!("Attempt to change a resource which is still in use: Table is being created: {}", table.name),
    )
    .into()
}

/// `DeleteTable`.
pub fn delete_table(state: &mut State, table_name: &str) -> Result<TableDescription> {
    let table = state.tables.get(table_name).ok_or_else(|| not_found(table_name))?;
    if table.deletion_protection {
        return Err(invalid(
            "Resource cannot be deleted as it is currently protected against deletion. Disable deletion protection first.",
        ));
    }
    let description = table.describe(TableStatus::Deleting);
    state.tables.remove(table_name);
    Ok(description)
}

/// `UpdateTable`.
pub fn update_table(state: &mut State, input: UpdateTableInput) -> Result<TableDescription> {
    let table = state.tables.get_mut(&input.table_name).ok_or_else(|| not_found(&input.table_name))?;
    if table.describes_until_active > 0 {
        return Err(being_created(table));
    }
    let nothing = input.billing_mode.is_none()
        && input.provisioned_throughput.is_none()
        && input.on_demand_throughput.is_none()
        && input.global_secondary_index_updates.is_empty()
        && input.stream_specification.is_none()
        && input.sse_specification.is_none()
        && input.table_class.is_none()
        && input.deletion_protection_enabled.is_none();
    if nothing {
        return Err(invalid(
            "At least one of ProvisionedThroughput, BillingMode, UpdateStreamEnabled, GlobalSecondaryIndexUpdates, SSESpecification, TableClass or DeletionProtectionEnabled is required",
        ));
    }
    let index_operations = input
        .global_secondary_index_updates
        .iter()
        .filter(|update| {
            matches!(update, GlobalSecondaryIndexUpdate::Create(_) | GlobalSecondaryIndexUpdate::Delete { .. })
        })
        .count();
    let index_being_created = table.indexes.iter().any(|index| index.status == IndexStatus::Creating);
    if index_operations > 1 || (index_operations == 1 && index_being_created) {
        // Measured for an index added while another is being created, also once the table is
        // ACTIVE again during the other's backfill (docs/parity). A deletion then is refused too,
        // as the message says, but was not measured.
        return Err(ServiceError::new(
            "LimitExceededException",
            "Subscriber limit exceeded: Only 1 online index can be created or deleted simultaneously per table",
        )
        .into());
    }
    // DynamoDB applies nothing of a request it refuses: validate all of it before changing the table.
    let billing_mode = input.billing_mode.unwrap_or(table.billing_mode);
    if input.provisioned_throughput.is_some() && billing_mode != BillingMode::Provisioned {
        return Err(parameter_error(
            "Neither ReadCapacityUnits nor WriteCapacityUnits can be specified when BillingMode is PAY_PER_REQUEST",
        ));
    }
    let provisioned_throughput = match billing_mode {
        BillingMode::PayPerRequest => None,
        BillingMode::Provisioned => input.provisioned_throughput.or(table.provisioned_throughput),
    };
    if billing_mode == BillingMode::Provisioned && provisioned_throughput.is_none() {
        return Err(parameter_error(
            "ReadCapacityUnits and WriteCapacityUnits must both be specified when BillingMode is PROVISIONED",
        ));
    }
    // Global indexes have capacity of their own on a provisioned table, and none on an on-demand
    // one (measured, docs/parity).
    let updated_capacity = |name: &str| {
        input.global_secondary_index_updates.iter().any(|update| {
            matches!(update, GlobalSecondaryIndexUpdate::Update { index_name, provisioned_throughput: Some(_), .. }
                if index_name == name)
        })
    };
    if billing_mode == BillingMode::Provisioned
        && table.billing_mode == BillingMode::PayPerRequest
        && let Some(index) = table.indexes.iter().find(|index| index.global && !updated_capacity(&index.name))
    {
        return Err(parameter_error(format!("ProvisionedThroughput must be specified for index: {}", index.name)));
    }
    for update in &input.global_secondary_index_updates {
        match update {
            GlobalSecondaryIndexUpdate::Create(global)
                if billing_mode == BillingMode::Provisioned && global.provisioned_throughput.is_none() =>
            {
                return Err(parameter_error(format!(
                    "Both ReadCapacityUnits and WriteCapacityUnits must be specified for index: {}",
                    global.index_name
                )));
            }
            GlobalSecondaryIndexUpdate::Update { index_name, provisioned_throughput: Some(_), .. }
                if billing_mode == BillingMode::PayPerRequest =>
            {
                return Err(parameter_error(format!(
                    "The only Updates for index: {index_name} when TableThroughputMode is PAY_PER_REQUEST can be to OnDemandThroughput, WarmThroughput"
                )));
            }
            _ => {}
        }
    }
    let index_not_found = |index_name: &str| -> Error {
        ServiceError::resource_not_found(format!("Requested resource not found: Index: {index_name} not found")).into()
    };
    // The index to create, with the attribute definitions it adds (at most one, checked above).
    let mut created: Option<(Index, Vec<AttributeDefinition>)> = None;
    for update in &input.global_secondary_index_updates {
        match update {
            GlobalSecondaryIndexUpdate::Create(global) => {
                check_name(&global.index_name, "indexName")?;
                check_projection(&global.projection)?;
                if table.indexes.iter().any(|index| index.name == global.index_name) {
                    return Err(parameter_error(format!(
                        "Attempting to create an index which already exists: {}",
                        global.index_name
                    )));
                }
                if table.indexes.iter().filter(|index| index.global).count() >= 20 {
                    return Err(parameter_error("GlobalSecondaryIndex count exceeds the per-table limit of 20"));
                }
                let mut definitions = table.attribute_definitions.clone();
                for definition in &input.attribute_definitions {
                    match definitions.iter().find(|existing| existing.name == definition.name) {
                        Some(existing) if existing.attribute_type != definition.attribute_type => {
                            return Err(parameter_error(format!(
                                "Cannot change the type of attribute {} from {} to {}",
                                definition.name,
                                existing.attribute_type.as_str(),
                                definition.attribute_type.as_str()
                            )));
                        }
                        Some(_) => {}
                        None => definitions.push(definition.clone()),
                    }
                }
                let key = KeyInfo::new(&global.key_schema, &definitions)?;
                let index = Index {
                    name: global.index_name.clone(),
                    global: true,
                    key,
                    projection: global.projection.clone(),
                    entries: BTreeSet::new(),
                    status: IndexStatus::Creating,
                    provisioned_throughput: global.provisioned_throughput,
                    on_demand_throughput: global.on_demand_throughput,
                };
                created = Some((index, definitions));
            }
            GlobalSecondaryIndexUpdate::Update { index_name, .. }
            | GlobalSecondaryIndexUpdate::Delete { index_name } => {
                if !table.indexes.iter().any(|index| index.global && index.name == *index_name) {
                    return Err(index_not_found(index_name));
                }
            }
        }
    }
    if let Some(stream) = &input.stream_specification {
        let currently = table.stream_specification.is_some_and(|current| current.enabled);
        if stream.enabled && currently {
            return Err(invalid(format!("Table already has an enabled stream: {}", table.arn)));
        }
        if stream.enabled && stream.view_type.is_none() {
            return Err(parameter_error("StreamViewType is required when StreamEnabled is true"));
        }
    }
    // Apply it.
    if billing_mode == BillingMode::PayPerRequest && table.billing_mode == BillingMode::Provisioned {
        for index in &mut table.indexes {
            index.provisioned_throughput = None; // DynamoDB reports 0/0 from then on
        }
    }
    table.billing_mode = billing_mode;
    table.provisioned_throughput = provisioned_throughput;
    if let Some(on_demand) = input.on_demand_throughput {
        table.on_demand_throughput = Some(on_demand);
    }
    for update in input.global_secondary_index_updates {
        match update {
            GlobalSecondaryIndexUpdate::Create(_) => {
                let Some((mut index, definitions)) = created.take() else { continue };
                // Backfill: items whose index key attributes have the wrong type are not indexed.
                for (stored, item) in &table.items {
                    if index.check_item(item, false).is_ok()
                        && let Some(entry) = index.entry_key(item)
                    {
                        index.entries.insert((entry, stored.clone()));
                    }
                }
                table.indexes.push(index);
                table.index_describes_until_active = CREATING_DESCRIBES;
                table.attribute_definitions = definitions;
                table.retain_used_definitions();
            }
            GlobalSecondaryIndexUpdate::Update { index_name, provisioned_throughput, on_demand_throughput } => {
                if let Some(index) = table.indexes.iter_mut().find(|index| index.global && index.name == index_name) {
                    if provisioned_throughput.is_some() {
                        index.provisioned_throughput = provisioned_throughput;
                    }
                    if on_demand_throughput.is_some() {
                        index.on_demand_throughput = on_demand_throughput;
                    }
                }
            }
            GlobalSecondaryIndexUpdate::Delete { index_name } => {
                table.indexes.retain(|index| !(index.global && index.name == index_name));
                table.retain_used_definitions();
            }
        }
    }
    if let Some(stream) = input.stream_specification {
        table.stream_label = stream.enabled.then(|| stream_label(now()));
        table.stream_specification = Some(stream);
    }
    if let Some(sse) = input.sse_specification {
        table.sse = sse_description(Some(&sse));
    }
    if let Some(class) = input.table_class {
        table.table_class = Some(class);
    }
    if let Some(protection) = input.deletion_protection_enabled {
        table.deletion_protection = protection;
    }
    Ok(table.describe(TableStatus::Updating))
}

/// `ListTables`.
pub fn list_tables(state: &State, input: &ListTablesInput) -> Result<ListTablesOutput> {
    let limit = input.limit.unwrap_or(100);
    if !(1..=100).contains(&limit) {
        return Err(invalid(format!(
            "1 validation error detected: Value '{limit}' at 'limit' failed to satisfy constraint: Member must have value less than or equal to 100 and greater than or equal to 1"
        )));
    }
    let names: Vec<&String> = state
        .tables
        .keys()
        .filter(|name| input.exclusive_start_table_name.as_ref().is_none_or(|start| *name > start))
        .collect();
    let page: Vec<String> = names.iter().take(limit as usize).map(|name| (*name).clone()).collect();
    let last = (names.len() > page.len()).then(|| page.last().cloned()).flatten();
    Ok(ListTablesOutput { table_names: page, last_evaluated_table_name: last })
}

/// `UpdateTimeToLive`.
pub fn update_time_to_live(state: &mut State, input: &UpdateTimeToLiveInput) -> Result<TimeToLiveDescription> {
    let table = state.tables.get_mut(&input.table_name).ok_or_else(|| not_found(&input.table_name))?;
    if table.describes_until_active > 0 {
        // DynamoDB answers as if the table did not exist yet (measured, docs/parity).
        return Err(not_found(&input.table_name));
    }
    if input.attribute_name.is_empty() {
        return Err(invalid("TimeToLiveSpecification AttributeName must not be empty"));
    }
    match (&table.ttl_attribute, input.enabled) {
        (Some(_), true) => return Err(invalid("TimeToLive is already enabled")),
        (None, false) => return Err(invalid("TimeToLive is already disabled")),
        _ => {}
    }
    table.ttl_attribute = input.enabled.then(|| input.attribute_name.clone());
    Ok(TimeToLiveDescription {
        status: if input.enabled { TimeToLiveStatus::Enabled } else { TimeToLiveStatus::Disabled },
        attribute_name: Some(input.attribute_name.clone()),
    })
}

/// `DescribeTimeToLive`.
pub fn describe_time_to_live(state: &State, table_name: &str) -> Result<TimeToLiveDescription> {
    let table = state.tables.get(table_name).ok_or_else(|| not_found(table_name))?;
    Ok(match &table.ttl_attribute {
        Some(attribute) => {
            TimeToLiveDescription { status: TimeToLiveStatus::Enabled, attribute_name: Some(attribute.clone()) }
        }
        None => TimeToLiveDescription { status: TimeToLiveStatus::Disabled, attribute_name: None },
    })
}

fn table_by_arn<'a>(state: &'a mut State, arn: &str) -> Result<&'a mut Table> {
    state.tables.values_mut().find(|table| table.arn == arn).ok_or_else(|| {
        ServiceError::resource_not_found(format!("Requested resource not found: ResourceArn: {arn} not found")).into()
    })
}

/// `TagResource`.
pub fn tag_resource(state: &mut State, arn: &str, tags: BTreeMap<String, String>) -> Result<()> {
    let table = table_by_arn(state, arn)?;
    // A refused request adds none of its tags.
    let added = tags.keys().filter(|key| !table.tags.contains_key(*key)).count();
    if table.tags.len() + added > 50 {
        return Err(Error::from(ServiceError::new(
            "LimitExceededException",
            "Too many tags: a resource can have at most 50",
        )));
    }
    table.tags.extend(tags);
    Ok(())
}

/// `UntagResource`.
pub fn untag_resource(state: &mut State, arn: &str, keys: &[String]) -> Result<()> {
    let table = table_by_arn(state, arn)?;
    for key in keys {
        table.tags.remove(key);
    }
    Ok(())
}

/// `ListTagsOfResource`.
pub fn list_tags(state: &mut State, arn: &str) -> Result<ListTagsOutput> {
    let table = table_by_arn(state, arn)?;
    Ok(ListTagsOutput { tags: table.tags.clone(), next_token: None })
}

fn continuous_backups(table: &Table) -> ContinuousBackupsDescription {
    let current = now();
    ContinuousBackupsDescription {
        continuous_backups_status: ContinuousBackupsStatus::Enabled,
        point_in_time_recovery_status: if table.pitr_days.is_some() {
            ContinuousBackupsStatus::Enabled
        } else {
            ContinuousBackupsStatus::Disabled
        },
        recovery_period_in_days: table.pitr_days,
        earliest_restorable_date_time: table.pitr_days.map(|_| table.created.max(current - 35.0 * 86_400.0)),
        latest_restorable_date_time: table.pitr_days.map(|_| current),
    }
}

/// `DescribeContinuousBackups`.
pub fn describe_continuous_backups(state: &State, table_name: &str) -> Result<ContinuousBackupsDescription> {
    let table = state.tables.get(table_name).ok_or_else(|| {
        Error::from(ServiceError::new("TableNotFoundException", format!("Table not found: {table_name}")))
    })?;
    Ok(continuous_backups(table))
}

/// `UpdateContinuousBackups`.
pub fn update_continuous_backups(
    state: &mut State,
    table_name: &str,
    enabled: bool,
    recovery_period_in_days: Option<i64>,
) -> Result<ContinuousBackupsDescription> {
    let table = state.tables.get_mut(table_name).ok_or_else(|| {
        Error::from(ServiceError::new("TableNotFoundException", format!("Table not found: {table_name}")))
    })?;
    if let Some(days) = recovery_period_in_days
        && !(1..=35).contains(&days)
    {
        return Err(invalid(format!(
            "1 validation error detected: Value '{days}' at 'pointInTimeRecoverySpecification.recoveryPeriodInDays' failed to satisfy constraint: Member must have value between 1 and 35"
        )));
    }
    table.pitr_days = enabled.then(|| recovery_period_in_days.unwrap_or(35));
    Ok(continuous_backups(table))
}

fn backup_description(backup: &Backup) -> BackupDescription {
    BackupDescription {
        details: backup.details.clone(),
        table_name: backup.table.name.clone(),
        table_arn: Some(backup.table.arn.clone()),
        table_id: Some(backup.table.id.clone()),
        key_schema: Some(backup.table.key.schema()),
        item_count: Some(backup.table.items.len() as i64),
    }
}

/// `CreateBackup`.
pub fn create_backup(state: &mut State, table_name: &str, backup_name: &str) -> Result<BackupDetails> {
    check_name(backup_name, "backupName")?;
    let table = state.tables.get(table_name).ok_or_else(|| {
        Error::from(ServiceError::new("TableNotFoundException", format!("Table not found: {table_name}")))
    })?;
    let created = now();
    let details = BackupDetails {
        backup_arn: format!("{}/backup/{:013}-{}", table.arn, (created * 1000.0) as i64, random_hex(8)),
        backup_name: backup_name.to_owned(),
        backup_size_bytes: Some(table.describe(TableStatus::Active).table_size_bytes),
        backup_status: BackupStatus::Available,
        backup_type: BackupType::User,
        backup_creation_date_time: created,
        backup_expiry_date_time: None,
    };
    let backup = Backup { details: details.clone(), table: table.clone() };
    state.backups.insert(details.backup_arn.clone(), backup);
    Ok(details)
}

fn backup_not_found(arn: &str) -> Error {
    ServiceError::new("BackupNotFoundException", format!("Backup not found: {arn}")).into()
}

/// `DescribeBackup`.
pub fn describe_backup(state: &State, arn: &str) -> Result<BackupDescription> {
    state.backups.get(arn).map(backup_description).ok_or_else(|| backup_not_found(arn))
}

/// `DeleteBackup`.
pub fn delete_backup(state: &mut State, arn: &str) -> Result<BackupDescription> {
    let mut backup = state.backups.remove(arn).ok_or_else(|| backup_not_found(arn))?;
    backup.details.backup_status = BackupStatus::Deleted;
    Ok(backup_description(&backup))
}

/// `ListBackups`.
pub fn list_backups(state: &State, input: &ListBackupsInput) -> Result<ListBackupsOutput> {
    let matching: Vec<&Backup> = state
        .backups
        .values()
        .filter(|backup| input.table_name.as_ref().is_none_or(|name| &backup.table.name == name))
        .filter(|backup| match input.backup_type {
            None | Some(BackupTypeFilter::All) => true,
            Some(BackupTypeFilter::User) => backup.details.backup_type == BackupType::User,
            Some(BackupTypeFilter::System) => backup.details.backup_type == BackupType::System,
            Some(BackupTypeFilter::AwsBackup) => backup.details.backup_type == BackupType::AwsBackup,
        })
        .filter(|backup| {
            input.time_range_lower_bound.is_none_or(|lower| backup.details.backup_creation_date_time >= lower)
        })
        .filter(|backup| {
            input.time_range_upper_bound.is_none_or(|upper| backup.details.backup_creation_date_time < upper)
        })
        .filter(|backup| {
            input.exclusive_start_backup_arn.as_ref().is_none_or(|start| &backup.details.backup_arn > start)
        })
        .collect();
    let limit = input.limit.unwrap_or(100).max(1) as usize;
    let page: Vec<&Backup> = matching.iter().take(limit).copied().collect();
    Ok(ListBackupsOutput {
        last_evaluated_backup_arn: (matching.len() > page.len())
            .then(|| page.last().map(|backup| backup.details.backup_arn.clone()))
            .flatten(),
        backups: page
            .into_iter()
            .map(|backup| crate::types::BackupSummary {
                details: backup.details.clone(),
                table_name: backup.table.name.clone(),
                table_arn: Some(backup.table.arn.clone()),
            })
            .collect(),
    })
}

fn restore(
    state: &mut State,
    source: Table,
    target_name: &str,
    overrides: RestoreOverrides,
) -> Result<TableDescription> {
    check_name(target_name, "targetTableName")?;
    if state.tables.contains_key(target_name) {
        return Err(
            ServiceError::new("TableAlreadyExistsException", format!("Table already exists: {target_name}")).into()
        );
    }
    let mut table = source;
    table.name = target_name.to_owned();
    table.arn = format!("arn:aws:dynamodb:{}:{}:table/{target_name}", state.region, state.account_id);
    table.id = format!("{}-{}-{}-{}-{}", random_hex(8), random_hex(4), random_hex(4), random_hex(4), random_hex(12));
    table.created = now();
    table.tags = BTreeMap::new();
    table.ttl_attribute = None;
    table.pitr_days = None;
    table.stream_specification = None;
    table.stream_label = None;
    table.deletion_protection = false;
    table.describes_until_active = CREATING_DESCRIBES;
    table.index_describes_until_active = 0;
    if let Some(mode) = overrides.billing_mode {
        table.billing_mode = mode;
    }
    if overrides.provisioned_throughput.is_some() {
        table.provisioned_throughput = overrides.provisioned_throughput;
    }
    if let Some(sse) = overrides.sse_specification {
        table.sse = sse_description(Some(&sse));
    }
    let description = table.describe(TableStatus::Creating);
    state.tables.insert(target_name.to_owned(), table);
    Ok(description)
}

/// `RestoreTableFromBackup`.
pub fn restore_table_from_backup(state: &mut State, input: RestoreTableFromBackupInput) -> Result<TableDescription> {
    let backup = state.backups.get(&input.backup_arn).ok_or_else(|| backup_not_found(&input.backup_arn))?;
    let source = backup.table.clone();
    restore(state, source, &input.target_table_name, input.overrides)
}

/// `RestoreTableToPointInTime`. The in-memory backend keeps no history: it restores the current
/// contents of the source table (documented limitation).
pub fn restore_table_to_point_in_time(
    state: &mut State,
    input: RestoreTableToPointInTimeInput,
) -> Result<TableDescription> {
    let source = state.tables.get(&input.source_table_name).ok_or_else(|| {
        Error::from(ServiceError::new(
            "TableNotFoundException",
            format!("Table not found: {}", input.source_table_name),
        ))
    })?;
    if source.pitr_days.is_none() {
        return Err(ServiceError::new(
            "PointInTimeRecoveryUnavailableException",
            format!("Point in time recovery is not enabled for table '{}'", input.source_table_name),
        )
        .into());
    }
    if !input.use_latest_restorable_time && input.restore_date_time.is_none() {
        return Err(invalid("Either RestoreDateTime or UseLatestRestorableTime must be specified"));
    }
    let source = source.clone();
    restore(state, source, &input.target_table_name, input.overrides)
}
