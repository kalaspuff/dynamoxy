//! Applies a `ProjectionExpression` to an item.
//!
//! Nested structure is kept (`a.b` projects to `{a: {b: ...}}`); selected list elements are
//! returned in index order as a shorter list (`l[3], l[1]` projects to `{l: [l1, l3]}`).
//! Missing paths are left out.

use std::collections::BTreeMap;

use super::ast::{Path, PathElement};
use crate::error::{Error, Result};
use crate::value::{AttributeValue, Item};

#[derive(Debug, Default)]
struct Node {
    whole: bool,
    names: BTreeMap<String, Node>,
    indexes: BTreeMap<usize, Node>,
}

impl Node {
    fn insert(&mut self, elements: &[PathElement]) {
        match elements.split_first() {
            None => self.whole = true,
            Some((PathElement::Name(name), rest)) => self.names.entry(name.clone()).or_default().insert(rest),
            Some((PathElement::Index(index), rest)) => self.indexes.entry(*index).or_default().insert(rest),
        }
    }

    fn project(&self, value: &AttributeValue) -> Option<AttributeValue> {
        if self.whole {
            return Some(value.clone());
        }
        match value {
            AttributeValue::M(map) if !self.names.is_empty() => {
                let projected: Item = self
                    .names
                    .iter()
                    .filter_map(|(name, node)| {
                        map.get(name).and_then(|value| node.project(value)).map(|value| (name.clone(), value))
                    })
                    .collect();
                (!projected.is_empty()).then_some(AttributeValue::M(projected))
            }
            AttributeValue::L(list) if !self.indexes.is_empty() => {
                let projected: Vec<AttributeValue> = self
                    .indexes
                    .iter()
                    .filter_map(|(index, node)| list.get(*index).and_then(|value| node.project(value)))
                    .collect();
                (!projected.is_empty()).then_some(AttributeValue::L(projected))
            }
            _ => None,
        }
    }
}

/// Rejects projections with overlapping paths (`a, a.b`), like DynamoDB.
pub fn validate_projection(paths: &[Path]) -> Result<()> {
    for (index, first) in paths.iter().enumerate() {
        for second in &paths[index + 1..] {
            let relation = if first == second {
                Some("conflict")
            } else if first.is_prefix_of(second) || second.is_prefix_of(first) {
                Some("overlap")
            } else {
                None
            };
            if let Some(relation) = relation {
                return Err(Error::validation_exception(format!(
                    "Invalid ProjectionExpression: Two document paths {relation} with each other; must remove or rewrite one of these paths; path one: {}, path two: {}",
                    first.error_notation(),
                    second.error_notation()
                )));
            }
        }
    }
    Ok(())
}

/// The parts of `item` selected by `paths`.
pub fn project(item: &Item, paths: &[Path]) -> Item {
    Projector::new(paths).apply(item)
}

/// A projection prepared once and applied to many items (the pages of a query).
#[derive(Debug, Default)]
pub struct Projector {
    root: Node,
}

impl Projector {
    /// Prepares the projection of `paths`.
    pub fn new(paths: &[Path]) -> Self {
        let mut root = Node::default();
        for path in paths {
            root.insert(&path.0);
        }
        Self { root }
    }

    /// The parts of `item` the projection selects.
    pub fn apply(&self, item: &Item) -> Item {
        self.root
            .names
            .iter()
            .filter_map(|(name, node)| {
                item.get(name).and_then(|value| node.project(value)).map(|value| (name.clone(), value))
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::item;

    #[test]
    fn projects_nested_maps_and_list_elements() {
        let source = item([
            ("a", AttributeValue::M(item([("b", AttributeValue::n(1)), ("c", AttributeValue::n(2))]))),
            (
                "l",
                AttributeValue::L(vec![
                    AttributeValue::n(0),
                    AttributeValue::n(1),
                    AttributeValue::n(2),
                    AttributeValue::n(3),
                ]),
            ),
            ("x", AttributeValue::s("x")),
        ]);
        let paths = vec![
            Path(vec![PathElement::Name("a".into()), PathElement::Name("b".into())]),
            Path(vec![PathElement::Name("l".into()), PathElement::Index(3)]),
            Path(vec![PathElement::Name("l".into()), PathElement::Index(1)]),
            Path::attribute("missing"),
        ];
        let projected = project(&source, &paths);
        assert_eq!(
            projected,
            item([
                ("a", AttributeValue::M(item([("b", AttributeValue::n(1))]))),
                ("l", AttributeValue::L(vec![AttributeValue::n(1), AttributeValue::n(3)])),
            ])
        );
    }

    #[test]
    fn overlapping_paths_are_rejected() {
        let paths =
            vec![Path::attribute("a"), Path(vec![PathElement::Name("a".into()), PathElement::Name("b".into())])];
        assert!(validate_projection(&paths).unwrap_err().to_string().contains("overlap"));
    }
}
