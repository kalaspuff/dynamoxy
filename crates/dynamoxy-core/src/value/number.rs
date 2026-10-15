//! [`Number`]: DynamoDB's number type.
//!
//! DynamoDB numbers are decimals with up to 38 significant digits and a magnitude between
//! `1E-130` and `9.9999999999999999999999999999999999999E+125`. The service stores them in a
//! canonical form (no exponent, no leading zeros, no trailing fractional zeros, `-0` is `0`),
//! so `"1.50"`, `"1.5"` and `"15E-1"` are the same number and are all returned as `"1.5"`.
//!
//! [`Number`] keeps that canonical text, which makes equality and hashing exact, and implements
//! numeric ordering and exact addition/subtraction (used by the in-memory backend for `ADD`,
//! `SET a = a + :n` and comparisons).

use std::cmp::Ordering;
use std::fmt;

/// Maximum number of significant digits of a DynamoDB number.
pub const MAX_SIGNIFICANT_DIGITS: usize = 38;
/// Largest allowed decimal exponent of the leading digit (`9.99...E+125`).
pub const MAX_ADJUSTED_EXPONENT: i64 = 125;
/// Smallest allowed decimal exponent of the leading digit (`1E-130`).
pub const MIN_ADJUSTED_EXPONENT: i64 = -130;

/// Why a text or an arithmetic result is not a valid DynamoDB number.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum NumberError {
    /// The text is not a decimal number.
    #[error("The parameter cannot be converted to a numeric value: {0}")]
    Invalid(String),
    /// More than 38 significant digits.
    #[error("Attempting to store more than 38 significant digits in a Number")]
    TooPrecise,
    /// The magnitude is larger than `9.99...E+125`.
    #[error("Number overflow. Attempting to store a number with magnitude larger than supported range")]
    Overflow,
    /// The magnitude is smaller than `1E-130` (and not zero).
    #[error("Number underflow. Attempting to store a number with magnitude smaller than supported range")]
    Underflow,
}

/// A validated DynamoDB number in canonical form.
///
/// Equality is numeric equality (the canonical text of a value is unique). Ordering is numeric.
#[derive(Clone, PartialEq, Eq, Hash)]
pub struct Number(String);

/// A decimal as sign, digits (no leading or trailing zeros; empty for zero) and exponent:
/// `value = digits × 10^exponent`.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Decimal {
    negative: bool,
    digits: Vec<u8>,
    exponent: i64,
}

impl Decimal {
    fn zero() -> Self {
        Self { negative: false, digits: Vec::new(), exponent: 0 }
    }

    fn is_zero(&self) -> bool {
        self.digits.is_empty()
    }

    /// Parses decimal text: optional sign, digits with an optional point, optional exponent.
    fn parse(text: &str) -> Option<Self> {
        let bytes = text.as_bytes();
        let mut index = 0;
        let mut negative = false;
        if let Some(&sign) = bytes.first()
            && (sign == b'+' || sign == b'-')
        {
            negative = sign == b'-';
            index = 1;
        }
        let mut digits = Vec::with_capacity(bytes.len());
        let mut exponent: i64 = 0;
        let mut seen_digit = false;
        let mut seen_point = false;
        while index < bytes.len() {
            let byte = bytes[index];
            match byte {
                b'0'..=b'9' => {
                    seen_digit = true;
                    digits.push(byte - b'0');
                    if seen_point {
                        exponent -= 1;
                    }
                }
                b'.' if !seen_point => seen_point = true,
                b'e' | b'E' => break,
                _ => return None,
            }
            index += 1;
        }
        if !seen_digit {
            return None;
        }
        if index < bytes.len() {
            // Exponent part: [eE][+-]?digits
            let rest = &text[index + 1..];
            let (sign, body) = match rest.as_bytes().first() {
                Some(b'+') => (1, &rest[1..]),
                Some(b'-') => (-1, &rest[1..]),
                _ => (1, rest),
            };
            if body.is_empty() || !body.bytes().all(|byte| byte.is_ascii_digit()) {
                return None;
            }
            // Exponents this large are outside DynamoDB's range anyway; clamping avoids overflow.
            let magnitude: i64 = body.parse::<i64>().unwrap_or(1_000_000).min(1_000_000);
            exponent = exponent.saturating_add(sign * magnitude);
        }
        Some(Self::normalized(negative, digits, exponent))
    }

