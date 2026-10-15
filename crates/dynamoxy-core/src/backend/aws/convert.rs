//! Conversions between dynamoxy's types and the AWS SDK's. SDK types stay in this module tree.

use std::collections::{BTreeMap, HashMap};

use aws_sdk_dynamodb::primitives::{Blob, DateTime};
use aws_sdk_dynamodb::types as sdk;

use crate::error::{Error, Result};
use crate::expr::{Names, Values};
use crate::types::{
    AttributeDefinition, BackupDescription, BackupDetails, BackupStatus, BackupSummary, BackupType, BillingMode,
    Capacity, ConsumedCapacity, ContinuousBackupsDescription, ContinuousBackupsStatus, IndexDescription, IndexStatus,
    ItemCollectionMetrics, KeySchema, OnDemandThroughput, Projection, ProjectionType, ProvisionedThroughput,
    ReturnConsumedCapacity, ReturnValues, ReturnValuesOnConditionCheckFailure, ScalarType, Select, SseDescription,
    SseSpecification, StreamSpecification, StreamViewType, TableClass, TableDescription, TableStatus,
};
use crate::value::{AttributeValue, Item, Number};

/// A response that does not have the shape DynamoDB documents.
pub fn protocol_error(message: impl std::fmt::Display) -> Error {
    Error::Transport {
        operation: "",
        message: format!("unexpected response from DynamoDB: {message}"),
        retryable: false,
    }
}

/// A request that the SDK refused to build (a required field is missing).
pub fn build_error(error: impl std::fmt::Display) -> Error {
    Error::Validation(format!("invalid request: {error}"))
}

// ---- values -------------------------------------------------------------------------------

/// Converts a value to the SDK's representation.
pub fn value_to_sdk(value: AttributeValue) -> sdk::AttributeValue {
    match value {
        AttributeValue::S(text) => sdk::AttributeValue::S(text),
        AttributeValue::N(number) => sdk::AttributeValue::N(number.into_string()),
        AttributeValue::B(bytes) => sdk::AttributeValue::B(Blob::new(bytes)),
        AttributeValue::Bool(value) => sdk::AttributeValue::Bool(value),
        AttributeValue::Null => sdk::AttributeValue::Null(true),
        AttributeValue::L(values) => sdk::AttributeValue::L(values.into_iter().map(value_to_sdk).collect()),
        AttributeValue::M(values) => sdk::AttributeValue::M(item_to_sdk(values)),
        AttributeValue::Ss(values) => sdk::AttributeValue::Ss(values),
        AttributeValue::Ns(values) => sdk::AttributeValue::Ns(values.into_iter().map(Number::into_string).collect()),
        AttributeValue::Bs(values) => sdk::AttributeValue::Bs(values.into_iter().map(Blob::new).collect()),
    }
}

/// Converts an item to the SDK's representation.
pub fn item_to_sdk(item: Item) -> HashMap<String, sdk::AttributeValue> {
    item.into_iter().map(|(name, value)| (name, value_to_sdk(value))).collect()
}

fn number(text: String) -> Result<Number> {
    // DynamoDB returns canonical numbers: they are kept as they are, without a copy.
    Number::parse_owned(text).map_err(|error| protocol_error(format!("invalid number: {error}")))
}

/// Converts a value from the SDK's representation.
pub fn value_from_sdk(value: sdk::AttributeValue) -> Result<AttributeValue> {
    Ok(match value {
        sdk::AttributeValue::S(text) => AttributeValue::S(text),
        sdk::AttributeValue::N(text) => AttributeValue::N(number(text)?),
        sdk::AttributeValue::B(blob) => AttributeValue::B(blob.into_inner()),
        sdk::AttributeValue::Bool(value) => AttributeValue::Bool(value),
        sdk::AttributeValue::Null(_) => AttributeValue::Null,
        sdk::AttributeValue::L(values) => {
            AttributeValue::L(values.into_iter().map(value_from_sdk).collect::<Result<_>>()?)
        }
        sdk::AttributeValue::M(values) => AttributeValue::M(item_from_sdk(values)?),
        sdk::AttributeValue::Ss(values) => AttributeValue::Ss(values),
        sdk::AttributeValue::Ns(values) => AttributeValue::Ns(values.into_iter().map(number).collect::<Result<_>>()?),
        sdk::AttributeValue::Bs(values) => AttributeValue::Bs(values.into_iter().map(Blob::into_inner).collect()),
        other => return Err(protocol_error(format!("unsupported attribute value {other:?}"))),
    })
}

