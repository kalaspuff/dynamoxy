//! DynamoDB attribute values: [`AttributeValue`], [`Item`] and [`Number`].
//!
//! These are dynamoxy's own types; SDK types never leave `backend::aws`. Set values compare as
//! sets (element order does not matter), like in DynamoDB.

pub mod number;
mod size;

use std::cmp::Ordering;
use std::collections::{BTreeMap, HashSet};
use std::fmt;
use std::hash::Hash;

pub use number::{Number, NumberError};
pub use size::{BATCH_GET_RESPONSE_LIMIT, MAX_ITEM_SIZE, item_size, value_size};

/// An item (or a key): attribute names to values. Ordered, so output is deterministic.
pub type Item = BTreeMap<String, AttributeValue>;

/// One DynamoDB attribute value.
#[derive(Clone)]
pub enum AttributeValue {
    /// String.
    S(String),
    /// Number.
    N(Number),
    /// Binary.
    B(Vec<u8>),
    /// Boolean.
    Bool(bool),
    /// Null.
    Null,
    /// List of values.
    L(Vec<AttributeValue>),
    /// Map of attribute names to values.
    M(Item),
    /// String set (non-empty, no duplicates).
    Ss(Vec<String>),
    /// Number set (non-empty, no duplicates).
    Ns(Vec<Number>),
    /// Binary set (non-empty, no duplicates).
    Bs(Vec<Vec<u8>>),
}

/// The type of an [`AttributeValue`], with DynamoDB's type descriptors (`S`, `N`, `SS`, ...).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ValueType {
    /// String.
    S,
    /// Number.
    N,
    /// Binary.
    B,
    /// Boolean.
    Bool,
    /// Null.
    Null,
    /// List.
    L,
    /// Map.
    M,
    /// String set.
    Ss,
    /// Number set.
    Ns,
    /// Binary set.
    Bs,
}

impl ValueType {
    /// Every type, in DynamoDB's documentation order.
    pub const ALL: [ValueType; 10] = [
        ValueType::S,
        ValueType::Ss,
        ValueType::N,
        ValueType::Ns,
        ValueType::B,
        ValueType::Bs,
        ValueType::Bool,
        ValueType::Null,
        ValueType::L,
        ValueType::M,
    ];

    /// DynamoDB's type descriptor (`"S"`, `"NS"`, `"BOOL"`, ...).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::S => "S",
            Self::N => "N",
            Self::B => "B",
            Self::Bool => "BOOL",
            Self::Null => "NULL",
            Self::L => "L",
            Self::M => "M",
            Self::Ss => "SS",
            Self::Ns => "NS",
            Self::Bs => "BS",
        }
    }

    /// Parses a type descriptor (exact, upper case).
    pub fn parse(descriptor: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == descriptor)
    }

    /// The type name DynamoDB uses in error messages (`STRING`, `NUMBER`, ...).
    pub fn error_name(self) -> &'static str {
        match self {
            Self::S => "STRING",
            Self::N => "NUMBER",
            Self::B => "BINARY",
            Self::Bool => "BOOLEAN",
            Self::Null => "NULL",
            Self::L => "LIST",
            Self::M => "MAP",
            Self::Ss => "STRING_SET",
            Self::Ns => "NUMBER_SET",
            Self::Bs => "BINARY_SET",
        }
    }
}

impl fmt::Display for ValueType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl AttributeValue {
    /// A string value.
    pub fn s(value: impl Into<String>) -> Self {
        Self::S(value.into())
    }

    /// A number value from an integer.
    pub fn n(value: i64) -> Self {
        Self::N(Number::from(value))
    }

    /// A number value parsed from text (panics on invalid input; for tests and constants).
    pub fn n_str(value: &str) -> Self {
        Self::N(Number::parse(value).unwrap_or_else(|err| panic!("invalid number {value:?}: {err}")))
    }

    /// A binary value.
    pub fn b(value: impl Into<Vec<u8>>) -> Self {
        Self::B(value.into())
    }

    /// The value's type.
    pub fn value_type(&self) -> ValueType {
        match self {
            Self::S(_) => ValueType::S,
            Self::N(_) => ValueType::N,
            Self::B(_) => ValueType::B,
            Self::Bool(_) => ValueType::Bool,
            Self::Null => ValueType::Null,
            Self::L(_) => ValueType::L,
            Self::M(_) => ValueType::M,
            Self::Ss(_) => ValueType::Ss,
            Self::Ns(_) => ValueType::Ns,
            Self::Bs(_) => ValueType::Bs,
        }
    }

    /// The string, if this is an `S` value.
    pub fn as_s(&self) -> Option<&str> {
        match self {
            Self::S(value) => Some(value),
            _ => None,
        }
    }

