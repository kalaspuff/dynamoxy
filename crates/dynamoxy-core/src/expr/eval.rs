//! Evaluates conditions against items, with DynamoDB's semantics.
//!
//! * A comparison involving a missing attribute is false, except `<>`, which is true.
//! * Ordering comparisons (`<`, `<=`, `>`, `>=`, `BETWEEN`) only hold between scalars of the same
//!   type (`S`, `N`, `B`); anything else is false (not an error).
//! * Literal operands of the wrong type are rejected up front by [`validate_condition`], like
//!   DynamoDB does before evaluating anything.

use std::borrow::Cow;
use std::cmp::Ordering;

use super::ast::{Comparator, Condition, Operand, Path, PathElement};
use super::parser::ExpressionKind;
use crate::error::{Error, Result};
use crate::value::{AttributeValue, Item, Number, ValueType};

/// The value at `path` in `item`, if there is one.
pub fn resolve<'a>(item: &'a Item, path: &Path) -> Option<&'a AttributeValue> {
    let mut elements = path.0.iter();
    let Some(PathElement::Name(root)) = elements.next() else { return None };
    let mut current = item.get(root)?;
    for element in elements {
        current = match (element, current) {
            (PathElement::Name(name), AttributeValue::M(map)) => map.get(name)?,
            (PathElement::Index(index), AttributeValue::L(list)) => list.get(*index)?,
            _ => return None,
        };
    }
    Some(current)
}

/// `size()` of a value: characters of a string, bytes of a binary, elements of a collection.
pub fn size_of(value: &AttributeValue) -> Option<usize> {
    match value {
        AttributeValue::S(text) => Some(text.chars().count()),
        AttributeValue::B(bytes) => Some(bytes.len()),
        AttributeValue::Ss(values) => Some(values.len()),
        AttributeValue::Ns(values) => Some(values.len()),
        AttributeValue::Bs(values) => Some(values.len()),
        AttributeValue::L(values) => Some(values.len()),
        AttributeValue::M(values) => Some(values.len()),
        _ => None,
    }
}

fn operand_value<'a>(operand: &'a Operand, item: &'a Item) -> Option<Cow<'a, AttributeValue>> {
    match operand {
        Operand::Path(path) => resolve(item, path).map(Cow::Borrowed),
        Operand::Value(value) => Some(Cow::Borrowed(value)),
        Operand::Size(path) => {
            resolve(item, path).and_then(size_of).map(|size| Cow::Owned(AttributeValue::N(Number::from(size as u64))))
        }
    }
}

fn compare(left: &AttributeValue, comparator: Comparator, right: &AttributeValue) -> bool {
    match comparator {
        Comparator::Eq => left == right,
        Comparator::Ne => left != right,
        _ => match left.cmp_scalar(right) {
            Some(ordering) => match comparator {
                Comparator::Lt => ordering == Ordering::Less,
                Comparator::Le => ordering != Ordering::Greater,
                Comparator::Gt => ordering == Ordering::Greater,
                Comparator::Ge => ordering != Ordering::Less,
                Comparator::Eq | Comparator::Ne => unreachable!("handled above"),
            },
            None => false,
        },
    }
}

fn contains(haystack: &AttributeValue, needle: &AttributeValue) -> bool {
    match (haystack, needle) {
        (AttributeValue::S(text), AttributeValue::S(part)) => text.contains(part.as_str()),
        (AttributeValue::B(bytes), AttributeValue::B(part)) => {
            part.is_empty() || bytes.windows(part.len()).any(|window| window == part.as_slice())
        }
        (AttributeValue::Ss(values), AttributeValue::S(value)) => values.contains(value),
        (AttributeValue::Ns(values), AttributeValue::N(value)) => values.contains(value),
        (AttributeValue::Bs(values), AttributeValue::B(value)) => values.contains(value),
        (AttributeValue::L(values), value) => values.contains(value),
        _ => false,
    }
}

/// Whether `condition` holds for `item` (pass an empty item for an item that does not exist).
pub fn evaluate(condition: &Condition, item: &Item) -> bool {
    match condition {
        Condition::Compare(left, comparator, right) => match (operand_value(left, item), operand_value(right, item)) {
            (Some(left), Some(right)) => compare(&left, *comparator, &right),
            _ => *comparator == Comparator::Ne,
        },
        Condition::Between(value, low, high) => {
            match (operand_value(value, item), operand_value(low, item), operand_value(high, item)) {
                (Some(value), Some(low), Some(high)) => {
                    matches!(value.cmp_scalar(&low), Some(Ordering::Greater | Ordering::Equal))
                        && matches!(value.cmp_scalar(&high), Some(Ordering::Less | Ordering::Equal))
                }
                _ => false,
            }
        }
        Condition::In(value, candidates) => operand_value(value, item).is_some_and(|value| {
            candidates
                .iter()
                .any(|candidate| operand_value(candidate, item).is_some_and(|candidate| *candidate == *value))
        }),
        Condition::AttributeExists(path) => resolve(item, path).is_some(),
        Condition::AttributeNotExists(path) => resolve(item, path).is_none(),
        Condition::AttributeType(path, kind) => match (resolve(item, path), operand_value(kind, item)) {
            (Some(value), Some(kind)) => kind.as_s() == Some(value.value_type().as_str()),
            _ => false,
        },
        Condition::BeginsWith(path, prefix) => match (resolve(item, path), operand_value(prefix, item)) {
            (Some(AttributeValue::S(text)), Some(prefix)) => {
                prefix.as_s().is_some_and(|prefix| text.starts_with(prefix))
            }
            (Some(AttributeValue::B(bytes)), Some(prefix)) => {
                prefix.as_b().is_some_and(|prefix| bytes.starts_with(prefix))
            }
            _ => false,
        },
        Condition::Contains(path, needle) => match (resolve(item, path), operand_value(needle, item)) {
            (Some(haystack), Some(needle)) => contains(haystack, &needle),
            _ => false,
        },
        Condition::And(left, right) => evaluate(left, item) && evaluate(right, item),
        Condition::Or(left, right) => evaluate(left, item) || evaluate(right, item),
        Condition::Not(inner) => !evaluate(inner, item),
    }
}