/// Converts an item from the SDK's representation.
pub fn item_from_sdk(item: HashMap<String, sdk::AttributeValue>) -> Result<Item> {
    item.into_iter().map(|(name, value)| Ok((name, value_from_sdk(value)?))).collect()
}

/// Converts an optional item; an empty map (DynamoDB omits nothing) stays an empty item.
pub fn optional_item(item: Option<HashMap<String, sdk::AttributeValue>>) -> Result<Option<Item>> {
    item.map(item_from_sdk).transpose()
}

/// Expression attribute names, omitted when empty (DynamoDB rejects empty maps).
pub fn names(names: Names) -> Option<HashMap<String, String>> {
    (!names.is_empty()).then(|| names.into_iter().collect())
}

/// Expression attribute values, omitted when empty (DynamoDB rejects empty maps).
pub fn values(values: Values) -> Option<HashMap<String, sdk::AttributeValue>> {
    (!values.is_empty()).then(|| values.into_iter().map(|(name, value)| (name, value_to_sdk(value))).collect())
}

// ---- request enums --------------------------------------------------------------------------

/// `ReturnConsumedCapacity` (omitted for `NONE`).
pub fn return_consumed_capacity(value: ReturnConsumedCapacity) -> Option<sdk::ReturnConsumedCapacity> {
    (value != ReturnConsumedCapacity::None).then(|| sdk::ReturnConsumedCapacity::from(value.as_str()))
}

/// `ReturnValues` (omitted for `NONE`).
pub fn return_values(value: ReturnValues) -> Option<sdk::ReturnValue> {
    (value != ReturnValues::None).then(|| sdk::ReturnValue::from(value.as_str()))
}

/// `ReturnValuesOnConditionCheckFailure` (omitted for `NONE`).
pub fn return_on_failure(
    value: ReturnValuesOnConditionCheckFailure,
) -> Option<sdk::ReturnValuesOnConditionCheckFailure> {
    (value != ReturnValuesOnConditionCheckFailure::None)
        .then(|| sdk::ReturnValuesOnConditionCheckFailure::from(value.as_str()))
}

/// `ReturnItemCollectionMetrics` (`SIZE` or omitted).
pub fn return_item_collection_metrics(enabled: bool) -> Option<sdk::ReturnItemCollectionMetrics> {
    enabled.then_some(sdk::ReturnItemCollectionMetrics::Size)
}

/// `Select`.
pub fn select(value: Option<Select>) -> Option<sdk::Select> {
    value.map(|value| sdk::Select::from(value.as_str()))
}

// ---- capacity and metrics -------------------------------------------------------------------

fn capacity(value: &sdk::Capacity) -> Capacity {
    Capacity {
        read_capacity_units: value.read_capacity_units().unwrap_or_default(),
        write_capacity_units: value.write_capacity_units().unwrap_or_default(),
        capacity_units: value.capacity_units().unwrap_or_default(),
    }
}

fn capacity_map(values: Option<&HashMap<String, sdk::Capacity>>) -> BTreeMap<String, Capacity> {
    values
        .map(|values| values.iter().map(|(name, value)| (name.clone(), capacity(value))).collect())
        .unwrap_or_default()
}