    /// Strips leading and trailing zeros (adjusting the exponent) and normalizes zero.
    fn normalized(negative: bool, mut digits: Vec<u8>, mut exponent: i64) -> Self {
        let leading = digits.iter().take_while(|digit| **digit == 0).count();
        digits.drain(..leading);
        while digits.last() == Some(&0) {
            digits.pop();
            exponent += 1;
        }
        if digits.is_empty() {
            return Self::zero();
        }
        Self { negative, digits, exponent }
    }

    /// Exponent of the leading digit (`1.5E+3` → 3).
    fn adjusted_exponent(&self) -> i64 {
        self.exponent + self.digits.len() as i64 - 1
    }

    fn check_limits(&self) -> Result<(), NumberError> {
        if self.is_zero() {
            return Ok(());
        }
        if self.digits.len() > MAX_SIGNIFICANT_DIGITS {
            return Err(NumberError::TooPrecise);
        }
        let adjusted = self.adjusted_exponent();
        if adjusted > MAX_ADJUSTED_EXPONENT {
            return Err(NumberError::Overflow);
        }
        if adjusted < MIN_ADJUSTED_EXPONENT {
            return Err(NumberError::Underflow);
        }
        Ok(())
    }

    /// Canonical text: plain notation without exponent.
    fn to_canonical(&self) -> String {
        if self.is_zero() {
            return "0".to_owned();
        }
        let mut out = String::with_capacity(self.digits.len() + 8);
        if self.negative {
            out.push('-');
        }
        let digits: String = self.digits.iter().map(|digit| char::from(b'0' + digit)).collect();
        if self.exponent >= 0 {
            out.push_str(&digits);
            out.extend(std::iter::repeat_n('0', self.exponent as usize));
        } else {
            let point = digits.len() as i64 + self.exponent;
            if point <= 0 {
                out.push_str("0.");
                out.extend(std::iter::repeat_n('0', (-point) as usize));
                out.push_str(&digits);
            } else {
                out.push_str(&digits[..point as usize]);
                out.push('.');
                out.push_str(&digits[point as usize..]);
            }
        }
        out
    }

    fn cmp_magnitude(&self, other: &Self) -> Ordering {
        match (self.is_zero(), other.is_zero()) {
            (true, true) => return Ordering::Equal,
            (true, false) => return Ordering::Less,
            (false, true) => return Ordering::Greater,
            (false, false) => {}
        }
        self.adjusted_exponent().cmp(&other.adjusted_exponent()).then_with(|| {
            // Same leading exponent: compare digit by digit (missing digits are zeros).
            let len = self.digits.len().max(other.digits.len());
            for index in 0..len {
                let left = self.digits.get(index).copied().unwrap_or(0);
                let right = other.digits.get(index).copied().unwrap_or(0);
                match left.cmp(&right) {
                    Ordering::Equal => {}
                    unequal => return unequal,
                }
            }
            Ordering::Equal
        })
    }

    /// Numeric order (the reference for [`Number`]'s text-based `Ord`; tests compare them).
    #[cfg(test)]
    fn cmp_value(&self, other: &Self) -> Ordering {
        match (self.negative && !self.is_zero(), other.negative && !other.is_zero()) {
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
            (false, false) => self.cmp_magnitude(other),
            (true, true) => other.cmp_magnitude(self),
        }
    }

    /// Digits scaled to `exponent` (which must be <= self.exponent), least significant first.
    fn scaled_le(&self, exponent: i64) -> Vec<u8> {
        let shift = (self.exponent - exponent) as usize;
        let mut out = vec![0u8; shift];
        out.extend(self.digits.iter().rev());
        out
    }

    fn add(&self, other: &Self) -> Self {
        if self.is_zero() {
            return other.clone();
        }
        if other.is_zero() {
            return self.clone();
        }
        let exponent = self.exponent.min(other.exponent);
        let left = self.scaled_le(exponent);
        let right = other.scaled_le(exponent);
        if self.negative == other.negative {
            let mut sum = Vec::with_capacity(left.len().max(right.len()) + 1);
            let mut carry = 0u8;
            for index in 0..left.len().max(right.len()) {
                let total = left.get(index).copied().unwrap_or(0) + right.get(index).copied().unwrap_or(0) + carry;
                sum.push(total % 10);
                carry = total / 10;
            }
            if carry > 0 {
                sum.push(carry);
            }
            sum.reverse();
            return Self::normalized(self.negative, sum, exponent);
        }
        // Different signs: subtract the smaller magnitude from the larger one.
        let (larger, smaller, negative) = match self.cmp_magnitude(other) {
            Ordering::Equal => return Self::zero(),
            Ordering::Greater => (left, right, self.negative),
            Ordering::Less => (right, left, other.negative),
        };
        let mut difference = Vec::with_capacity(larger.len());
        let mut borrow = 0i8;
        for (index, digit) in larger.iter().enumerate() {
            let mut value = *digit as i8 - smaller.get(index).copied().unwrap_or(0) as i8 - borrow;
            if value < 0 {
                value += 10;
                borrow = 1;
            } else {
                borrow = 0;
            }
            difference.push(value as u8);
        }
        difference.reverse();
        Self::normalized(negative, difference, exponent)
    }

