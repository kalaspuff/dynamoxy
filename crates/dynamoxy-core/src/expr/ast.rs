//! Parsed expressions, with placeholders already resolved to names and values.

use std::fmt;

use crate::value::AttributeValue;

/// One step of a document path.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum PathElement {
    /// A map key (or a top-level attribute name).
    Name(String),
    /// A list index.
    Index(usize),
}

/// A document path such as `a.b[2].c`. The first element is always a name.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Path(pub Vec<PathElement>);

impl Path {
    /// A top-level attribute.
    pub fn attribute(name: impl Into<String>) -> Self {
        Self(vec![PathElement::Name(name.into())])
    }

    /// The top-level attribute name.
    pub fn root(&self) -> &str {
        match self.0.first() {
            Some(PathElement::Name(name)) => name,
            _ => "",
        }
    }

    /// Whether the path is a single top-level attribute.
    pub fn is_top_level(&self) -> bool {
        self.0.len() == 1
    }

    /// Whether `self` is a prefix of `other` (or equal to it).
    pub fn is_prefix_of(&self, other: &Path) -> bool {
        self.0.len() <= other.0.len() && self.0.iter().zip(&other.0).all(|(left, right)| left == right)
    }

    /// The path in DynamoDB's error-message notation: `[a, b, [2]]`.
    pub fn error_notation(&self) -> String {
        let parts: Vec<String> = self
            .0
            .iter()
            .map(|element| match element {
                PathElement::Name(name) => name.clone(),
                PathElement::Index(index) => format!("[{index}]"),
            })
            .collect();
        format!("[{}]", parts.join(", "))
    }
}

impl fmt::Display for Path {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (position, element) in self.0.iter().enumerate() {
            match element {
                PathElement::Name(name) if position == 0 => f.write_str(name)?,
                PathElement::Name(name) => write!(f, ".{name}")?,
                PathElement::Index(index) => write!(f, "[{index}]")?,
            }
        }
        Ok(())
    }
}

/// A value in a condition: a path, a literal value or `size(path)`.
#[derive(Clone, Debug, PartialEq)]
pub enum Operand {
    /// The value at a document path (missing when the item has none).
    Path(Path),
    /// An expression attribute value.
    Value(AttributeValue),
    /// `size(path)`: length of a string or binary, number of elements of a set, list or map.
    Size(Path),
}

/// Comparison operators.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Comparator {
    /// `=`
    Eq,
    /// `<>`
    Ne,
    /// `<`
    Lt,
    /// `<=`
    Le,
    /// `>`
    Gt,
    /// `>=`
    Ge,
}

impl Comparator {
    /// The operator as written in expressions.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Eq => "=",
            Self::Ne => "<>",
            Self::Lt => "<",
            Self::Le => "<=",
            Self::Gt => ">",
            Self::Ge => ">=",
        }
    }
}

/// A condition (`ConditionExpression`, `FilterExpression`, `KeyConditionExpression`).
#[derive(Clone, Debug, PartialEq)]
pub enum Condition {
    /// `a <op> b`
    Compare(Operand, Comparator, Operand),
    /// `a BETWEEN low AND high`
    Between(Operand, Operand, Operand),
    /// `a IN (b, c, ...)`
    In(Operand, Vec<Operand>),
    /// `attribute_exists(path)`
    AttributeExists(Path),
    /// `attribute_not_exists(path)`
    AttributeNotExists(Path),
    /// `attribute_type(path, :type)`
    AttributeType(Path, Operand),
    /// `begins_with(path, :prefix)`
    BeginsWith(Path, Operand),
    /// `contains(path, operand)`
    Contains(Path, Operand),
    /// `a AND b`
    And(Box<Condition>, Box<Condition>),
    /// `a OR b`
    Or(Box<Condition>, Box<Condition>),
    /// `NOT a`
    Not(Box<Condition>),
}

/// An operand on the right-hand side of `SET path = ...`.
#[derive(Clone, Debug, PartialEq)]
pub enum UpdateOperand {
    /// The value at a path (it must exist).
    Path(Path),
    /// An expression attribute value.
    Value(AttributeValue),
    /// `if_not_exists(path, fallback)`
    IfNotExists(Path, Box<UpdateOperand>),
    /// `list_append(a, b)`
    ListAppend(Box<UpdateOperand>, Box<UpdateOperand>),
}

/// The value assigned by a `SET` action.
#[derive(Clone, Debug, PartialEq)]
pub enum SetValue {
    /// `path = operand`
    Operand(UpdateOperand),
    /// `path = a + b`
    Plus(UpdateOperand, UpdateOperand),
    /// `path = a - b`
    Minus(UpdateOperand, UpdateOperand),
}

/// A parsed `UpdateExpression`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct UpdateExpression {
    /// `SET path = value` actions.
    pub set: Vec<(Path, SetValue)>,
    /// `REMOVE path` actions.
    pub remove: Vec<Path>,
    /// `ADD path :value` actions (numbers and sets).
    pub add: Vec<(Path, AttributeValue)>,
    /// `DELETE path :value` actions (sets).
    pub delete: Vec<(Path, AttributeValue)>,
}

impl UpdateExpression {
    /// Every path an action writes, in section order.
    pub fn paths(&self) -> impl Iterator<Item = &Path> {
        self.set
            .iter()
            .map(|(path, _)| path)
            .chain(&self.remove)
            .chain(self.add.iter().map(|(path, _)| path))
            .chain(self.delete.iter().map(|(path, _)| path))
    }
}

/// The sort key part of a `KeyConditionExpression`.
#[derive(Clone, Debug, PartialEq)]
pub enum SortKeyCondition {
    /// `sk <op> :v` (`=`, `<`, `<=`, `>`, `>=`)
    Compare(Comparator, AttributeValue),
    /// `sk BETWEEN :low AND :high`
    Between(AttributeValue, AttributeValue),
    /// `begins_with(sk, :prefix)`
    BeginsWith(AttributeValue),
}

/// A parsed `KeyConditionExpression`: equality on the partition key, optionally a sort key condition.
#[derive(Clone, Debug, PartialEq)]
pub struct KeyCondition {
    /// Partition key attribute name.
    pub partition_name: String,
    /// Partition key value.
    pub partition_value: AttributeValue,
    /// Sort key attribute name and condition.
    pub sort: Option<(String, SortKeyCondition)>,
}