/// Converts consumed capacity.
pub fn consumed_capacity(value: &sdk::ConsumedCapacity) -> ConsumedCapacity {
    ConsumedCapacity {
        table_name: value.table_name().unwrap_or_default().to_owned(),
        capacity_units: value.capacity_units().unwrap_or_default(),
        read_capacity_units: value.read_capacity_units().unwrap_or_default(),
        write_capacity_units: value.write_capacity_units().unwrap_or_default(),
        table: value.table().map(capacity),
        local_secondary_indexes: capacity_map(value.local_secondary_indexes()),
        global_secondary_indexes: capacity_map(value.global_secondary_indexes()),
    }
}

/// Converts a list of consumed capacities.
pub fn consumed_capacities(values: Option<&[sdk::ConsumedCapacity]>) -> Vec<ConsumedCapacity> {
    values.unwrap_or_default().iter().map(consumed_capacity).collect()
}

/// Converts item collection metrics.
pub fn item_collection_metrics(value: sdk::ItemCollectionMetrics) -> Result<ItemCollectionMetrics> {
    Ok(ItemCollectionMetrics {
        size_estimate_range_gb: value.size_estimate_range_gb().to_vec(),
        item_collection_key: optional_item(value.item_collection_key)?.unwrap_or_default(),
    })
}

/// Converts per-table item collection metrics.
pub fn item_collection_metrics_map(
    values: Option<HashMap<String, Vec<sdk::ItemCollectionMetrics>>>,
) -> Result<BTreeMap<String, Vec<ItemCollectionMetrics>>> {
    values
        .unwrap_or_default()
        .into_iter()
        .map(|(table, metrics)| Ok((table, metrics.into_iter().map(item_collection_metrics).collect::<Result<_>>()?)))
        .collect()
}

// ---- schema ---------------------------------------------------------------------------------

/// Converts a key schema.
pub fn key_schema_to_sdk(schema: &KeySchema) -> Result<Vec<sdk::KeySchemaElement>> {
    let mut elements = vec![
        sdk::KeySchemaElement::builder()
            .attribute_name(&schema.partition_key)
            .key_type(sdk::KeyType::Hash)
            .build()
            .map_err(build_error)?,
    ];
    if let Some(sort) = &schema.sort_key {
        elements.push(
            sdk::KeySchemaElement::builder()
                .attribute_name(sort)
                .key_type(sdk::KeyType::Range)
                .build()
                .map_err(build_error)?,
        );
    }
    Ok(elements)
}

/// Converts a key schema from the SDK.
pub fn key_schema_from_sdk(elements: &[sdk::KeySchemaElement]) -> KeySchema {
    let find = |kind: sdk::KeyType| {
        elements.iter().find(|element| *element.key_type() == kind).map(|element| element.attribute_name().to_owned())
    };
    KeySchema { partition_key: find(sdk::KeyType::Hash).unwrap_or_default(), sort_key: find(sdk::KeyType::Range) }
}

/// Converts attribute definitions.
pub fn attribute_definitions_to_sdk(definitions: &[AttributeDefinition]) -> Result<Vec<sdk::AttributeDefinition>> {
    definitions
        .iter()
        .map(|definition| {
            sdk::AttributeDefinition::builder()
                .attribute_name(&definition.name)
                .attribute_type(sdk::ScalarAttributeType::from(definition.attribute_type.as_str()))
                .build()
                .map_err(build_error)
        })
        .collect()
}

fn attribute_definitions_from_sdk(definitions: &[sdk::AttributeDefinition]) -> Vec<AttributeDefinition> {
    definitions
        .iter()
        .filter_map(|definition| {
            Some(AttributeDefinition {
                name: definition.attribute_name().to_owned(),
                attribute_type: ScalarType::parse(definition.attribute_type().as_str())?,
            })
        })
        .collect()
}

