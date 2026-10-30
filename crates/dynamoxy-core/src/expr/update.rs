//! Applies an [`UpdateExpression`] to an item, with DynamoDB's semantics.
//!
//! All right-hand sides are evaluated against the item *before* the update (so
//! `SET a = b, b = a` swaps two attributes), then `SET`, `REMOVE`, `ADD` and `DELETE` are applied.
//! `SET` of list elements goes in order of their index (`SET l[4] = :b, l[3] = :a` past the end
//! appends `a`, then `b`), and `REMOVE` of list elements uses the original indexes.

use super::ast::{Path, PathElement, SetValue, UpdateExpression, UpdateOperand};
use super::eval::resolve;
use crate::error::{Error, Result};
use crate::value::{AttributeValue, Item, Number};

fn invalid(message: impl std::fmt::Display) -> Error {
    Error::validation_exception(format!("Invalid UpdateExpression: {message}"))
}

fn invalid_path() -> Error {
    Error::validation_exception("The document path provided in the update expression is invalid for update")
}

fn missing_attribute() -> Error {
    Error::validation_exception("The provided expression refers to an attribute that does not exist in the item")
}

fn incorrect_type() -> Error {
    Error::validation_exception("An operand in the update expression has an incorrect data type")
}

/// Whether applying `update` twice in a row has the effect of applying it once, so that a request
/// that may have been applied can be sent again.
///
/// Not idempotent: `ADD` of a number, an action whose value reads a path the update writes
/// (`SET n = n + :one`, `SET l = list_append(l, :v)`, `SET a = b, b = a`), and actions on a list
/// element (`REMOVE l[0]` removes the next element too; `SET l[5]` past the end appends). `SET a =
/// if_not_exists(a, :v)` is idempotent: once `a` exists, it stays.
pub fn is_idempotent(update: &UpdateExpression) -> bool {
    let on_element = |path: &Path| matches!(path.0.last(), Some(PathElement::Index(_)));
    if update.add.iter().any(|(_, value)| matches!(value, AttributeValue::N(_)))
        || update.remove.iter().any(on_element)
        || update.set.iter().any(|(path, _)| on_element(path))
    {
        return false;
    }
    let written: Vec<&Path> = update.paths().collect();
    update.set.iter().all(|(target, value)| {
        let mut read = Vec::new();
        match value {
            SetValue::Operand(UpdateOperand::IfNotExists(path, fallback)) if path == target => {
                read_paths(fallback, &mut read);
            }
            SetValue::Operand(operand) => read_paths(operand, &mut read),
            SetValue::Plus(left, right) | SetValue::Minus(left, right) => {
                read_paths(left, &mut read);
                read_paths(right, &mut read);
            }
        }
        read.iter().all(|path| written.iter().all(|other| !overlap(path, other)))
    })
}

/// The paths an operand reads.
fn read_paths<'a>(operand: &'a UpdateOperand, out: &mut Vec<&'a Path>) {
    match operand {
        UpdateOperand::Path(path) => out.push(path),
        UpdateOperand::Value(_) => {}
        UpdateOperand::IfNotExists(path, fallback) => {
            out.push(path);
            read_paths(fallback, out);
        }
        UpdateOperand::ListAppend(first, second) => {
            read_paths(first, out);
            read_paths(second, out);
        }
    }
}

/// Whether one path is the other or leads into it (`a` and `a.b`).
fn overlap(left: &Path, right: &Path) -> bool {
    let shared = left.0.len().min(right.0.len());
    left.0[..shared] == right.0[..shared]
}

