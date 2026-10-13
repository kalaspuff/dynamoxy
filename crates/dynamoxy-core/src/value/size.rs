//! Item sizes, computed with DynamoDB's rules ("Item sizes and formats" in the developer guide).
//!
//! Sizes drive the 400 KB item limit, the 1 MB page limit of Query and Scan, and the consumed
//! capacity the in-memory backend reports.

use super::{AttributeValue, Item};

/// Largest item DynamoDB stores: 400 KB, attribute names included.
pub const MAX_ITEM_SIZE: usize = 400 * 1024;

/// Size of one value in bytes (without its attribute name).
///
/// Strings and binaries count their bytes; numbers 1 byte per two significant digits plus 1;
/// booleans and nulls 1; lists and maps 3 bytes plus 1 byte and the size of each element (and
/// each key's length for maps); sets the sum of their elements.
pub fn value_size(value: &AttributeValue) -> usize {
    match value {
        AttributeValue::S(text) => text.len(),
        AttributeValue::N(number) => number.significant_digits().div_ceil(2) + 1,
        AttributeValue::B(bytes) => bytes.len(),
        AttributeValue::Bool(_) | AttributeValue::Null => 1,
        AttributeValue::L(values) => 3 + values.iter().map(|value| value_size(value) + 1).sum::<usize>(),
        AttributeValue::M(values) => {
            3 + values.iter().map(|(name, value)| name.len() + value_size(value) + 1).sum::<usize>()
        }
        AttributeValue::Ss(values) => values.iter().map(String::len).sum(),
        AttributeValue::Ns(values) => values.iter().map(|number| number.significant_digits().div_ceil(2) + 1).sum(),
        AttributeValue::Bs(values) => values.iter().map(Vec::len).sum(),
    }
}

/// Size of an item in bytes: attribute name lengths plus value sizes.
pub fn item_size(item: &Item) -> usize {
    item.iter().map(|(name, value)| name.len() + value_size(value)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::item;

    #[test]
    fn sizes_follow_the_documented_rules() {
        assert_eq!(value_size(&AttributeValue::s("héllo")), 6);
        assert_eq!(value_size(&AttributeValue::n(12345)), 4);
        assert_eq!(value_size(&AttributeValue::Bool(true)), 1);
        assert_eq!(value_size(&AttributeValue::L(vec![AttributeValue::s("ab")])), 6);
        let doc = item([("pk", AttributeValue::s("user#1")), ("age", AttributeValue::n(42))]);
        assert_eq!(item_size(&doc), 2 + 6 + 3 + 2);
    }
}
