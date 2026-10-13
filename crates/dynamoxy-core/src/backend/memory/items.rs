//! Single-item reads and writes. Writes are split into *prepare* (validate, evaluate the
//! condition, compute the new item) and *apply*, so that transactions can prepare every
//! operation before applying any.

use super::State;
use super::table::{CapacityMeter, StoredKey, Table, read_units};
use crate::error::{Error, Result, ServiceError};
use crate::expr::{
    Condition, ExpressionInput, UpdateExpression, apply_update, evaluate, parse_request, project, validate_update,
};
use crate::types::{
    DeleteItemInput, GetItemInput, GetItemOutput, PutItemInput, ReturnValues, ReturnValuesOnConditionCheckFailure,
    UpdateItemInput, WriteItemOutput,
};
use crate::value::{Item, item_size};

/// Why a write could not be prepared.
#[derive(Debug)]
pub enum Failure {
    /// The condition does not hold; carries the item when `ALL_OLD` was requested.
    Condition(Option<Item>),
    /// The write is invalid for this item (e.g. an update path that does not exist).
    Invalid(Error),
}

impl From<Error> for Failure {
    fn from(error: Error) -> Self {
        Self::Invalid(error)
    }
}

impl Failure {
    /// The error a single-item operation returns for this failure.
    pub fn into_error(self) -> Error {
        match self {
            Self::Condition(item) => ServiceError::conditional_check_failed(item).into(),
            Self::Invalid(error) => error,
        }
    }
}

/// A validated write, ready to apply.
#[derive(Debug)]
pub struct Prepared {
    /// Table name.
    pub table: String,
    /// Key of the item.
    pub key: StoredKey,
    /// The item before the write.
    pub old: Option<Item>,
    /// The item after the write (`None`: deleted or does not exist).
    pub new: Option<Item>,
    /// Whether anything is written (`false` for condition checks).
    pub writes: bool,
    /// Paths written by an update (for `UPDATED_OLD` / `UPDATED_NEW`).
    pub updated_paths: Vec<crate::expr::Path>,
}

fn check_condition(
    condition: Option<&Condition>,
    old: Option<&Item>,
    return_on_failure: ReturnValuesOnConditionCheckFailure,
) -> Result<(), Failure> {
    let Some(condition) = condition else { return Ok(()) };
    let empty = Item::new();
    if evaluate(condition, old.unwrap_or(&empty)) {
        return Ok(());
    }
    let item = match return_on_failure {
        ReturnValuesOnConditionCheckFailure::AllOld => old.cloned(),
        ReturnValuesOnConditionCheckFailure::None => None,
    };
    Err(Failure::Condition(item))
}

/// Prepares a `PutItem` (also used by transactions and batch writes).
pub fn prepare_put(
    table: &Table,
    item: Item,
    condition: Option<&Condition>,
    return_on_failure: ReturnValuesOnConditionCheckFailure,
) -> Result<Prepared, Failure> {
    let key = table.validate_item(&item)?;
    let old = table.items.get(&key).cloned();
    check_condition(condition, old.as_ref(), return_on_failure)?;
    Ok(Prepared { table: table.name.clone(), key, old, new: Some(item), writes: true, updated_paths: Vec::new() })
}

/// Prepares a `DeleteItem`.
pub fn prepare_delete(
    table: &Table,
    key: &Item,
    condition: Option<&Condition>,
    return_on_failure: ReturnValuesOnConditionCheckFailure,
) -> Result<Prepared, Failure> {
    let key = table.key.exact_key(key)?;
    let old = table.items.get(&key).cloned();
    check_condition(condition, old.as_ref(), return_on_failure)?;
    Ok(Prepared { table: table.name.clone(), key, old, new: None, writes: true, updated_paths: Vec::new() })
}

/// Prepares a condition check (transactions).
pub fn prepare_check(
    table: &Table,
    key: &Item,
    condition: Option<&Condition>,
    return_on_failure: ReturnValuesOnConditionCheckFailure,
) -> Result<Prepared, Failure> {
    let key = table.key.exact_key(key)?;
    let old = table.items.get(&key).cloned();
    check_condition(condition, old.as_ref(), return_on_failure)?;
    Ok(Prepared { table: table.name.clone(), key, new: old.clone(), old, writes: false, updated_paths: Vec::new() })
}

/// Validates an update against the table's key (before any item is looked at).
pub fn validate_update_for(table: &Table, update: &UpdateExpression) -> Result<()> {
    validate_update(update, &table.key.names())
}

/// Prepares an `UpdateItem`. A missing item is created from its key (an upsert), unless the
/// condition prevents it.
pub fn prepare_update(
    table: &Table,
    key: &Item,
    update: Option<&UpdateExpression>,
    condition: Option<&Condition>,
    return_on_failure: ReturnValuesOnConditionCheckFailure,
) -> Result<Prepared, Failure> {
    let stored = table.key.exact_key(key)?;
    let old = table.items.get(&stored).cloned();
    check_condition(condition, old.as_ref(), return_on_failure)?;
    let base = old.clone().unwrap_or_else(|| key.clone());
    let (new, updated_paths) = match update {
        Some(update) => (apply_update(update, &base)?, update.paths().cloned().collect()),
        None => (base, Vec::new()),
    };
    table.validate_item(&new)?;
    Ok(Prepared { table: table.name.clone(), key: stored, old, new: Some(new), writes: true, updated_paths })
}