/// Checks an update before applying it: key attributes are not touched, no two actions write
/// overlapping paths, `ADD` takes numbers or sets and `DELETE` takes sets.
pub fn validate_update(update: &UpdateExpression, key_names: &[&str]) -> Result<()> {
    let paths: Vec<&Path> = update.paths().collect();
    for path in &paths {
        if key_names.contains(&path.root()) {
            return Err(Error::validation_exception(format!(
                "One or more parameter values were invalid: Cannot update attribute {}. This attribute is part of the key",
                path.root()
            )));
        }
    }
    for (index, first) in paths.iter().enumerate() {
        for second in &paths[index + 1..] {
            if first == second {
                return Err(invalid(format!(
                    "Two document paths conflict with each other; must remove or rewrite one of these paths; path one: {}, path two: {}",
                    first.error_notation(),
                    second.error_notation()
                )));
            }
            if first.is_prefix_of(second) || second.is_prefix_of(first) {
                return Err(invalid(format!(
                    "Two document paths overlap with each other; must remove or rewrite one of these paths; path one: {}, path two: {}",
                    first.error_notation(),
                    second.error_notation()
                )));
            }
        }
    }
    for (_, value) in &update.add {
        if !matches!(value, AttributeValue::N(_)) && !value.is_set() {
            return Err(invalid(format!(
                "Incorrect operand type for operator or function; operator: ADD, operand type: {}",
                value.value_type().error_name()
            )));
        }
    }
    for (_, value) in &update.delete {
        if !value.is_set() {
            return Err(invalid(format!(
                "Incorrect operand type for operator or function; operator: DELETE, operand type: {}",
                value.value_type().error_name()
            )));
        }
    }
    Ok(())
}

fn evaluate_operand(operand: &UpdateOperand, item: &Item) -> Result<AttributeValue> {
    match operand {
        UpdateOperand::Path(path) => resolve(item, path).cloned().ok_or_else(missing_attribute),
        UpdateOperand::Value(value) => Ok(value.clone()),
        UpdateOperand::IfNotExists(path, fallback) => match resolve(item, path) {
            Some(value) => Ok(value.clone()),
            None => evaluate_operand(fallback, item),
        },
        UpdateOperand::ListAppend(first, second) => {
            let first = evaluate_operand(first, item)?;
            let second = evaluate_operand(second, item)?;
            match (first, second) {
                (AttributeValue::L(mut first), AttributeValue::L(second)) => {
                    first.extend(second);
                    Ok(AttributeValue::L(first))
                }
                (AttributeValue::L(_), other) | (other, _) => Err(invalid(format!(
                    "Incorrect operand type for operator or function; operator or function: list_append, operand type: {}",
                    other.value_type().as_str()
                ))),
            }
        }
    }
}

fn evaluate_set_value(value: &SetValue, item: &Item) -> Result<AttributeValue> {
    let arithmetic = |left: &UpdateOperand, right: &UpdateOperand, operator: &str| -> Result<(Number, Number)> {
        let left = evaluate_operand(left, item)?;
        let right = evaluate_operand(right, item)?;
        match (left, right) {
            (AttributeValue::N(left), AttributeValue::N(right)) => Ok((left, right)),
            (AttributeValue::N(_), other) | (other, _) => Err(invalid(format!(
                "Incorrect operand type for operator or function; operator: {operator}, operand type: {}",
                other.value_type().error_name()
            ))),
        }
    };
    let number_error = |error: crate::value::NumberError| Error::validation_exception(error.to_string());
    match value {
        SetValue::Operand(operand) => evaluate_operand(operand, item),
        SetValue::Plus(left, right) => {
            let (left, right) = arithmetic(left, right, "+")?;
            Ok(AttributeValue::N(left.checked_add(&right).map_err(number_error)?))
        }
        SetValue::Minus(left, right) => {
            let (left, right) = arithmetic(left, right, "-")?;
            Ok(AttributeValue::N(left.checked_sub(&right).map_err(number_error)?))
        }
    }
}

/// The container that holds the last element of `path`, creating nothing.
fn parent_mut<'a>(item: &'a mut Item, path: &Path) -> Option<&'a mut AttributeValue> {
    let elements = &path.0[..path.0.len() - 1];
    let Some((PathElement::Name(root), rest)) = elements.split_first() else { return None };
    let mut current = item.get_mut(root)?;
    for element in rest {
        current = match (element, current) {
            (PathElement::Name(name), AttributeValue::M(map)) => map.get_mut(name)?,
            (PathElement::Index(index), AttributeValue::L(list)) => list.get_mut(*index)?,
            _ => return None,
        };
    }
    Some(current)
}

fn assign(item: &mut Item, path: &Path, value: AttributeValue) -> Result<()> {
    if path.is_top_level() {
        item.insert(path.root().to_owned(), value);
        return Ok(());
    }
    let last = path.0.last().cloned();
    let parent = parent_mut(item, path).ok_or_else(invalid_path)?;
    match (last, parent) {
        (Some(PathElement::Name(name)), AttributeValue::M(map)) => {
            map.insert(name, value);
            Ok(())
        }
        (Some(PathElement::Index(index)), AttributeValue::L(list)) => {
            if index < list.len() {
                list[index] = value;
            } else {
                list.push(value);
            }
            Ok(())
        }
        _ => Err(invalid_path()),
    }
}