    fn negated(&self) -> Self {
        let mut out = self.clone();
        if !out.is_zero() {
            out.negative = !out.negative;
        }
        out
    }
}

/// Significant digits and adjusted exponent of `text` if it is a canonical number within
/// DynamoDB's limits (what [`Number`] stores), else `None`.
///
/// Canonical: an optional `-`, then `0`, or an integer without leading zeros, optionally with a
/// fraction that does not end in `0` (`0.5`, `12.25`, `100`). Zero is `0`, never `-0`.
fn canonical(text: &str) -> Option<(usize, i64)> {
    let (negative, body) = match text.strip_prefix('-') {
        Some(body) => (true, body),
        None => (false, text),
    };
    let (integer, fraction) = match body.split_once('.') {
        Some((integer, fraction)) => (integer, Some(fraction)),
        None => (body, None),
    };
    let integer = integer.as_bytes();
    if integer.is_empty() || !integer.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let (digits, adjusted) = match fraction {
        None => {
            if integer[0] == b'0' {
                // "0" is canonical; "-0" and leading zeros are not.
                return (integer.len() == 1 && !negative).then_some((1, 0));
            }
            let trailing = integer.iter().rev().take_while(|digit| **digit == b'0').count();
            (integer.len() - trailing, integer.len() as i64 - 1)
        }
        Some(fraction) => {
            let fraction = fraction.as_bytes();
            if fraction.is_empty() || !fraction.iter().all(u8::is_ascii_digit) || fraction.last() == Some(&b'0') {
                return None;
            }
            if integer == b"0" {
                let leading = fraction.iter().take_while(|digit| **digit == b'0').count();
                (fraction.len() - leading, -(leading as i64) - 1)
            } else if integer[0] == b'0' {
                return None;
            } else {
                (integer.len() + fraction.len(), integer.len() as i64 - 1)
            }
        }
    };
    let within =
        digits <= MAX_SIGNIFICANT_DIGITS && (MIN_ADJUSTED_EXPONENT..=MAX_ADJUSTED_EXPONENT).contains(&adjusted);
    within.then_some((digits, adjusted))
}

/// Compares the magnitudes of two canonical numbers without signs (`"0.5"`, `"12"`).
fn cmp_canonical_magnitude(left: &str, right: &str) -> Ordering {
    let (left_integer, left_fraction) = left.split_once('.').unwrap_or((left, ""));
    let (right_integer, right_fraction) = right.split_once('.').unwrap_or((right, ""));
    // Integer parts have no leading zeros and fractions no trailing zeros, so text order is
    // numeric order once the integer parts have the same length.
    left_integer
        .len()
        .cmp(&right_integer.len())
        .then_with(|| left_integer.cmp(right_integer))
        .then_with(|| left_fraction.cmp(right_fraction))
}

impl Number {
    /// Parses and validates `text`, returning the canonical number.
    ///
    /// Accepts an optional sign, digits with an optional decimal point and an optional exponent
    /// (`"-1.5e3"`). Whitespace, underscores, `NaN` and infinities are rejected, like DynamoDB.
    pub fn parse(text: &str) -> Result<Self, NumberError> {
        if canonical(text).is_some() {
            return Ok(Self(text.to_owned()));
        }
        let decimal = Decimal::parse(text).ok_or_else(|| NumberError::Invalid(text.to_owned()))?;
        decimal.check_limits()?;
        Ok(Self(decimal.to_canonical()))
    }

    /// Like [`Self::parse`], taking ownership of the text (no copy when it is canonical already,
    /// as DynamoDB's responses are).
    pub fn parse_owned(text: String) -> Result<Self, NumberError> {
        if canonical(&text).is_some() { Ok(Self(text)) } else { Self::parse(&text) }
    }