/// Converts a projection.
pub fn projection_to_sdk(projection: &Projection) -> sdk::Projection {
    sdk::Projection::builder()
        .projection_type(sdk::ProjectionType::from(projection.projection_type.as_str()))
        .set_non_key_attributes(
            (!projection.non_key_attributes.is_empty()).then(|| projection.non_key_attributes.clone()),
        )
        .build()
}

fn projection_from_sdk(projection: Option<&sdk::Projection>) -> Projection {
    Projection {
        projection_type: projection
            .and_then(|projection| projection.projection_type())
            .and_then(|kind| ProjectionType::parse(kind.as_str()))
            .unwrap_or(ProjectionType::All),
        non_key_attributes: projection.map(|projection| projection.non_key_attributes().to_vec()).unwrap_or_default(),
    }
}

/// Converts provisioned throughput.
pub fn throughput_to_sdk(throughput: &ProvisionedThroughput) -> Result<sdk::ProvisionedThroughput> {
    sdk::ProvisionedThroughput::builder()
        .read_capacity_units(throughput.read_capacity_units)
        .write_capacity_units(throughput.write_capacity_units)
        .build()
        .map_err(build_error)
}

fn throughput_from_sdk(throughput: Option<&sdk::ProvisionedThroughputDescription>) -> Option<ProvisionedThroughput> {
    let throughput = throughput?;
    let read = throughput.read_capacity_units().unwrap_or_default();
    let write = throughput.write_capacity_units().unwrap_or_default();
    // On-demand tables report 0/0.
    (read > 0 || write > 0).then_some(ProvisionedThroughput { read_capacity_units: read, write_capacity_units: write })
}

/// Converts on-demand throughput.
pub fn on_demand_to_sdk(value: &OnDemandThroughput) -> sdk::OnDemandThroughput {
    sdk::OnDemandThroughput::builder()
        .set_max_read_request_units(value.max_read_request_units)
        .set_max_write_request_units(value.max_write_request_units)
        .build()
}

fn on_demand_from_sdk(value: Option<&sdk::OnDemandThroughput>) -> Option<OnDemandThroughput> {
    value.map(|value| OnDemandThroughput {
        max_read_request_units: value.max_read_request_units(),
        max_write_request_units: value.max_write_request_units(),
    })
}

/// Converts a stream specification.
pub fn stream_to_sdk(stream: &StreamSpecification) -> Result<sdk::StreamSpecification> {
    sdk::StreamSpecification::builder()
        .stream_enabled(stream.enabled)
        .set_stream_view_type(stream.view_type.map(|view| sdk::StreamViewType::from(view.as_str())))
        .build()
        .map_err(build_error)
}

/// Converts encryption settings.
pub fn sse_to_sdk(sse: &SseSpecification) -> sdk::SseSpecification {
    sdk::SseSpecification::builder()
        .enabled(sse.enabled)
        .set_sse_type(sse.sse_type.as_deref().map(sdk::SseType::from))
        .set_kms_master_key_id(sse.kms_master_key_id.clone())
        .build()
}

fn secs(value: Option<&DateTime>) -> Option<f64> {
    value.map(DateTime::as_secs_f64)
}

