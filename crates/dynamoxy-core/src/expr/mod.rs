//! The DynamoDB expression language: parsing, validation and evaluation.
//!
//! The in-memory backend uses this module to behave like DynamoDB: it parses
//! `ConditionExpression`, `FilterExpression`, `KeyConditionExpression`, `UpdateExpression` and
//! `ProjectionExpression` with the request's placeholders, rejects what DynamoDB rejects (with the
//! same messages where practical) and evaluates the result against items.
//!
//! Expressions are *built* by the Python layer (`python/dynamoxy/expressions.py`); the AWS
//! backend passes them through unchanged.

pub mod ast;
mod eval;
mod parser;
mod projection;
pub mod reserved;
mod update;

use std::collections::BTreeMap;

pub use ast::{
    Comparator, Condition, KeyCondition, Operand, Path, PathElement, SetValue, SortKeyCondition, UpdateExpression,
    UpdateOperand,
};
pub use eval::{condition_paths, evaluate, resolve, size_of, validate_condition};
pub use parser::{ExpressionKind, Placeholders, parse_condition, parse_projection, parse_update};
pub use projection::{project, validate_projection};
pub use update::{apply_update, validate_update};

use crate::error::{Error, Result};
use crate::value::AttributeValue;

/// `ExpressionAttributeNames`: placeholder (`#name`) to attribute name.
pub type Names = BTreeMap<String, String>;
/// `ExpressionAttributeValues`: placeholder (`:value`) to value.
pub type Values = BTreeMap<String, AttributeValue>;

/// The expressions of one request (all optional).
#[derive(Clone, Copy, Debug, Default)]
pub struct ExpressionInput<'a> {
    /// `ConditionExpression`
    pub condition: Option<&'a str>,
    /// `FilterExpression`
    pub filter: Option<&'a str>,
    /// `KeyConditionExpression`
    pub key_condition: Option<&'a str>,
    /// `UpdateExpression`
    pub update: Option<&'a str>,
    /// `ProjectionExpression`
    pub projection: Option<&'a str>,
}

/// The parsed expressions of one request.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ParsedExpressions {
    /// Parsed `ConditionExpression`.
    pub condition: Option<Condition>,
    /// Parsed `FilterExpression`.
    pub filter: Option<Condition>,
    /// Parsed `KeyConditionExpression`.
    pub key_condition: Option<Condition>,
    /// Parsed `UpdateExpression`.
    pub update: Option<UpdateExpression>,
    /// Parsed `ProjectionExpression`.
    pub projection: Option<Vec<Path>>,
}

/// Parses every expression of a request and checks the placeholder maps the way DynamoDB does:
/// keys start with `#`/`:`, values are valid, every placeholder is defined and every one is used.
pub fn parse_request(input: ExpressionInput<'_>, names: &Names, values: &Values) -> Result<ParsedExpressions> {
    let has_expressions = input.condition.is_some()
        || input.filter.is_some()
        || input.key_condition.is_some()
        || input.update.is_some()
        || input.projection.is_some();
    if !names.is_empty() && !has_expressions {
        return Err(Error::validation_exception(
            "ExpressionAttributeNames can only be specified when using expressions",
        ));
    }
    if !values.is_empty()
        && input.condition.is_none()
        && input.filter.is_none()
        && input.key_condition.is_none()
        && input.update.is_none()
    {
        return Err(Error::validation_exception(
            "ExpressionAttributeValues can only be specified when using expressions: ConditionExpression, FilterExpression, KeyConditionExpression and UpdateExpression are null",
        ));
    }
    for key in names.keys() {
        if !key.starts_with('#') || key.len() < 2 {
            return Err(Error::validation_exception(format!(
                "ExpressionAttributeNames contains invalid key: Syntax error; key: \"{key}\""
            )));
        }
    }
    for (key, value) in values {
        if !key.starts_with(':') || key.len() < 2 {
            return Err(Error::validation_exception(format!(
                "ExpressionAttributeValues contains invalid key: Syntax error; key: \"{key}\""
            )));
        }
        if let Err(message) = value.validate() {
            return Err(Error::validation_exception(format!(
                "ExpressionAttributeValues contains invalid value: {message} for key {key}"
            )));
        }
    }
    let mut placeholders = Placeholders::new(names, values);
    let mut parsed = ParsedExpressions::default();
    if let Some(text) = input.key_condition {
        let condition = parse_condition(ExpressionKind::KeyCondition, text, &mut placeholders)?;
        parsed.key_condition = Some(condition);
    }
    if let Some(text) = input.update {
        parsed.update = Some(parse_update(text, &mut placeholders)?);
    }
    if let Some(text) = input.condition {
        let condition = parse_condition(ExpressionKind::Condition, text, &mut placeholders)?;
        validate_condition(ExpressionKind::Condition, &condition)?;
        parsed.condition = Some(condition);
    }
    if let Some(text) = input.filter {
        let condition = parse_condition(ExpressionKind::Filter, text, &mut placeholders)?;
        validate_condition(ExpressionKind::Filter, &condition)?;
        parsed.filter = Some(condition);
    }
    if let Some(text) = input.projection {
        let paths = parse_projection(text, &mut placeholders)?;
        validate_projection(&paths)?;
        parsed.projection = Some(paths);
    }
    let unused_names = placeholders.unused_names();
    if !unused_names.is_empty() {
        return Err(Error::validation_exception(format!(
            "Value provided in ExpressionAttributeNames unused in expressions: keys: {{{}}}",
            unused_names.join(", ")
        )));
    }
    let unused_values = placeholders.unused_values();
    if !unused_values.is_empty() {
        return Err(Error::validation_exception(format!(
            "Value provided in ExpressionAttributeValues unused in expressions: keys: {{{}}}",
            unused_values.join(", ")
        )));
    }
    Ok(parsed)
}