    /// The number zero.
    pub fn zero() -> Self {
        Self("0".to_owned())
    }

    /// The canonical text (`"1.5"`, `"-20"`, `"0.001"`).
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Consumes the number and returns its canonical text.
    pub fn into_string(self) -> String {
        self.0
    }

    /// Whether the number is zero.
    pub fn is_zero(&self) -> bool {
        self.0 == "0"
    }

    /// Whether the number is an integer (no fractional digits).
    pub fn is_integer(&self) -> bool {
        !self.0.contains('.')
    }

    /// Number of significant digits (used for DynamoDB's item size calculation).
    pub fn significant_digits(&self) -> usize {
        match canonical(&self.0) {
            Some((digits, _)) => digits.max(1),
            None => self.decimal().digits.len().max(1),
        }
    }

    fn decimal(&self) -> Decimal {
        // The canonical text always parses.
        Decimal::parse(&self.0).unwrap_or_else(Decimal::zero)
    }

    /// Exact sum, failing if the result is outside DynamoDB's range or precision.
    pub fn checked_add(&self, other: &Self) -> Result<Self, NumberError> {
        let sum = self.decimal().add(&other.decimal());
        sum.check_limits()?;
        Ok(Self(sum.to_canonical()))
    }

    /// Exact difference, failing if the result is outside DynamoDB's range or precision.
    pub fn checked_sub(&self, other: &Self) -> Result<Self, NumberError> {
        let difference = self.decimal().add(&other.decimal().negated());
        difference.check_limits()?;
        Ok(Self(difference.to_canonical()))
    }
}

impl From<i64> for Number {
    fn from(value: i64) -> Self {
        Self(value.to_string())
    }
}

impl From<u64> for Number {
    fn from(value: u64) -> Self {
        Self(value.to_string())
    }
}

impl From<i32> for Number {
    fn from(value: i32) -> Self {
        Self(value.to_string())
    }
}

impl From<u32> for Number {
    fn from(value: u32) -> Self {
        Self(value.to_string())
    }
}

impl std::str::FromStr for Number {
    type Err = NumberError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::parse(text)
    }
}

impl PartialOrd for Number {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Number {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.0 == other.0 {
            return Ordering::Equal;
        }
        // The text is canonical (zero is "0"), so the sign decides first.
        match (self.0.strip_prefix('-'), other.0.strip_prefix('-')) {
            (None, None) => cmp_canonical_magnitude(&self.0, &other.0),
            (Some(left), Some(right)) => cmp_canonical_magnitude(right, left),
            (None, Some(_)) => Ordering::Greater,
            (Some(_), None) => Ordering::Less,
        }
    }
}

impl fmt::Display for Number {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl fmt::Debug for Number {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Number({})", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(text: &str) -> Number {
        Number::parse(text).unwrap_or_else(|err| panic!("{text}: {err}"))
    }

    #[test]
    fn canonical_forms() {
        for (input, expected) in [
            ("0", "0"),
            ("-0", "0"),
            ("+0.000", "0"),
            ("1.50", "1.5"),
            ("15E-1", "1.5"),
            ("1e2", "100"),
            ("0001.2300", "1.23"),
            ("-0.0010", "-0.001"),
            (".5", "0.5"),
            ("5.", "5"),
            ("1E+3", "1000"),
            ("123456789012345678901234567890.12345678", "123456789012345678901234567890.12345678"),
        ] {
            assert_eq!(n(input).as_str(), expected, "{input}");
        }
        assert_eq!(n("1E-130").as_str(), format!("0.{}1", "0".repeat(129)));
        assert_eq!(n("1E+5").as_str(), "100000");
    }

    #[test]
    fn invalid_numbers_are_rejected() {
        for input in
            ["", " 1", "1 ", "1_000", "abc", "1e", "1e+", "--1", "NaN", "inf", "1.2.3", "+", "-", ".", "e5", "0x10"]
        {
            assert!(matches!(Number::parse(input), Err(NumberError::Invalid(_))), "{input:?}");
        }
    }

