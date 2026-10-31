//! `TransactGetItems` and `TransactWriteItems`: all-or-nothing, with cancellation reasons per
//! operation, idempotency tokens and transactional (2x) capacity.

use std::collections::BTreeSet;
use std::hash::{DefaultHasher, Hash, Hasher};

use super::items::{
    Failure, Prepared, apply, prepare_check, prepare_delete, prepare_put, prepare_update, validate_update_for,
};
use super::table::{CapacityMeter, read_units};
use super::{State, TokenRecord, now};
use crate::error::{CancellationReason, Error, Result, ServiceError};
use crate::expr::{ExpressionInput, parse_request, project};
use crate::types::{
    TransactGetItemsInput, TransactGetItemsOutput, TransactWriteItem, TransactWriteItemsInput, TransactWriteItemsOutput,
};
use crate::value::{AttributeValue, Item, item_size};

/// Largest total item size of one transaction (4 MB).
const TRANSACTION_SIZE_LIMIT: usize = 4 * 1024 * 1024;

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

fn check_count(count: usize) -> Result<()> {
    if count == 0 || count > 100 {
        return Err(invalid(format!(
            "1 validation error detected: Value at 'transactItems' failed to satisfy constraint: Member must have length less than or equal to 100 and greater than or equal to 1 (got {count})"
        )));
    }
    Ok(())
}

/// `TransactGetItems`.
pub fn transact_get_items(state: &State, input: &TransactGetItemsInput) -> Result<TransactGetItemsOutput> {
    check_count(input.transact_items.len())?;
    let mut seen = BTreeSet::new();
    let mut parsed = Vec::with_capacity(input.transact_items.len());
    for get in &input.transact_items {
        let table = state.table(&get.table_name)?;
        let key = table.key.exact_key(&get.key)?;
        if !seen.insert((get.table_name.clone(), key.clone())) {
            return Err(invalid("Transaction request cannot include multiple operations on one item"));
        }
        let projection = parse_request(
            ExpressionInput { projection: get.projection_expression.as_deref(), ..Default::default() },
            &get.expression_attribute_names,
            &Default::default(),
        )?
        .projection;
        parsed.push((table, key, projection));
    }
    let mut meter = CapacityMeter::default();
    let mut responses = Vec::with_capacity(parsed.len());
    for (table, key, projection) in parsed {
        let found = table.items.get(&key);
        meter.read(&table.name, None, 2.0 * read_units(found.map_or(0, item_size), true));
        responses.push(found.map(|item| match &projection {
            Some(paths) => project(item, paths),
            None => item.clone(),
        }));
    }
    Ok(TransactGetItemsOutput { responses, consumed_capacity: meter.reports(input.return_consumed_capacity) })
}

/// A hash of every parameter of a transaction's operations (an idempotency token may only be
/// reused for the same request).
fn fingerprint(input: &TransactWriteItemsInput) -> u64 {
    let mut hasher = DefaultHasher::new();
    input.transact_items.len().hash(&mut hasher);
    for operation in &input.transact_items {
        let (kind, item, update) = match operation {
            TransactWriteItem::ConditionCheck { key, .. } => (0u8, key, None),
            TransactWriteItem::Put { item, .. } => (1, item, None),
            TransactWriteItem::Delete { key, .. } => (2, key, None),
            TransactWriteItem::Update { key, update_expression, .. } => (3, key, Some(update_expression)),
        };
        let target = operation.target();
        kind.hash(&mut hasher);
        target.table_name.hash(&mut hasher);
        target.condition_expression.hash(&mut hasher);
        target.expression_attribute_names.hash(&mut hasher);
        target.expression_attribute_values.len().hash(&mut hasher);
        for (placeholder, value) in &target.expression_attribute_values {
            placeholder.hash(&mut hasher);
            hash_value(value, &mut hasher);
        }
        target.return_values_on_condition_check_failure.hash(&mut hasher);
        hash_item(item, &mut hasher);
        update.hash(&mut hasher);
    }
    hasher.finish()
}

fn hash_item(item: &Item, hasher: &mut DefaultHasher) {
    item.len().hash(hasher);
    for (name, value) in item {
        name.hash(hasher);
        hash_value(value, hasher);
    }
}

fn hash_value(value: &AttributeValue, hasher: &mut DefaultHasher) {
    std::mem::discriminant(value).hash(hasher);
    match value {
        AttributeValue::S(text) => text.hash(hasher),
        AttributeValue::N(number) => number.hash(hasher),
        AttributeValue::B(bytes) => bytes.hash(hasher),
        AttributeValue::Bool(flag) => flag.hash(hasher),
        AttributeValue::Null => {}
        AttributeValue::L(values) => {
            values.len().hash(hasher);
            for value in values {
                hash_value(value, hasher);
            }
        }
        AttributeValue::M(item) => hash_item(item, hasher),
        AttributeValue::Ss(values) => values.hash(hasher),
        AttributeValue::Ns(values) => values.hash(hasher),
        AttributeValue::Bs(values) => values.hash(hasher),
    }
}