/// Removes the value at `path`; missing values are ignored.
fn remove(item: &mut Item, path: &Path) -> Result<()> {
    if path.is_top_level() {
        item.remove(path.root());
        return Ok(());
    }
    let last = path.0.last().cloned();
    let Some(parent) = parent_mut(item, path) else { return Ok(()) };
    match (last, parent) {
        (Some(PathElement::Name(name)), AttributeValue::M(map)) => {
            map.remove(&name);
            Ok(())
        }
        (Some(PathElement::Index(index)), AttributeValue::L(list)) => {
            if index < list.len() {
                list.remove(index);
            }
            Ok(())
        }
        _ => Err(invalid_path()),
    }
}

fn set_union<T: PartialEq + Clone>(existing: &mut Vec<T>, added: &[T]) {
    for value in added {
        if !existing.contains(value) {
            existing.push(value.clone());
        }
    }
}

fn add(item: &mut Item, path: &Path, value: &AttributeValue) -> Result<()> {
    let number_error = |error: crate::value::NumberError| Error::validation_exception(error.to_string());
    let existing = resolve(item, path).cloned();
    let updated = match (existing, value) {
        (None, value) => value.clone(),
        (Some(AttributeValue::N(current)), AttributeValue::N(delta)) => {
            AttributeValue::N(current.checked_add(delta).map_err(number_error)?)
        }
        (Some(AttributeValue::Ss(mut current)), AttributeValue::Ss(added)) => {
            set_union(&mut current, added);
            AttributeValue::Ss(current)
        }
        (Some(AttributeValue::Ns(mut current)), AttributeValue::Ns(added)) => {
            set_union(&mut current, added);
            AttributeValue::Ns(current)
        }
        (Some(AttributeValue::Bs(mut current)), AttributeValue::Bs(added)) => {
            set_union(&mut current, added);
            AttributeValue::Bs(current)
        }
        _ => return Err(incorrect_type()),
    };
    assign(item, path, updated)
}

fn delete(item: &mut Item, path: &Path, value: &AttributeValue) -> Result<()> {
    let Some(existing) = resolve(item, path).cloned() else { return Ok(()) };
    let remaining = match (existing, value) {
        (AttributeValue::Ss(mut current), AttributeValue::Ss(removed)) => {
            current.retain(|value| !removed.contains(value));
            (!current.is_empty()).then_some(AttributeValue::Ss(current))
        }
        (AttributeValue::Ns(mut current), AttributeValue::Ns(removed)) => {
            current.retain(|value| !removed.contains(value));
            (!current.is_empty()).then_some(AttributeValue::Ns(current))
        }
        (AttributeValue::Bs(mut current), AttributeValue::Bs(removed)) => {
            current.retain(|value| !removed.contains(value));
            (!current.is_empty()).then_some(AttributeValue::Bs(current))
        }
        _ => return Err(incorrect_type()),
    };
    match remaining {
        Some(value) => assign(item, path, value),
        None => remove(item, path),
    }
}