    #[test]
    fn limits_follow_dynamodb() {
        assert!(Number::parse("9.9999999999999999999999999999999999999E+125").is_ok());
        assert_eq!(Number::parse("1E+126"), Err(NumberError::Overflow));
        assert!(Number::parse("1E-130").is_ok());
        assert_eq!(Number::parse("1E-131"), Err(NumberError::Underflow));
        assert!(Number::parse("12345678901234567890123456789012345678").is_ok());
        assert_eq!(Number::parse("123456789012345678901234567890123456789"), Err(NumberError::TooPrecise));
        // Trailing zeros are not significant.
        assert!(Number::parse("1234567890123456789012345678901234567800000").is_ok());
    }

    #[test]
    fn ordering_is_numeric() {
        let mut values: Vec<Number> = ["10", "-1", "2", "1.5", "-10.25", "0", "100", "0.001", "-0.5"].map(n).to_vec();
        values.sort();
        let sorted: Vec<&str> = values.iter().map(Number::as_str).collect();
        assert_eq!(sorted, ["-10.25", "-1", "-0.5", "0", "0.001", "1.5", "2", "10", "100"]);
        assert_eq!(n("1.0"), n("1"));
    }

    #[test]
    fn arithmetic_is_exact() {
        assert_eq!(n("0.1").checked_add(&n("0.2")).unwrap(), n("0.3"));
        assert_eq!(n("1").checked_sub(&n("1.5")).unwrap(), n("-0.5"));
        assert_eq!(n("-5").checked_add(&n("5")).unwrap(), Number::zero());
        assert_eq!(n("999").checked_add(&n("1")).unwrap(), n("1000"));
        assert_eq!(n("-999").checked_sub(&n("1")).unwrap(), n("-1000"));
        assert_eq!(n("1E+125").checked_sub(&n("1")).unwrap_err(), NumberError::TooPrecise);
        assert_eq!(n("9E+125").checked_add(&n("1E+125")).unwrap_err(), NumberError::Overflow);
        assert_eq!(n("12345678901234567890").checked_add(&n("0.5")).unwrap(), n("12345678901234567890.5"));
    }

    #[test]
    fn fast_paths_agree_with_the_general_algorithm() {
        let inputs = [
            "0",
            "-0",
            "1",
            "-1",
            "10",
            "100",
            "1500",
            "007",
            "0.5",
            "-0.5",
            "0.05",
            "00.5",
            "0.50",
            "1.0",
            "12.25",
            "-12.25",
            "1e3",
            "1E-3",
            ".5",
            "5.",
            "+1",
            "1.",
            "0.0",
            "-0.001",
            "123456789012345678901234567890.12345678",
            "12345678901234567890123456789012345678",
            "123456789012345678901234567890123456789",
            "1234567890123456789012345678901234567800000",
        ];
        for input in inputs {
            let general = Decimal::parse(input).map(|decimal| (decimal.check_limits(), decimal));
            match general {
                Some((Ok(()), decimal)) => {
                    let parsed = n(input);
                    assert_eq!(parsed.as_str(), decimal.to_canonical(), "{input}");
                    assert_eq!(parsed.significant_digits(), decimal.digits.len().max(1), "{input}");
                    if let Some((digits, adjusted)) = canonical(input) {
                        assert_eq!(input, decimal.to_canonical(), "{input} is not canonical");
                        assert_eq!(digits, decimal.digits.len().max(1), "{input}");
                        assert_eq!(
                            adjusted,
                            if decimal.is_zero() { 0 } else { decimal.adjusted_exponent() },
                            "{input}"
                        );
                    }
                }
                _ => assert!(Number::parse(input).is_err(), "{input}"),
            }
        }
        let mut values: Vec<Number> =
            ["10", "-1", "2", "1.5", "-10.25", "0", "100", "0.001", "-0.5", "0.25", "-0.25", "9.99", "10.01"]
                .map(n)
                .to_vec();
        for left in &values {
            for right in &values {
                assert_eq!(left.cmp(right), left.decimal().cmp_value(&right.decimal()), "{left} vs {right}");
            }
        }
        values.sort();
        assert_eq!(values.first().map(Number::as_str), Some("-10.25"));
        assert_eq!(Number::parse_owned("12.5".to_owned()).unwrap(), n("12.5"));
        assert_eq!(Number::parse_owned("12.50".to_owned()).unwrap(), n("12.5"));
    }

    #[test]
    fn integer_detection_and_digits() {
        assert!(n("100").is_integer());
        assert!(!n("1.5").is_integer());
        assert_eq!(n("1500").significant_digits(), 2);
        assert_eq!(n("0").significant_digits(), 1);
    }
}