/// `TransactWriteItems`.
pub fn transact_write_items(state: &mut State, input: TransactWriteItemsInput) -> Result<TransactWriteItemsOutput> {
    check_count(input.transact_items.len())?;
    let token = match &input.client_request_token {
        Some(token) if token.is_empty() || token.len() > 36 => {
            return Err(invalid(
                "1 validation error detected: Value at 'clientRequestToken' failed to satisfy constraint: Member must have length less than or equal to 36 and greater than or equal to 1",
            ));
        }
        token => token.clone(),
    };
    let request_fingerprint = fingerprint(&input);
    let current = now();
    if let Some(token) = &token
        && let Some(record) = state.tokens.get(token, current)
    {
        if record.fingerprint != request_fingerprint {
            return Err(ServiceError::new(
                "IdempotentParameterMismatchException",
                "Specified idempotency token has already been used for a different request",
            )
            .into());
        }
        // A replay of a transaction that succeeded: nothing is written again, and DynamoDB bills
        // it as a transactional read of the items the first call wrote (2 units per 4 KB, of the
        // larger of each item's old and new image), in the replay's own capacity mode.
        let mut meter = CapacityMeter::default();
        for (table, size) in &record.sizes {
            meter.read(table, None, 2.0 * read_units(*size, true));
        }
        return Ok(TransactWriteItemsOutput {
            consumed_capacity: meter.reports(input.return_consumed_capacity),
            ..TransactWriteItemsOutput::default()
        });
    }

    // Request-level validation: expressions, keys, duplicate items, size.
    let mut seen = BTreeSet::new();
    let mut parsed = Vec::with_capacity(input.transact_items.len());
    let mut total_size = 0usize;
    for operation in &input.transact_items {
        let target = operation.target();
        if matches!(operation, TransactWriteItem::ConditionCheck { .. }) && target.condition_expression.is_none() {
            return Err(invalid(
                "1 validation error detected: Value null at 'transactItems.member.conditionCheck.conditionExpression' failed to satisfy constraint: Member must not be null",
            ));
        }
        let expressions = parse_request(
            ExpressionInput {
                condition: target.condition_expression.as_deref(),
                update: match operation {
                    TransactWriteItem::Update { update_expression, .. } => Some(update_expression.as_str()),
                    _ => None,
                },
                ..Default::default()
            },
            &target.expression_attribute_names,
            &target.expression_attribute_values,
        )?;
        let table = state.table(&target.table_name)?;
        let key = match operation {
            TransactWriteItem::Put { item, .. } => {
                total_size += item_size(item);
                table.validate_item(item)?
            }
            TransactWriteItem::ConditionCheck { key, .. }
            | TransactWriteItem::Delete { key, .. }
            | TransactWriteItem::Update { key, .. } => table.key.exact_key(key)?,
        };
        if let Some(update) = &expressions.update {
            validate_update_for(table, update)?;
        }
        if !seen.insert((target.table_name.clone(), key)) {
            return Err(invalid("Transaction request cannot include multiple operations on one item"));
        }
        parsed.push(expressions);
    }
    if total_size > TRANSACTION_SIZE_LIMIT {
        return Err(invalid("Transaction size has exceeded the maximum allowed size"));
    }

    // Prepare every operation; any failure cancels the whole transaction.
    let mut prepared: Vec<Prepared> = Vec::with_capacity(parsed.len());
    let mut reasons = Vec::with_capacity(parsed.len());
    let mut canceled = false;
    for (operation, expressions) in input.transact_items.iter().zip(&parsed) {
        let target = operation.target();
        let table = state.table(&target.table_name)?;
        let on_failure = target.return_values_on_condition_check_failure;
        let condition = expressions.condition.as_ref();
        let result = match operation {
            TransactWriteItem::ConditionCheck { key, .. } => prepare_check(table, key, condition, on_failure),
            TransactWriteItem::Put { item, .. } => prepare_put(table, item.clone(), condition, on_failure),
            TransactWriteItem::Delete { key, .. } => prepare_delete(table, key, condition, on_failure),
            TransactWriteItem::Update { key, .. } => {
                prepare_update(table, key, expressions.update.as_ref(), condition, on_failure)
            }
        };
        match result {
            Ok(write) => {
                // An update's item counts toward the transaction's size as it will be stored.
                if matches!(operation, TransactWriteItem::Update { .. }) {
                    total_size += write.new.as_ref().map_or(0, item_size);
                }
                reasons.push(CancellationReason::none());
                prepared.push(write);
            }
            Err(Failure::Condition(item)) => {
                canceled = true;
                reasons.push(CancellationReason {
                    code: "ConditionalCheckFailed".into(),
                    message: Some("The conditional request failed".into()),
                    item,
                });
            }
            Err(Failure::Invalid(error)) => {
                canceled = true;
                let message = match &error {
                    Error::Service(service) => service.message.clone(),
                    other => other.to_string(),
                };
                reasons.push(CancellationReason { code: "ValidationError".into(), message: Some(message), item: None });
            }
        }
    }
    if canceled {
        return Err(ServiceError::transaction_canceled(reasons).into());
    }
    if total_size > TRANSACTION_SIZE_LIMIT {
        return Err(invalid("Transaction size has exceeded the maximum allowed size"));
    }
    let mut meter = CapacityMeter::default();
    let mut output = TransactWriteItemsOutput::default();
    let mut sizes = Vec::with_capacity(prepared.len());
    for write in prepared {
        if input.return_item_collection_metrics
            && write.writes
            && let Some(table) = state.tables.get(&write.table)
            && let Some(metrics) = write
                .new
                .as_ref()
                .or_else(|| table.items.get(&write.key))
                .and_then(|item| table.item_collection_metrics(item))
        {
            output.item_collection_metrics.entry(write.table.clone()).or_default().push(metrics);
        }
        if write.writes {
            let old = state.tables.get(&write.table).and_then(|table| table.items.get(&write.key)).map_or(0, item_size);
            sizes.push((write.table.clone(), old.max(write.new.as_ref().map_or(0, item_size))));
            apply(state, write, &mut meter, 2.0);
        } else {
            sizes.push((write.table.clone(), write.read_size));
            meter.read(&write.table, None, 2.0 * read_units(write.read_size, true));
        }
    }
    if let Some(token) = token {
        state.tokens.insert(token, TokenRecord { fingerprint: request_fingerprint, at: current, sizes });
    }
    output.consumed_capacity = meter.reports(input.return_consumed_capacity);
    Ok(output)
}