/// Converts a table description.
pub fn table_description(description: sdk::TableDescription) -> TableDescription {
    let globals = description
        .global_secondary_indexes()
        .iter()
        .map(|index| IndexDescription {
            index_name: index.index_name().unwrap_or_default().to_owned(),
            key_schema: key_schema_from_sdk(index.key_schema()),
            projection: projection_from_sdk(index.projection()),
            index_status: index.index_status().and_then(|status| IndexStatus::parse(status.as_str())),
            item_count: index.item_count().unwrap_or_default(),
            index_size_bytes: index.index_size_bytes().unwrap_or_default(),
            index_arn: index.index_arn().map(str::to_owned),
            provisioned_throughput: throughput_from_sdk(index.provisioned_throughput()),
            backfilling: index.backfilling().unwrap_or(false),
        })
        .collect();
    let locals = description
        .local_secondary_indexes()
        .iter()
        .map(|index| IndexDescription {
            index_name: index.index_name().unwrap_or_default().to_owned(),
            key_schema: key_schema_from_sdk(index.key_schema()),
            projection: projection_from_sdk(index.projection()),
            index_status: None,
            item_count: index.item_count().unwrap_or_default(),
            index_size_bytes: index.index_size_bytes().unwrap_or_default(),
            index_arn: index.index_arn().map(str::to_owned),
            provisioned_throughput: None,
            backfilling: false,
        })
        .collect();
    let billing_mode = description
        .billing_mode_summary()
        .and_then(|summary| summary.billing_mode())
        .and_then(|mode| BillingMode::parse(mode.as_str()))
        .unwrap_or_else(|| {
            // Tables created as provisioned before billing modes existed report no summary.
            if throughput_from_sdk(description.provisioned_throughput()).is_some() {
                BillingMode::Provisioned
            } else {
                BillingMode::PayPerRequest
            }
        });
    TableDescription {
        table_name: description.table_name().unwrap_or_default().to_owned(),
        table_arn: description.table_arn().map(str::to_owned),
        table_id: description.table_id().map(str::to_owned),
        table_status: description
            .table_status()
            .and_then(|status| TableStatus::parse(status.as_str()))
            .unwrap_or(TableStatus::Active),
        creation_date_time: secs(description.creation_date_time()),
        key_schema: key_schema_from_sdk(description.key_schema()),
        attribute_definitions: attribute_definitions_from_sdk(description.attribute_definitions()),
        item_count: description.item_count().unwrap_or_default(),
        table_size_bytes: description.table_size_bytes().unwrap_or_default(),
        billing_mode,
        provisioned_throughput: throughput_from_sdk(description.provisioned_throughput()),
        on_demand_throughput: on_demand_from_sdk(description.on_demand_throughput()),
        global_secondary_indexes: globals,
        local_secondary_indexes: locals,
        stream_specification: description.stream_specification().map(|stream| StreamSpecification {
            enabled: stream.stream_enabled(),
            view_type: stream.stream_view_type().and_then(|view| StreamViewType::parse(view.as_str())),
        }),
        latest_stream_arn: description.latest_stream_arn().map(str::to_owned),
        latest_stream_label: description.latest_stream_label().map(str::to_owned),
        sse_description: description.sse_description().map(|sse| SseDescription {
            status: sse.status().map(|status| status.as_str().to_owned()),
            sse_type: sse.sse_type().map(|kind| kind.as_str().to_owned()),
            kms_master_key_arn: sse.kms_master_key_arn().map(str::to_owned),
        }),
        table_class: description
            .table_class_summary()
            .and_then(|summary| summary.table_class())
            .and_then(|class| TableClass::parse(class.as_str())),
        deletion_protection_enabled: description.deletion_protection_enabled().unwrap_or(false),
    }
}

/// Converts backup details.
pub fn backup_details(details: &sdk::BackupDetails) -> BackupDetails {
    BackupDetails {
        backup_arn: details.backup_arn().to_owned(),
        backup_name: details.backup_name().to_owned(),
        backup_size_bytes: details.backup_size_bytes(),
        backup_status: BackupStatus::parse(details.backup_status().as_str()).unwrap_or(BackupStatus::Available),
        backup_type: BackupType::parse(details.backup_type().as_str()).unwrap_or(BackupType::User),
        backup_creation_date_time: details.backup_creation_date_time().as_secs_f64(),
        backup_expiry_date_time: secs(details.backup_expiry_date_time()),
    }
}