    /// The number, if this is an `N` value.
    pub fn as_n(&self) -> Option<&Number> {
        match self {
            Self::N(value) => Some(value),
            _ => None,
        }
    }

    /// The bytes, if this is a `B` value.
    pub fn as_b(&self) -> Option<&[u8]> {
        match self {
            Self::B(value) => Some(value),
            _ => None,
        }
    }

    /// The map, if this is an `M` value.
    pub fn as_m(&self) -> Option<&Item> {
        match self {
            Self::M(value) => Some(value),
            _ => None,
        }
    }

    /// The list, if this is an `L` value.
    pub fn as_l(&self) -> Option<&[AttributeValue]> {
        match self {
            Self::L(value) => Some(value),
            _ => None,
        }
    }

    /// Whether this is a set type (`SS`, `NS`, `BS`).
    pub fn is_set(&self) -> bool {
        matches!(self, Self::Ss(_) | Self::Ns(_) | Self::Bs(_))
    }

    /// Whether this is a scalar that can be a key attribute (`S`, `N`, `B`).
    pub fn is_key_scalar(&self) -> bool {
        matches!(self, Self::S(_) | Self::N(_) | Self::B(_))
    }

    /// Orders two values of the same scalar type the way DynamoDB orders sort keys: strings and
    /// binaries by their (UTF-8) bytes, numbers numerically. `None` for other combinations.
    pub fn cmp_scalar(&self, other: &Self) -> Option<Ordering> {
        match (self, other) {
            (Self::S(left), Self::S(right)) => Some(left.as_bytes().cmp(right.as_bytes())),
            (Self::N(left), Self::N(right)) => Some(left.cmp(right)),
            (Self::B(left), Self::B(right)) => Some(left.cmp(right)),
            _ => None,
        }
    }

    /// Checks the rules DynamoDB applies to every stored value: sets are non-empty and have no
    /// duplicates, documents are nested at most 32 levels deep.
    pub fn validate(&self) -> Result<(), String> {
        self.validate_at_depth(1)
    }

    fn validate_at_depth(&self, depth: usize) -> Result<(), String> {
        if depth > 32 {
            return Err("Nesting Levels have exceeded supported limits: Attributes in the item have nested levels beyond supported limit".into());
        }
        match self {
            Self::Ss(values) => check_set(values, "An string set  may not be empty"),
            Self::Ns(values) => check_set(values, "An number set  may not be empty"),
            Self::Bs(values) => check_set(values, "Binary sets should not be empty"),
            Self::L(values) => values.iter().try_for_each(|value| value.validate_at_depth(depth + 1)),
            Self::M(values) => values.iter().try_for_each(|(name, value)| {
                if name.is_empty() {
                    return Err(crate::expr::EMPTY_NAME.into());
                }
                if name.len() > crate::expr::MAX_NAME_SIZE {
                    return Err(crate::expr::NAME_TOO_LARGE.into());
                }
                value.validate_at_depth(depth + 1)
            }),
            _ => Ok(()),
        }
    }
}

/// DynamoDB's messages for empty sets are verbatim (including "An string set  may not be empty").
/// Sets may hold tens of thousands of elements (an item holds up to 400 KB): duplicates are found
/// with a hash set, in linear time.
fn check_set<T: Eq + Hash>(values: &[T], empty_message: &str) -> Result<(), String> {
    if values.is_empty() {
        return Err(format!("One or more parameter values were invalid: {empty_message}"));
    }
    let mut seen = HashSet::with_capacity(values.len());
    if !values.iter().all(|value| seen.insert(value)) {
        return Err("Input collection contains duplicates".into());
    }
    Ok(())
}

/// Whether two sets (without duplicates) hold the same elements, in any order.
fn same_set<T: Eq + Hash>(left: &[T], right: &[T]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let right: HashSet<&T> = right.iter().collect();
    left.iter().all(|value| right.contains(value))
}

impl PartialEq for AttributeValue {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::S(left), Self::S(right)) => left == right,
            (Self::N(left), Self::N(right)) => left == right,
            (Self::B(left), Self::B(right)) => left == right,
            (Self::Bool(left), Self::Bool(right)) => left == right,
            (Self::Null, Self::Null) => true,
            (Self::L(left), Self::L(right)) => left == right,
            (Self::M(left), Self::M(right)) => left == right,
            (Self::Ss(left), Self::Ss(right)) => same_set(left, right),
            (Self::Ns(left), Self::Ns(right)) => same_set(left, right),
            (Self::Bs(left), Self::Bs(right)) => same_set(left, right),
            _ => false,
        }
    }
}

impl Eq for AttributeValue {}