/// Applies `update` to `item` (the item before the update; empty if it does not exist).
///
/// Call [`validate_update`] first. Returns the item after the update.
pub fn apply_update(update: &UpdateExpression, item: &Item) -> Result<Item> {
    // Evaluate every SET value against the original item first.
    let values = update.set.iter().map(|(_, value)| evaluate_set_value(value, item)).collect::<Result<Vec<_>>>()?;
    // Assign in path order, which sets the elements of each list in order of their index: those
    // past the end append in that order, whatever the order of the actions.
    let mut assignments: Vec<(&Path, AttributeValue)> = update.set.iter().map(|(path, _)| path).zip(values).collect();
    assignments.sort_by(|left, right| left.0.cmp(right.0));
    let mut result = item.clone();
    for (path, value) in assignments {
        assign(&mut result, path, value)?;
    }
    // REMOVE list elements by their original indexes: paths in descending order remove the
    // highest index of each list first (paths never overlap, see `validate_update`).
    let mut removals: Vec<&Path> = update.remove.iter().collect();
    removals.sort_by(|left, right| right.cmp(left));
    for path in removals {
        remove(&mut result, path)?;
    }
    for (path, value) in &update.add {
        add(&mut result, path, value)?;
    }
    for (path, value) in &update.delete {
        delete(&mut result, path, value)?;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::expr::{Placeholders, parse_update};
    use crate::value::item;

    #[test]
    fn idempotent_updates_can_be_sent_twice() {
        let names = BTreeMap::from([("#n".to_owned(), "name".to_owned())]);
        let values = BTreeMap::from([
            (":one".to_owned(), AttributeValue::n(1)),
            (":tags".to_owned(), AttributeValue::Ss(vec!["a".into()])),
            (":l".to_owned(), AttributeValue::L(vec![AttributeValue::n(1)])),
        ]);
        let idempotent = |expression: &str| {
            let mut placeholders = Placeholders::new(&names, &values);
            is_idempotent(&parse_update(expression, &mut placeholders).unwrap())
        };
        for expression in [
            "SET #n = :one, z1 = :one REMOVE z2",
            "SET a = b",
            "SET a = if_not_exists(a, :one)",
            "SET a = b + :one",
            "SET m.x[0].y = :one",
            "ADD tags :tags DELETE z3 :tags",
        ] {
            assert!(idempotent(expression), "{expression}");
        }
        for expression in [
            "ADD n :one",
            "SET n = n + :one",
            "SET n = :one - n",
            "SET l = list_append(l, :l)",
            "SET l = list_append(:l, if_not_exists(l, :l))",
            "SET a = if_not_exists(a, :one) + :one",
            "SET a = b, b = a",
            "SET a = m.x REMOVE m",
            "REMOVE l[0]",
            "SET l[5] = :one",
        ] {
            assert!(!idempotent(expression), "{expression}");
        }
    }

    fn run(expression: &str, values: &[(&str, AttributeValue)], before: Item) -> Result<Item> {
        let names = BTreeMap::from([("#n".to_owned(), "name".to_owned())]);
        let values: BTreeMap<String, AttributeValue> =
            values.iter().map(|(key, value)| ((*key).to_owned(), value.clone())).collect();
        let mut placeholders = Placeholders::new(&names, &values);
        let update = parse_update(expression, &mut placeholders)?;
        validate_update(&update, &["pk"])?;
        apply_update(&update, &before)
    }

    #[test]
    fn set_swap_uses_old_values() {
        let before = item([("a", AttributeValue::n(1)), ("b", AttributeValue::n(2))]);
        let after = run("SET a = b, b = a", &[], before).unwrap();
        assert_eq!(after, item([("a", AttributeValue::n(2)), ("b", AttributeValue::n(1))]));
    }

    #[test]
    fn arithmetic_list_append_and_if_not_exists() {
        let before = item([("n", AttributeValue::n(5)), ("l", AttributeValue::L(vec![AttributeValue::n(1)]))]);
        let after = run(
            "SET n = n + :two, l = list_append(l, :more), c = if_not_exists(c, :zero) - :two",
            &[
                (":two", AttributeValue::n(2)),
                (":more", AttributeValue::L(vec![AttributeValue::n(2)])),
                (":zero", AttributeValue::n(0)),
            ],
            before,
        )
        .unwrap();
        assert_eq!(after["n"], AttributeValue::n(7));
        assert_eq!(after["l"], AttributeValue::L(vec![AttributeValue::n(1), AttributeValue::n(2)]));
        assert_eq!(after["c"], AttributeValue::n(-2));
    }

    #[test]
    fn nested_paths_need_existing_parents() {
        let error = run("SET m.x = :v", &[(":v", AttributeValue::n(1))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("document path provided in the update expression is invalid"));
        let before = item([("m", AttributeValue::M(Item::new()))]);
        let after = run("SET m.x = :v", &[(":v", AttributeValue::n(1))], before).unwrap();
        assert_eq!(after["m"], AttributeValue::M(item([("x", AttributeValue::n(1))])));
    }

    #[test]
    fn list_index_past_the_end_appends_and_remove_uses_original_indexes() {
        let list = |values: &[i64]| AttributeValue::L(values.iter().map(|value| AttributeValue::n(*value)).collect());
        let before = item([("l", list(&[0, 1, 2, 3]))]);
        let after = run("REMOVE l[1], l[2]", &[], before.clone()).unwrap();
        assert_eq!(after["l"], list(&[0, 3]));
        let after = run("SET l[10] = :v", &[(":v", AttributeValue::n(9))], before).unwrap();
        assert_eq!(after["l"], list(&[0, 1, 2, 3, 9]));
    }

    #[test]
    fn list_elements_are_set_in_index_order() {
        // "If you add multiple elements in a single SET operation, the elements are sorted in order
        // by element number" (the update expressions guide), whatever their order in the expression.
        let list = |values: &[&str]| AttributeValue::L(values.iter().copied().map(AttributeValue::s).collect());
        let values = [(":a", AttributeValue::s("a")), (":b", AttributeValue::s("b")), (":c", AttributeValue::s("c"))];
        let before = item([
            ("l", list(&["x", "y", "z"])),
            ("m", AttributeValue::M(item([("l", list(&["x"]))]))),
            ("n", AttributeValue::L(vec![list(&["x"])])),
        ]);
        for (expression, expected) in [
            ("SET l[4] = :b, l[3] = :a", &["x", "y", "z", "a", "b"][..]),
            ("SET l[9] = :b, l[5] = :a", &["x", "y", "z", "a", "b"]),
            ("SET l[5] = :c, l[1] = :a, l[3] = :b", &["x", "a", "z", "b", "c"]),
        ] {
            let after = run(expression, &values, before.clone()).unwrap();
            assert_eq!(after["l"], list(expected), "{expression}");
        }
        // Lists in maps and in lists too.
        let after = run("SET m.l[7] = :b, n[0][7] = :b, m.l[5] = :a, n[0][5] = :a", &values, before.clone()).unwrap();
        assert_eq!(after["m"], AttributeValue::M(item([("l", list(&["x", "a", "b"]))])));
        assert_eq!(after["n"], AttributeValue::L(vec![list(&["x", "a", "b"])]));
        // A path through an element past the end is invalid, even when the update appends one there.
        let values = [(":a", AttributeValue::s("a")), (":m", AttributeValue::M(Item::new()))];
        for expression in ["SET l[4] = :m, l[3].k = :a", "SET l[3].k = :a, l[4] = :m"] {
            let error = run(expression, &values, before.clone()).expect_err(expression);
            assert!(error.to_string().contains("document path provided in the update expression is invalid"));
        }
    }

    #[test]
    fn add_and_delete_on_numbers_and_sets() {
        let before = item([("tags", AttributeValue::Ss(vec!["a".into(), "b".into()]))]);
        let after = run(
            "ADD visits :one, tags :c DELETE tags :ab",
            &[
                (":one", AttributeValue::n(1)),
                (":c", AttributeValue::Ss(vec!["c".into()])),
                (":ab", AttributeValue::Ss(vec!["a".into(), "b".into()])),
            ],
            before,
        );
        // ADD and DELETE on the same path overlap.
        assert!(after.unwrap_err().to_string().contains("Two document paths conflict"));
        let before = item([("tags", AttributeValue::Ss(vec!["a".into()]))]);
        let after = run("DELETE tags :a", &[(":a", AttributeValue::Ss(vec!["a".into()]))], before).unwrap();
        assert!(!after.contains_key("tags"), "deleting every element removes the attribute");
        let after = run("ADD visits :one", &[(":one", AttributeValue::n(1))], Item::new()).unwrap();
        assert_eq!(after["visits"], AttributeValue::n(1));
    }

    #[test]
    fn invalid_updates_are_rejected() {
        let error = run("SET pk = :v", &[(":v", AttributeValue::n(1))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("Cannot update attribute pk"));
        let error = run("SET a = :v, a.b = :v", &[(":v", AttributeValue::n(1))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("overlap"));
        let error = run("ADD a :v", &[(":v", AttributeValue::s("x"))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("operator: ADD, operand type: STRING"));
        let error = run("SET a = b + :v", &[(":v", AttributeValue::n(1))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("does not exist in the item"));
        let error = run("SET #n = :v SET b = :v", &[(":v", AttributeValue::n(1))], Item::new()).unwrap_err();
        assert!(error.to_string().contains("\"SET\" section can only be used once"));
    }
}