/// Converts a backup description.
pub fn backup_description(description: &sdk::BackupDescription) -> Result<BackupDescription> {
    let details = description.backup_details().ok_or_else(|| protocol_error("backup description without details"))?;
    let source = description.source_table_details();
    Ok(BackupDescription {
        details: backup_details(details),
        table_name: source.map(|source| source.table_name().to_owned()).unwrap_or_default(),
        table_arn: source.and_then(|source| source.table_arn().map(str::to_owned)),
        table_id: source.map(|source| source.table_id().to_owned()),
        key_schema: source.map(|source| key_schema_from_sdk(source.key_schema())),
        item_count: source.and_then(sdk::SourceTableDetails::item_count),
    })
}

/// Converts a backup summary.
pub fn backup_summary(summary: &sdk::BackupSummary) -> BackupSummary {
    BackupSummary {
        details: BackupDetails {
            backup_arn: summary.backup_arn().unwrap_or_default().to_owned(),
            backup_name: summary.backup_name().unwrap_or_default().to_owned(),
            backup_size_bytes: summary.backup_size_bytes(),
            backup_status: summary
                .backup_status()
                .and_then(|status| BackupStatus::parse(status.as_str()))
                .unwrap_or(BackupStatus::Available),
            backup_type: summary
                .backup_type()
                .and_then(|kind| BackupType::parse(kind.as_str()))
                .unwrap_or(BackupType::User),
            backup_creation_date_time: secs(summary.backup_creation_date_time()).unwrap_or_default(),
            backup_expiry_date_time: secs(summary.backup_expiry_date_time()),
        },
        table_name: summary.table_name().unwrap_or_default().to_owned(),
        table_arn: summary.table_arn().map(str::to_owned),
    }
}

/// Converts the continuous backups state.
pub fn continuous_backups(description: &sdk::ContinuousBackupsDescription) -> ContinuousBackupsDescription {
    let recovery = description.point_in_time_recovery_description();
    ContinuousBackupsDescription {
        continuous_backups_status: ContinuousBackupsStatus::parse(description.continuous_backups_status().as_str())
            .unwrap_or(ContinuousBackupsStatus::Disabled),
        point_in_time_recovery_status: recovery
            .and_then(|recovery| recovery.point_in_time_recovery_status())
            .and_then(|status| ContinuousBackupsStatus::parse(status.as_str()))
            .unwrap_or(ContinuousBackupsStatus::Disabled),
        recovery_period_in_days: recovery.and_then(|recovery| recovery.recovery_period_in_days()).map(i64::from),
        earliest_restorable_date_time: recovery.and_then(|recovery| secs(recovery.earliest_restorable_date_time())),
        latest_restorable_date_time: recovery.and_then(|recovery| secs(recovery.latest_restorable_date_time())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_round_trip_through_the_sdk() {
        let original = AttributeValue::M(crate::value::item([
            ("s", AttributeValue::s("x")),
            ("n", AttributeValue::n_str("1.5")),
            ("b", AttributeValue::b(vec![1, 2])),
            ("null", AttributeValue::Null),
            ("bool", AttributeValue::Bool(true)),
            ("l", AttributeValue::L(vec![AttributeValue::n(1)])),
            ("ss", AttributeValue::Ss(vec!["a".into()])),
            ("ns", AttributeValue::Ns(vec![Number::from(1)])),
            ("bs", AttributeValue::Bs(vec![vec![0]])),
        ]));
        assert_eq!(value_from_sdk(value_to_sdk(original.clone())).unwrap(), original);
        assert!(value_from_sdk(sdk::AttributeValue::N("not a number".into())).is_err());
    }

    #[test]
    fn empty_placeholder_maps_are_omitted() {
        assert!(names(Names::new()).is_none());
        assert!(values(Values::new()).is_none());
        assert!(return_values(ReturnValues::None).is_none());
        assert_eq!(return_values(ReturnValues::AllNew), Some(sdk::ReturnValue::AllNew));
    }

    #[test]
    fn key_schemas_round_trip() {
        let schema = KeySchema { partition_key: "pk".into(), sort_key: Some("sk".into()) };
        assert_eq!(key_schema_from_sdk(&key_schema_to_sdk(&schema).unwrap()), schema);
    }
}