impl fmt::Debug for AttributeValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::S(value) => write!(f, "{{S: {value:?}}}"),
            Self::N(value) => write!(f, "{{N: {}}}", value.as_str()),
            Self::B(value) => write!(f, "{{B: <{} bytes>}}", value.len()),
            Self::Bool(value) => write!(f, "{{BOOL: {value}}}"),
            Self::Null => f.write_str("{NULL: true}"),
            Self::L(values) => f.debug_map().entry(&"L", values).finish(),
            Self::M(values) => f.debug_map().entry(&"M", values).finish(),
            Self::Ss(values) => f.debug_map().entry(&"SS", values).finish(),
            Self::Ns(values) => {
                f.debug_map().entry(&"NS", &values.iter().map(Number::as_str).collect::<Vec<_>>()).finish()
            }
            Self::Bs(values) => write!(f, "{{BS: <{} values>}}", values.len()),
        }
    }
}

impl From<&str> for AttributeValue {
    fn from(value: &str) -> Self {
        Self::S(value.to_owned())
    }
}

impl From<String> for AttributeValue {
    fn from(value: String) -> Self {
        Self::S(value)
    }
}

impl From<i64> for AttributeValue {
    fn from(value: i64) -> Self {
        Self::N(Number::from(value))
    }
}

impl From<bool> for AttributeValue {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

impl From<Number> for AttributeValue {
    fn from(value: Number) -> Self {
        Self::N(value)
    }
}

/// Builds an [`Item`] from `(name, value)` pairs: `item([("pk", "a".into()), ("n", 1.into())])`.
pub fn item<const N: usize>(pairs: [(&str, AttributeValue); N]) -> Item {
    pairs.into_iter().map(|(name, value)| (name.to_owned(), value)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sets_compare_as_sets() {
        let left = AttributeValue::Ss(vec!["a".into(), "b".into()]);
        let right = AttributeValue::Ss(vec!["b".into(), "a".into()]);
        assert_eq!(left, right);
        assert_ne!(left, AttributeValue::Ss(vec!["a".into()]));
        assert_ne!(AttributeValue::L(vec!["a".into(), "b".into()]), AttributeValue::L(vec!["b".into(), "a".into()]));
    }

    #[test]
    fn large_sets_are_checked_and_compared_as_sets() {
        // 60,000 numbers fit in an item (about 210 KB); quadratic checks took seconds.
        let numbers: Vec<Number> = (0..60_000).map(Number::from).collect();
        let reversed: Vec<Number> = numbers.iter().rev().cloned().collect();
        assert_eq!(AttributeValue::Ns(numbers.clone()), AttributeValue::Ns(reversed));
        assert!(AttributeValue::Ns(numbers.clone()).validate().is_ok());
        let mut duplicated = numbers.clone();
        duplicated.push(Number::from(59_999));
        assert!(AttributeValue::Ns(duplicated).validate().unwrap_err().contains("duplicates"));
        let mut other = numbers.clone();
        other[30_000] = Number::from(-1);
        assert_ne!(AttributeValue::Ns(numbers), AttributeValue::Ns(other));
        let words: Vec<String> = (0..60_000).map(|index| index.to_string()).collect();
        assert!(AttributeValue::Ss(words.clone()).validate().is_ok());
        assert!(AttributeValue::Ss([words, vec!["7".into()]].concat()).validate().unwrap_err().contains("duplicates"));
    }

    #[test]
    fn scalar_ordering_follows_dynamodb() {
        assert_eq!(AttributeValue::s("B").cmp_scalar(&AttributeValue::s("a")), Some(Ordering::Less));
        assert_eq!(AttributeValue::n(10).cmp_scalar(&AttributeValue::n(9)), Some(Ordering::Greater));
        assert_eq!(
            AttributeValue::b(vec![0xff]).cmp_scalar(&AttributeValue::b(vec![0x01, 0x02])),
            Some(Ordering::Greater)
        );
        assert_eq!(AttributeValue::s("1").cmp_scalar(&AttributeValue::n(1)), None);
    }

    #[test]
    fn validation_rejects_empty_and_duplicate_sets() {
        assert!(AttributeValue::Ss(vec![]).validate().unwrap_err().contains("may not be empty"));
        assert!(
            AttributeValue::Ns(vec![Number::from(1), Number::from(1)]).validate().unwrap_err().contains("duplicates")
        );
        let mut deep = AttributeValue::Null;
        for _ in 0..40 {
            deep = AttributeValue::L(vec![deep]);
        }
        assert!(deep.validate().unwrap_err().contains("Nesting Levels"));
    }

    #[test]
    fn type_descriptors_round_trip() {
        for kind in ValueType::ALL {
            assert_eq!(ValueType::parse(kind.as_str()), Some(kind));
        }
    }
}