fn key_error(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

fn operator_name(condition: &Condition) -> &'static str {
    match condition {
        Condition::Or(..) => "OR",
        Condition::Not(..) => "NOT",
        Condition::In(..) => "IN",
        Condition::AttributeExists(_) => "attribute_exists",
        Condition::AttributeNotExists(_) => "attribute_not_exists",
        Condition::AttributeType(..) => "attribute_type",
        Condition::Contains(..) => "contains",
        _ => "",
    }
}

fn key_name(operand: &Operand) -> Option<&str> {
    match operand {
        Operand::Path(path) if path.is_top_level() => Some(path.root()),
        _ => None,
    }
}

fn literal(operand: &Operand) -> Option<&AttributeValue> {
    match operand {
        Operand::Value(value) => Some(value),
        _ => None,
    }
}

/// One key condition part: `(attribute name, condition)`.
fn key_part(condition: &Condition) -> Result<(String, SortKeyCondition)> {
    let unsupported = || key_error("Query key condition not supported");
    match condition {
        Condition::Compare(left, comparator, right) => {
            if *comparator == Comparator::Ne {
                return Err(key_error("Unsupported operator on KeyConditionExpression: operator: <>"));
            }
            let (name, value, comparator) = match (key_name(left), literal(right), key_name(right), literal(left)) {
                (Some(name), Some(value), _, _) => (name, value, *comparator),
                (_, _, Some(name), Some(value)) => {
                    let flipped = match comparator {
                        Comparator::Lt => Comparator::Gt,
                        Comparator::Le => Comparator::Ge,
                        Comparator::Gt => Comparator::Lt,
                        Comparator::Ge => Comparator::Le,
                        other => *other,
                    };
                    (name, value, flipped)
                }
                _ => return Err(unsupported()),
            };
            Ok((name.to_owned(), SortKeyCondition::Compare(comparator, value.clone())))
        }
        Condition::Between(value, low, high) => match (key_name(value), literal(low), literal(high)) {
            (Some(name), Some(low), Some(high)) => {
                Ok((name.to_owned(), SortKeyCondition::Between(low.clone(), high.clone())))
            }
            _ => Err(unsupported()),
        },
        Condition::BeginsWith(path, prefix) => match (path.is_top_level(), literal(prefix)) {
            (true, Some(prefix)) => Ok((path.root().to_owned(), SortKeyCondition::BeginsWith(prefix.clone()))),
            _ => Err(unsupported()),
        },
        other => Err(key_error(format!("Invalid operator used in KeyConditionExpression: {}", operator_name(other)))),
    }
}

