//! `BatchGetItem` and `BatchWriteItem`: up to 100 reads or 25 writes in one request. Not
//! atomic: each item succeeds on its own (but an invalid request fails before anything is written).

use std::collections::{BTreeMap, BTreeSet};

use super::State;
use super::items::{Failure, apply, prepare_delete, prepare_put};
use super::table::{CapacityMeter, StoredKey, read_units};
use crate::error::{Error, Result};
use crate::expr::{ExpressionInput, Projector, parse_request};
use crate::types::{
    BatchGetItemInput, BatchGetItemOutput, BatchWriteItemInput, BatchWriteItemOutput, KeysAndAttributes,
    ReturnValuesOnConditionCheckFailure, WriteRequest,
};
use crate::value::{BATCH_GET_RESPONSE_LIMIT, item_size};

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

/// `BatchGetItem`. With `unprocessed` (fault injection), the keys of that table
/// (`Some(Some(table))`) or every key (`Some(None)`) are returned unprocessed.
pub fn batch_get_item(
    state: &State,
    input: &BatchGetItemInput,
    unprocessed: Option<Option<&str>>,
) -> Result<BatchGetItemOutput> {
    if input.request_items.is_empty() {
        return Err(invalid(
            "1 validation error detected: Value '{}' at 'requestItems' failed to satisfy constraint: Member must have length greater than or equal to 1",
        ));
    }
    let total: usize = input.request_items.values().map(|request| request.keys.len()).sum();
    if total > 100 {
        return Err(invalid("Too many items requested for the BatchGetItem call"));
    }
    let mut parsed = Vec::with_capacity(input.request_items.len());
    for (table_name, request) in &input.request_items {
        if request.keys.is_empty() {
            return Err(invalid(format!(
                "1 validation error detected: Value '[]' at 'requestItems.{table_name}.member.keys' failed to satisfy constraint: Member must have length greater than or equal to 1"
            )));
        }
        let table = state.table(table_name)?;
        let mut positions = BTreeMap::new();
        for (position, key) in request.keys.iter().enumerate() {
            if positions.insert(table.key.exact_key(key)?, position).is_some() {
                return Err(invalid("Provided list of item keys contains duplicates"));
            }
        }
        let mut stored: Vec<Option<StoredKey>> = vec![None; request.keys.len()];
        for (key, position) in positions {
            stored[position] = Some(key);
        }
        let projection = parse_request(
            ExpressionInput { projection: request.projection_expression.as_deref(), ..Default::default() },
            &request.expression_attribute_names,
            &Default::default(),
        )?
        .projection;
        parsed.push((stored, projection.as_deref().map(Projector::new)));
    }
    let mut output = BatchGetItemOutput::default();
    let mut meter = CapacityMeter::default();
    let mut returned = 0usize;
    for ((table_name, request), (stored_keys, projection)) in input.request_items.iter().zip(&parsed) {
        if unprocessed.is_some_and(|only| only.is_none_or(|only| only == table_name)) {
            output.unprocessed_keys.insert(table_name.clone(), request.clone());
            continue;
        }
        let table = state.table(table_name)?;
        for (key, stored) in request.keys.iter().zip(stored_keys) {
            if returned >= BATCH_GET_RESPONSE_LIMIT {
                output
                    .unprocessed_keys
                    .entry(table_name.clone())
                    .or_insert_with(|| KeysAndAttributes {
                        keys: Vec::new(),
                        consistent_read: request.consistent_read,
                        projection_expression: request.projection_expression.clone(),
                        expression_attribute_names: request.expression_attribute_names.clone(),
                    })
                    .keys
                    .push(key.clone());
                continue;
            }
            let found = stored.as_ref().and_then(|stored| table.items.get(stored));
            meter.read(table_name, None, read_units(found.map_or(0, item_size), request.consistent_read));
            if let Some(item) = found {
                let item = match projection {
                    Some(projector) => projector.apply(item),
                    None => item.clone(),
                };
                returned += item_size(&item);
                output.responses.entry(table_name.clone()).or_default().push(item);
            }
        }
        output.responses.entry(table_name.clone()).or_default();
    }
    output.consumed_capacity = meter.reports(input.return_consumed_capacity);
    Ok(output)
}

/// `BatchWriteItem`. With `unprocessed` (fault injection), the requests of that table
/// (`Some(Some(table))`) or every request (`Some(None)`) are returned unprocessed, and not written.
pub fn batch_write_item(
    state: &mut State,
    input: BatchWriteItemInput,
    unprocessed: Option<Option<&str>>,
) -> Result<BatchWriteItemOutput> {
    if input.request_items.is_empty() {
        return Err(invalid(
            "1 validation error detected: Value '{}' at 'requestItems' failed to satisfy constraint: Member must have length greater than or equal to 1",
        ));
    }
    let total: usize = input.request_items.values().map(Vec::len).sum();
    if total > 25 {
        return Err(invalid("Too many items requested for the BatchWriteItem call"));
    }
    // Validate everything before writing anything.
    let mut prepared = Vec::with_capacity(total);
    for (table_name, requests) in &input.request_items {
        if requests.is_empty() {
            return Err(invalid(format!(
                "1 validation error detected: Value '[]' at 'requestItems.{table_name}.member' failed to satisfy constraint: Member must have length greater than or equal to 1"
            )));
        }
        let table = state.table(table_name)?;
        let mut seen = BTreeSet::new();
        for request in requests {
            let write = match request {
                WriteRequest::Put(item) => {
                    prepare_put(table, item.clone(), None, ReturnValuesOnConditionCheckFailure::None)
                }
                WriteRequest::Delete(key) => {
                    prepare_delete(table, key, None, ReturnValuesOnConditionCheckFailure::None)
                }
            }
            .map_err(Failure::into_error)?;
            if !seen.insert(write.key.clone()) {
                return Err(invalid("Provided list of item keys contains duplicates"));
            }
            prepared.push(write);
        }
    }
    let mut output = BatchWriteItemOutput::default();
    let skipped = |table: &str| unprocessed.is_some_and(|only| only.is_none_or(|only| only == table));
    for (table_name, requests) in &input.request_items {
        if skipped(table_name) {
            output.unprocessed_items.insert(table_name.clone(), requests.clone());
        }
    }
    let mut meter = CapacityMeter::default();
    for write in prepared.into_iter().filter(|write| !skipped(&write.table)) {
        if input.return_item_collection_metrics
            && let Some(table) = state.tables.get(&write.table)
            && let Some(metrics) = write
                .new
                .as_ref()
                .or_else(|| table.items.get(&write.key))
                .and_then(|item| table.item_collection_metrics(item))
        {
            output.item_collection_metrics.entry(write.table.clone()).or_default().push(metrics);
        }
        apply(state, write, &mut meter, 1.0);
    }
    output.consumed_capacity = meter.reports(input.return_consumed_capacity);
    Ok(output)
}