/// Applies a prepared write and records its capacity. Returns the old item.
pub fn apply(state: &mut State, prepared: Prepared, meter: &mut CapacityMeter, multiplier: f64) -> Option<Item> {
    let Some(table) = state.tables.get_mut(&prepared.table) else { return prepared.old };
    meter.write(table, prepared.old.as_ref(), prepared.new.as_ref(), multiplier);
    if prepared.writes {
        table.write(prepared.key, prepared.new);
    }
    prepared.old
}

/// `PutItem` and `DeleteItem` only return the old item.
fn check_return_values(return_values: ReturnValues) -> Result<()> {
    if !matches!(return_values, ReturnValues::None | ReturnValues::AllOld) {
        return Err(Error::validation_exception("ReturnValues can only be ALL_OLD or NONE"));
    }
    Ok(())
}

/// `GetItem`.
pub fn get_item(state: &State, input: &GetItemInput) -> Result<GetItemOutput> {
    let table = state.table(&input.table_name)?;
    let parsed = parse_request(
        ExpressionInput { projection: input.projection_expression.as_deref(), ..Default::default() },
        &input.expression_attribute_names,
        &Default::default(),
    )?;
    let key = table.key.exact_key(&input.key)?;
    let found = table.items.get(&key);
    let mut meter = CapacityMeter::default();
    meter.read(&table.name, None, read_units(found.map_or(0, item_size), input.consistent_read));
    let item = found.map(|item| match &parsed.projection {
        Some(paths) => project(item, paths),
        None => item.clone(),
    });
    Ok(GetItemOutput { item, consumed_capacity: meter.table_report(&table.name, input.return_consumed_capacity) })
}

/// `PutItem`.
pub fn put_item(state: &mut State, input: PutItemInput) -> Result<WriteItemOutput> {
    check_return_values(input.return_values)?;
    let parsed = parse_request(
        ExpressionInput { condition: input.condition_expression.as_deref(), ..Default::default() },
        &input.expression_attribute_names,
        &input.expression_attribute_values,
    )?;
    let table = state.table(&input.table_name)?;
    let mut meter = CapacityMeter::default();
    let prepared =
        prepare_put(table, input.item, parsed.condition.as_ref(), input.return_values_on_condition_check_failure)
            .map_err(Failure::into_error)?;
    let metrics = if input.return_item_collection_metrics {
        prepared.new.as_ref().and_then(|item| table.item_collection_metrics(item))
    } else {
        None
    };
    let table_name = prepared.table.clone();
    let old = apply(state, prepared, &mut meter, 1.0);
    Ok(WriteItemOutput {
        attributes: if input.return_values == ReturnValues::AllOld { old } else { None },
        consumed_capacity: meter.table_report(&table_name, input.return_consumed_capacity),
        item_collection_metrics: metrics,
    })
}

/// `DeleteItem`.
pub fn delete_item(state: &mut State, input: DeleteItemInput) -> Result<WriteItemOutput> {
    check_return_values(input.return_values)?;
    let parsed = parse_request(
        ExpressionInput { condition: input.condition_expression.as_deref(), ..Default::default() },
        &input.expression_attribute_names,
        &input.expression_attribute_values,
    )?;
    let table = state.table(&input.table_name)?;
    let mut meter = CapacityMeter::default();
    let prepared =
        prepare_delete(table, &input.key, parsed.condition.as_ref(), input.return_values_on_condition_check_failure)
            .map_err(Failure::into_error)?;
    let metrics = if input.return_item_collection_metrics {
        prepared.old.as_ref().and_then(|item| table.item_collection_metrics(item))
    } else {
        None
    };
    let table_name = prepared.table.clone();
    let old = apply(state, prepared, &mut meter, 1.0);
    Ok(WriteItemOutput {
        attributes: if input.return_values == ReturnValues::AllOld { old } else { None },
        consumed_capacity: meter.table_report(&table_name, input.return_consumed_capacity),
        item_collection_metrics: metrics,
    })
}

/// `UpdateItem`.
pub fn update_item(state: &mut State, input: UpdateItemInput) -> Result<WriteItemOutput> {
    let parsed = parse_request(
        ExpressionInput {
            condition: input.condition_expression.as_deref(),
            update: input.update_expression.as_deref(),
            ..Default::default()
        },
        &input.expression_attribute_names,
        &input.expression_attribute_values,
    )?;
    let table = state.table(&input.table_name)?;
    if let Some(update) = &parsed.update {
        validate_update_for(table, update)?;
    }
    let mut meter = CapacityMeter::default();
    let prepared = prepare_update(
        table,
        &input.key,
        parsed.update.as_ref(),
        parsed.condition.as_ref(),
        input.return_values_on_condition_check_failure,
    )
    .map_err(Failure::into_error)?;
    let metrics = if input.return_item_collection_metrics {
        prepared.new.as_ref().and_then(|item| table.item_collection_metrics(item))
    } else {
        None
    };
    let new = prepared.new.clone();
    let paths = prepared.updated_paths.clone();
    let table_name = prepared.table.clone();
    let old = apply(state, prepared, &mut meter, 1.0);
    let non_empty = |item: Item| (!item.is_empty()).then_some(item);
    let attributes = match input.return_values {
        ReturnValues::None => None,
        ReturnValues::AllOld => old,
        ReturnValues::AllNew => new,
        ReturnValues::UpdatedOld => old.and_then(|old| non_empty(project(&old, &paths))),
        ReturnValues::UpdatedNew => new.and_then(|new| non_empty(project(&new, &paths))),
    };
    Ok(WriteItemOutput {
        attributes,
        consumed_capacity: meter.table_report(&table_name, input.return_consumed_capacity),
        item_collection_metrics: metrics,
    })
}