fn check_key_type(value: &AttributeValue, expected: Option<crate::value::ValueType>) -> Result<()> {
    if let Some(expected) = expected
        && value.value_type() != expected
    {
        return Err(key_error(
            "One or more parameter values were invalid: Condition parameter type does not match schema type",
        ));
    }
    Ok(())
}

/// Checks a parsed `KeyConditionExpression` against the key schema of the table or index being
/// queried and extracts the partition key value and the sort key condition.
///
/// `partition` and `sort` are `(attribute name, key type)`.
pub fn key_condition(
    condition: &Condition,
    partition: (&str, crate::value::ValueType),
    sort: Option<(&str, crate::value::ValueType)>,
) -> Result<KeyCondition> {
    let parts = match condition {
        Condition::And(left, right) => {
            if matches!(**left, Condition::And(..)) || matches!(**right, Condition::And(..)) {
                return Err(key_error("Conditions can be of length 1 or 2 only"));
            }
            vec![key_part(left)?, key_part(right)?]
        }
        single => vec![key_part(single)?],
    };
    if parts.len() == 2 && parts[0].0 == parts[1].0 {
        return Err(key_error("KeyConditionExpressions must only contain one condition per key"));
    }
    let mut partition_value = None;
    let mut sort_condition = None;
    for (name, part) in parts {
        if name == partition.0 {
            match part {
                SortKeyCondition::Compare(Comparator::Eq, value) => {
                    check_key_type(&value, Some(partition.1))?;
                    partition_value = Some(value);
                }
                _ => return Err(key_error("Query key condition not supported")),
            }
        } else if sort.is_some_and(|(sort_name, _)| sort_name == name) {
            let expected = sort.map(|(_, kind)| kind);
            match &part {
                SortKeyCondition::Compare(_, value) | SortKeyCondition::BeginsWith(value) => {
                    check_key_type(value, expected)?
                }
                SortKeyCondition::Between(low, high) => {
                    check_key_type(low, expected)?;
                    check_key_type(high, expected)?;
                    if low.cmp_scalar(high) == Some(std::cmp::Ordering::Greater) {
                        return Err(key_error(format!(
                            "Invalid KeyConditionExpression: The BETWEEN operator requires upper bound to be greater than or equal to lower bound; lower bound operand: AttributeValue: {low:?}, upper bound operand: AttributeValue: {high:?}"
                        )));
                    }
                }
            }
            if let SortKeyCondition::BeginsWith(prefix) = &part
                && matches!(prefix, AttributeValue::N(_))
            {
                return Err(key_error(
                    "Invalid KeyConditionExpression: Incorrect operand type for operator or function; operator or function: begins_with, operand type: N",
                ));
            }
            sort_condition = Some((name, part));
        } else {
            return Err(key_error(format!("Query condition missed key schema element: {}", partition.0)));
        }
    }
    let Some(partition_value) = partition_value else {
        return Err(key_error(format!("Query condition missed key schema element: {}", partition.0)));
    };
    Ok(KeyCondition { partition_name: partition.0.to_owned(), partition_value, sort: sort_condition })
}

/// Whether a sort key value satisfies a sort key condition.
pub fn sort_key_matches(value: &AttributeValue, condition: &SortKeyCondition) -> bool {
    use std::cmp::Ordering;
    match condition {
        SortKeyCondition::Compare(comparator, expected) => match value.cmp_scalar(expected) {
            Some(ordering) => match comparator {
                Comparator::Eq => ordering == Ordering::Equal,
                Comparator::Lt => ordering == Ordering::Less,
                Comparator::Le => ordering != Ordering::Greater,
                Comparator::Gt => ordering == Ordering::Greater,
                Comparator::Ge => ordering != Ordering::Less,
                Comparator::Ne => ordering != Ordering::Equal,
            },
            None => false,
        },
        SortKeyCondition::Between(low, high) => {
            matches!(value.cmp_scalar(low), Some(Ordering::Greater | Ordering::Equal))
                && matches!(value.cmp_scalar(high), Some(Ordering::Less | Ordering::Equal))
        }
        SortKeyCondition::BeginsWith(prefix) => match (value, prefix) {
            (AttributeValue::S(value), AttributeValue::S(prefix)) => value.starts_with(prefix.as_str()),
            (AttributeValue::B(value), AttributeValue::B(prefix)) => value.starts_with(prefix),
            _ => false,
        },
    }
}

#[cfg(test)]
mod tests;