fn invalid(kind: ExpressionKind, message: impl std::fmt::Display) -> Error {
    Error::validation_exception(format!("Invalid {}: {message}", kind.as_str()))
}

fn operand_type_error(kind: ExpressionKind, operator: &str, value: &AttributeValue) -> Error {
    invalid(
        kind,
        format!(
            "Incorrect operand type for operator or function; operator or function: {operator}, operand type: {}",
            value.value_type().as_str()
        ),
    )
}

fn literal(operand: &Operand) -> Option<&AttributeValue> {
    match operand {
        Operand::Value(value) => Some(value),
        _ => None,
    }
}

/// Rejects conditions DynamoDB rejects before evaluating them: literal operands of the wrong type,
/// inverted `BETWEEN` bounds and unknown `attribute_type` names.
pub fn validate_condition(kind: ExpressionKind, condition: &Condition) -> Result<()> {
    match condition {
        Condition::Compare(left, comparator, right) => {
            if !matches!(comparator, Comparator::Eq | Comparator::Ne) {
                for value in [left, right].into_iter().filter_map(literal) {
                    if !value.is_key_scalar() {
                        return Err(operand_type_error(kind, comparator.as_str(), value));
                    }
                }
            }
            Ok(())
        }
        Condition::Between(_, low, high) => {
            for value in [low, high].into_iter().filter_map(literal) {
                if !value.is_key_scalar() {
                    return Err(operand_type_error(kind, "BETWEEN", value));
                }
            }
            if let (Some(low), Some(high)) = (literal(low), literal(high))
                && low.cmp_scalar(high) == Some(Ordering::Greater)
            {
                return Err(invalid(
                    kind,
                    format!(
                        "The BETWEEN operator requires upper bound to be greater than or equal to lower bound; lower bound operand: AttributeValue: {low:?}, upper bound operand: AttributeValue: {high:?}"
                    ),
                ));
            }
            Ok(())
        }
        Condition::AttributeType(_, type_operand) => {
            if let Some(value) = literal(type_operand) {
                let Some(name) = value.as_s() else {
                    return Err(operand_type_error(kind, "attribute_type", value));
                };
                if ValueType::parse(name).is_none() {
                    return Err(invalid(
                        kind,
                        format!(
                            "Invalid attribute type name found; type: {name}, valid types: {{ B,NULL,SS,BOOL,L,BS,N,NS,S,M }}"
                        ),
                    ));
                }
            }
            Ok(())
        }
        Condition::BeginsWith(_, prefix) => match literal(prefix) {
            Some(value) if !matches!(value, AttributeValue::S(_) | AttributeValue::B(_)) => {
                Err(operand_type_error(kind, "begins_with", value))
            }
            _ => Ok(()),
        },
        Condition::And(left, right) | Condition::Or(left, right) => {
            validate_condition(kind, left)?;
            validate_condition(kind, right)
        }
        Condition::Not(inner) => validate_condition(kind, inner),
        // Captured against DynamoDB by dynoxide-rs (tests/expression_syntax_errors.rs).
        Condition::Contains(path, Operand::Path(other)) if other == path => Err(invalid(
            kind,
            format!(
                "The first operand must be distinct from the remaining operands for this operator or function; operator: contains, first operand: [{}]",
                path.root()
            ),
        )),
        Condition::In(..)
        | Condition::AttributeExists(_)
        | Condition::AttributeNotExists(_)
        | Condition::Contains(..) => Ok(()),
    }
}

/// Every path a condition reads (used to reject conditions on key attributes in a few places).
pub fn condition_paths(condition: &Condition) -> Vec<&Path> {
    fn operand_path(operand: &Operand) -> Option<&Path> {
        match operand {
            Operand::Path(path) | Operand::Size(path) => Some(path),
            Operand::Value(_) => None,
        }
    }
    let mut paths = Vec::new();
    match condition {
        Condition::Compare(left, _, right) => paths.extend([left, right].into_iter().filter_map(operand_path)),
        Condition::Between(value, low, high) => paths.extend([value, low, high].into_iter().filter_map(operand_path)),
        Condition::In(value, candidates) => {
            paths.extend(operand_path(value));
            paths.extend(candidates.iter().filter_map(operand_path));
        }
        Condition::AttributeExists(path) | Condition::AttributeNotExists(path) => paths.push(path),
        Condition::AttributeType(path, operand)
        | Condition::BeginsWith(path, operand)
        | Condition::Contains(path, operand) => {
            paths.push(path);
            paths.extend(operand_path(operand));
        }
        Condition::And(left, right) | Condition::Or(left, right) => {
            paths.extend(condition_paths(left));
            paths.extend(condition_paths(right));
        }
        Condition::Not(inner) => paths.extend(condition_paths(inner)),
    }
    paths
}
