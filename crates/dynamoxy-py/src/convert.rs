//! Conversions between Python objects and the core's types.
//!
//! Python values map to DynamoDB types like this (both directions):
//!
//! | Python | DynamoDB |
//! | --- | --- |
//! | `str` | `S` |
//! | `int`, `float`, `decimal.Decimal` | `N` (read back as `int` when integral, else `Decimal`) |
//! | `bytes`, `bytearray`, `memoryview` | `B` (read back as `bytes`) |
//! | `bool` | `BOOL` |
//! | `None` | `NULL` |
//! | `list`, `tuple` | `L` (read back as `list`) |
//! | `dict` (string keys) | `M` |
//! | `set`/`frozenset` of `str` / numbers / bytes | `SS` / `NS` / `BS` (read back as `set`) |
//!
//! Anything else raises `ValidationError` naming the accepted types.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};
use std::time::Instant;

use dynamoxy_core::types::{
    BackupDescription, BackupDetails, BackupSummary, Capacity, ConsumedCapacity, ContinuousBackupsDescription,
    IndexDescription, ItemCollectionMetrics, KeySchema, KeysAndAttributes, Projection, ProvisionedThroughput,
    TableDescription, TimeToLiveDescription, WriteRequest,
};
use dynamoxy_core::value::NumberError;
use dynamoxy_core::{AttributeValue, Item, Number, OperationStats};
use pyo3::exceptions::PyTypeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyMemoryView, PySet, PyString, PyTuple,
    PyType,
};

use crate::errors::ValidationError;

static DECIMAL: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MAPPING: PyOnceLock<Py<PyType>> = PyOnceLock::new();

fn decimal_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    DECIMAL.import(py, "decimal", "Decimal")
}

fn mapping_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    MAPPING.import(py, "collections.abc", "Mapping")
}

/// Where a value is in the argument being converted, for error messages (`tags[2]`,
/// `address.city`). Rendered only when there is an error.
#[derive(Clone, Copy)]
enum ValuePath<'a> {
    Root(&'a str),
    Member(&'a ValuePath<'a>, &'a str),
    Element(&'a ValuePath<'a>, usize),
}

impl ValuePath<'_> {
    fn is_empty(&self) -> bool {
        match self {
            Self::Root(name) => name.is_empty(),
            Self::Member(parent, name) => parent.is_empty() && name.is_empty(),
            Self::Element(..) => false,
        }
    }
}

impl std::fmt::Display for ValuePath<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Root(name) => f.write_str(name),
            Self::Member(parent, name) if parent.is_empty() => f.write_str(name),
            Self::Member(parent, name) => write!(f, "{parent}.{name}"),
            Self::Element(parent, index) => write!(f, "{parent}[{index}]"),
        }
    }
}

fn number(text: &str, path: &ValuePath<'_>) -> PyResult<Number> {
    Number::parse(text).map_err(|error| match error {
        // DynamoDB's wording quotes the text; values stay out of messages (they may be personal data).
        NumberError::Invalid(_) => ValidationError::new_err(format!("{path}: not a number")),
        other => ValidationError::new_err(format!("{path}: {other}")),
    })
}

/// The text of a `str`, refusing one that is not valid Unicode (a lone surrogate, from
/// `json.loads('"\\ud800"')`), which cannot be sent: a `ValidationError`, not Python's
/// `UnicodeEncodeError`.
fn text(value: &Bound<'_, PyString>, path: &dyn std::fmt::Display) -> PyResult<String> {
    match value.to_str() {
        Ok(text) => Ok(text.to_owned()),
        Err(_) => Err(ValidationError::new_err(format!(
            "{path}: the text is not valid Unicode (it contains a lone surrogate)"
        ))),
    }
}

/// The digits of an `int` that does not fit 64 bits, as `int` itself writes them (a subclass's
/// own `__str__` is not the number). Numbers above DynamoDB's range (about 1E126, 419 bits) are
/// refused before they are written out, which Python refuses above 4,300 digits.
fn big_int(value: &Bound<'_, PyAny>, path: &ValuePath<'_>) -> PyResult<Number> {
    let py = value.py();
    let bits: u64 = py.get_type::<PyInt>().call_method1(intern!(py, "bit_length"), (value,))?.extract()?;
    if bits > 419 {
        return Err(ValidationError::new_err(format!(
            "{path}: the number is too large for DynamoDB (its numbers are below 1E+126)"
        )));
    }
    let digits = py.get_type::<PyInt>().call_method1(intern!(py, "__repr__"), (value,))?;
    number(&digits.cast::<PyString>()?.to_cow()?, path)
}

fn type_name(value: &Bound<'_, PyAny>) -> String {
    value.get_type().name().map(|name| name.to_string()).unwrap_or_else(|_| "?".into())
}

/// How deep DynamoDB nests documents: a value inside 32 lists or maps is refused. Conversions
/// stop there, so a deeper value (or one that contains itself) raises `ValidationError` instead
/// of exhausting the stack.
const MAX_DEPTH: usize = 32;

/// DynamoDB's message for values nested more than [`MAX_DEPTH`] levels deep.
fn too_deep(path: &dyn std::fmt::Display) -> PyErr {
    ValidationError::new_err(format!(
        "{path}: Nesting Levels have exceeded supported limits: Attributes in the item have nested levels beyond \
         supported limit"
    ))
}

/// Converts a Python value to an attribute value. `path` names the value in error messages.
pub fn py_to_av(value: &Bound<'_, PyAny>, path: &str) -> PyResult<AttributeValue> {
    to_av(value, &ValuePath::Root(path), 1)
}

/// Converts a value `depth` levels deep (an attribute's value is 1 level deep).
fn to_av(value: &Bound<'_, PyAny>, path: &ValuePath<'_>, depth: usize) -> PyResult<AttributeValue> {
    if depth > MAX_DEPTH {
        return Err(too_deep(path));
    }
    let py = value.py();
    if value.is_none() {
        return Ok(AttributeValue::Null);
    }
    if let Ok(string) = value.cast_exact::<PyString>() {
        return Ok(AttributeValue::S(text(string, path)?));
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(AttributeValue::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(small) = value.extract::<i64>() {
            return Ok(AttributeValue::N(Number::from(small)));
        }
        if let Ok(wide) = value.extract::<i128>() {
            // Up to 39 digits: Number::parse refuses more than 38 significant ones, as for others.
            return Ok(AttributeValue::N(number(&wide.to_string(), path)?));
        }
        return Ok(AttributeValue::N(big_int(value, path)?));
    }
    if let Ok(float) = value.cast::<PyFloat>() {
        let float = float.value();
        if !float.is_finite() {
            return Err(ValidationError::new_err(format!(
                "{path}: DynamoDB numbers cannot be NaN or infinite, got {float}"
            )));
        }
        return Number::parse_owned(float.to_string())
            .map(AttributeValue::N)
            .map_err(|error| ValidationError::new_err(format!("{path}: {error}")));
    }
    if value.get_type().is(decimal_type(py)?) {
        return decimal(value, path);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return Ok(AttributeValue::M(dict_to_item(dict, path, depth + 1)?));
    }
    if let Ok(list) = value.cast::<PyList>() {
        let mut values = Vec::with_capacity(list.len());
        for (index, element) in list.iter().enumerate() {
            values.push(to_av(&element, &ValuePath::Element(path, index), depth + 1)?);
        }
        return Ok(AttributeValue::L(values));
    }
    if let Ok(string) = value.cast::<PyString>() {
        return Ok(AttributeValue::S(text(string, path)?));
    }
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(AttributeValue::B(bytes.as_bytes().to_vec()));
    }
    if let Ok(bytes) = value.cast::<PyByteArray>() {
        return Ok(AttributeValue::B(bytes.to_vec()));
    }
    if value.is_instance_of::<PyMemoryView>() {
        return Ok(AttributeValue::B(
            value.call_method0(intern!(py, "tobytes"))?.cast::<PyBytes>()?.as_bytes().to_vec(),
        ));
    }
    if value.is_instance(decimal_type(py)?)? {
        return decimal(value, path);
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        let mut values = Vec::with_capacity(tuple.len());
        for (index, element) in tuple.iter().enumerate() {
            values.push(to_av(&element, &ValuePath::Element(path, index), depth + 1)?);
        }
        return Ok(AttributeValue::L(values));
    }
    if value.is_instance_of::<PySet>() || value.is_instance_of::<PyFrozenSet>() {
        return set_to_av(value, path, depth);
    }
    if value.is_instance(mapping_type(py)?)? {
        let dict = PyDict::new(py);
        for pair in value.call_method0(intern!(py, "items"))?.try_iter()? {
            let (key, element): (Bound<'_, PyAny>, Bound<'_, PyAny>) = pair?.extract()?;
            dict.set_item(key, element)?;
        }
        return Ok(AttributeValue::M(dict_to_item(&dict, path, depth + 1)?));
    }
    Err(ValidationError::new_err(format!(
        "{path}: cannot store a value of type {} in DynamoDB; use str, int, float, Decimal, bytes, bool, None, list, tuple, dict or a set of str/numbers/bytes",
        type_name(value)
    )))
}

/// A `decimal.Decimal` (or a subclass) as a number. Its text tells NaN and infinities apart
/// (`NaN`, `sNaN`, `Infinity`, never in the text of a finite one), which saves calling
/// `is_finite()`.
fn decimal(value: &Bound<'_, PyAny>, path: &ValuePath<'_>) -> PyResult<AttributeValue> {
    let text = value.str()?;
    let text = text.to_cow()?;
    if text.bytes().any(|byte| byte == b'N' || byte == b'I') {
        return Err(ValidationError::new_err(format!(
            "{path}: DynamoDB numbers cannot be NaN or infinite, got Decimal({text})"
        )));
    }
    Ok(AttributeValue::N(number(&text, path)?))
}

fn set_to_av(value: &Bound<'_, PyAny>, path: &ValuePath<'_>, depth: usize) -> PyResult<AttributeValue> {
    let elements: Vec<Bound<'_, PyAny>> = value.try_iter()?.collect::<PyResult<_>>()?;
    if elements.is_empty() {
        return Err(ValidationError::new_err(format!(
            "{path}: DynamoDB does not store empty sets; leave the attribute out (or use None or an empty list)"
        )));
    }
    // Elements are no level of their own, as in DynamoDB; a set in a set (which DynamoDB refuses)
    // is converted one level deeper, so that frozensets nested in frozensets stay bounded too.
    let converted = elements
        .iter()
        .map(|element| {
            let nested = element.is_instance_of::<PySet>() || element.is_instance_of::<PyFrozenSet>();
            to_av(element, path, if nested { depth + 1 } else { depth })
        })
        .collect::<PyResult<Vec<_>>>()?;
    match converted[0] {
        AttributeValue::S(_) => converted
            .into_iter()
            .map(|value| match value {
                AttributeValue::S(text) => Ok(text),
                other => Err(mixed_set(path, &other)),
            })
            .collect::<PyResult<_>>()
            .map(AttributeValue::Ss),
        AttributeValue::N(_) => converted
            .into_iter()
            .map(|value| match value {
                AttributeValue::N(number) => Ok(number),
                other => Err(mixed_set(path, &other)),
            })
            .collect::<PyResult<Vec<_>>>()
            .map(|numbers| {
                // 1 and 1.0 are the same DynamoDB number (one canonical text): keep the set valid,
                // in linear time, since the conversion holds the GIL.
                let mut seen = HashSet::with_capacity(numbers.len());
                let first: Vec<bool> = numbers.iter().map(|number| seen.insert(number.as_str())).collect();
                drop(seen);
                AttributeValue::Ns(
                    numbers.into_iter().zip(first).filter_map(|(number, first)| first.then_some(number)).collect(),
                )
            }),
        AttributeValue::B(_) => converted
            .into_iter()
            .map(|value| match value {
                AttributeValue::B(bytes) => Ok(bytes),
                other => Err(mixed_set(path, &other)),
            })
            .collect::<PyResult<_>>()
            .map(AttributeValue::Bs),
        ref other => Err(ValidationError::new_err(format!(
            "{path}: sets may only contain str, numbers or bytes, got an element of DynamoDB type {}",
            other.value_type()
        ))),
    }
}

fn mixed_set(path: &ValuePath<'_>, value: &AttributeValue) -> PyErr {
    ValidationError::new_err(format!(
        "{path}: all elements of a set must have the same type (str, numbers or bytes); found a {} element",
        value.value_type()
    ))
}

/// Converts the members of a map whose values are `depth` levels deep.
fn dict_to_item(dict: &Bound<'_, PyDict>, path: &ValuePath<'_>, depth: usize) -> PyResult<Item> {
    let mut item = Item::new();
    for (key, value) in dict.iter() {
        let Ok(name) = key.cast::<PyString>() else {
            return Err(ValidationError::new_err(format!(
                "{path}: attribute names must be str, got {}",
                type_name(&key)
            )));
        };
        let name = text(name, path)?;
        let converted = to_av(&value, &ValuePath::Member(path, &name), depth)?;
        item.insert(name, converted);
    }
    Ok(item)
}

/// Converts a Python mapping (an item or a key) to an [`Item`].
pub fn py_to_item(value: &Bound<'_, PyAny>, what: &str) -> PyResult<Item> {
    if let Ok(dict) = value.cast::<PyDict>() {
        return dict_to_item(dict, &ValuePath::Root(""), 1);
    }
    match py_to_av(value, what)? {
        AttributeValue::M(item) => Ok(item),
        _ => Err(ValidationError::new_err(format!(
            "{what} must be a dict of attribute values, got {}",
            type_name(value)
        ))),
    }
}

/// FNV-1a: a fast hash for the short strings of [`ToPython`]'s caches.
struct Fnv(u64);

impl Default for Fnv {
    fn default() -> Self {
        Self(0xcbf2_9ce4_8422_2325)
    }
}

impl Hasher for Fnv {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 = (self.0 ^ u64::from(*byte)).wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
}

type FnvMap<K, V> = HashMap<K, V, BuildHasherDefault<Fnv>>;

/// Converts attribute values to Python objects. Converting many items (a page of a query), it
/// creates each attribute name string and each decimal once and shares them between the items
/// (both are immutable in Python), which also saves Python from hashing every key again.
struct ToPython<'py, 'i> {
    py: Python<'py>,
    shared: Option<Shared<'py, 'i>>,
}

/// Attribute name strings and decimals created so far, by their text.
type Shared<'py, 'i> = (FnvMap<&'i str, Bound<'py, PyString>>, FnvMap<&'i str, Bound<'py, PyAny>>);

impl<'py, 'i> ToPython<'py, 'i> {
    fn new(py: Python<'py>, share: bool) -> Self {
        Self { py, shared: share.then(Default::default) }
    }

    fn name(&mut self, name: &'i str) -> Bound<'py, PyString> {
        match &mut self.shared {
            Some((names, _)) => names.entry(name).or_insert_with(|| PyString::new(self.py, name)).clone(),
            None => PyString::new(self.py, name),
        }
    }

    fn number(&mut self, number: &'i Number) -> PyResult<Bound<'py, PyAny>> {
        if number.is_integer() {
            return number_to_py(self.py, number);
        }
        match &mut self.shared {
            Some((_, decimals)) => {
                if let Some(decimal) = decimals.get(number.as_str()) {
                    return Ok(decimal.clone());
                }
                let decimal = number_to_py(self.py, number)?;
                decimals.insert(number.as_str(), decimal.clone());
                Ok(decimal)
            }
            None => number_to_py(self.py, number),
        }
    }

    fn item(&mut self, item: &'i Item) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(self.py);
        for (name, value) in item {
            dict.set_item(self.name(name), self.value(value)?)?;
        }
        Ok(dict)
    }

    fn value(&mut self, value: &'i AttributeValue) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        Ok(match value {
            AttributeValue::S(text) => PyString::new(py, text).into_any(),
            AttributeValue::N(number) => self.number(number)?,
            AttributeValue::B(bytes) => PyBytes::new(py, bytes).into_any(),
            AttributeValue::Bool(flag) => PyBool::new(py, *flag).to_owned().into_any(),
            AttributeValue::Null => py.None().into_bound(py),
            AttributeValue::L(values) => {
                let list = PyList::empty(py);
                for value in values {
                    list.append(self.value(value)?)?;
                }
                list.into_any()
            }
            AttributeValue::M(values) => self.item(values)?.into_any(),
            AttributeValue::Ss(values) => PySet::new(py, values)?.into_any(),
            AttributeValue::Ns(values) => {
                let set = PySet::empty(py)?;
                for number in values {
                    set.add(self.number(number)?)?;
                }
                set.into_any()
            }
            AttributeValue::Bs(values) => {
                let set = PySet::empty(py)?;
                for bytes in values {
                    set.add(PyBytes::new(py, bytes))?;
                }
                set.into_any()
            }
        })
    }
}

/// A number as `int` when it is integral, otherwise as `decimal.Decimal`.
pub fn number_to_py<'py>(py: Python<'py>, number: &Number) -> PyResult<Bound<'py, PyAny>> {
    if number.is_integer() {
        if let Ok(small) = number.as_str().parse::<i64>() {
            return Ok(small.into_pyobject(py)?.into_any());
        }
        return py.get_type::<PyInt>().call1((number.as_str(),));
    }
    decimal_type(py)?.call1((number.as_str(),))
}

/// Converts an item to a Python `dict`.
pub fn item_to_py<'py>(py: Python<'py>, item: &Item) -> PyResult<Bound<'py, PyDict>> {
    ToPython::new(py, false).item(item)
}

/// Converts an optional item.
pub fn optional_item<'py>(py: Python<'py>, item: Option<&Item>) -> PyResult<Bound<'py, PyAny>> {
    match item {
        Some(item) => Ok(item_to_py(py, item)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// Converts a list of items (sharing attribute names and decimals between them).
pub fn items_to_py<'py>(py: Python<'py>, items: &[Item]) -> PyResult<Bound<'py, PyList>> {
    let mut convert = ToPython::new(py, items.len() > 1);
    let list = PyList::empty(py);
    for item in items {
        list.append(convert.item(item)?)?;
    }
    Ok(list)
}

/// How many items [`ItemsInSteps`] converts between two looks at the clock.
const ITEMS_PER_CHECK: usize = 32;

/// Up to how many converted items [`ItemsInSteps`] frees on the event loop's thread.
const FREED_HERE: usize = 1_000;

/// Lists of items converted to Python lists a bounded amount at a time, for outcomes built in
/// steps (`runtime::Deferred::in_steps`): a page of thousands of items does not hold the event
/// loop for its whole conversion.
pub struct ItemsInSteps {
    lists: Vec<Vec<Item>>,
    /// The list being converted, and how many of its items are.
    list: usize,
    done: usize,
    built: Vec<Py<PyList>>,
}

impl ItemsInSteps {
    /// Lists to convert, in order.
    pub fn new(lists: Vec<Vec<Item>>) -> Self {
        Self { lists, list: 0, done: 0, built: Vec::new() }
    }

    /// Converts items until every list is converted (returning them) or `deadline` has passed
    /// (`None`; call it again). Each call converts some items, so that steps make progress.
    pub fn step<'py>(&mut self, py: Python<'py>, deadline: Instant) -> PyResult<Option<Vec<Bound<'py, PyList>>>> {
        let mut converted = false;
        while self.list < self.lists.len() {
            if self.built.len() == self.list {
                self.built.push(PyList::empty(py).unbind());
            }
            let target = self.built[self.list].bind(py);
            let items = &self.lists[self.list];
            // Names and decimals are shared between the items of one step.
            let mut convert = ToPython::new(py, items.len() - self.done > 1);
            while self.done < items.len() {
                if converted && Instant::now() >= deadline {
                    return Ok(None);
                }
                let end = (self.done + ITEMS_PER_CHECK).min(items.len());
                for item in &items[self.done..end] {
                    target.append(convert.item(item)?)?;
                }
                self.done = end;
                converted = true;
            }
            self.list += 1;
            self.done = 0;
        }
        // Freeing thousands of items takes milliseconds (more when the allocator returns their
        // memory to the system), which the event loop's thread need not spend; a few are freed
        // here, which costs less than handing them over.
        let lists = std::mem::take(&mut self.lists);
        if lists.iter().map(Vec::len).sum::<usize>() > FREED_HERE {
            crate::runtime::drop_elsewhere(lists);
        }
        Ok(Some(std::mem::take(&mut self.built).into_iter().map(|list| list.into_bound(py)).collect()))
    }
}

/// A dict argument holding attribute values (`item`, `key`, `exclusive_start_key`).
#[derive(Debug, Default)]
pub struct PyItem(pub Item);

impl<'a, 'py> FromPyObject<'a, 'py> for PyItem {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        py_to_item(&value, "item").map(Self)
    }
}

/// `value` as a dict: the dict itself, or any other `collections.abc.Mapping` (a
/// `MappingProxyType`, a `UserDict`) copied into one; `None` for anything else.
pub fn as_dict<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
    if let Ok(dict) = value.cast::<PyDict>() {
        return Ok(Some(dict.clone()));
    }
    let py = value.py();
    if !value.is_instance(mapping_type(py)?)? {
        return Ok(None);
    }
    let dict = PyDict::new(py);
    for pair in value.call_method0(intern!(py, "items"))?.try_iter()? {
        let (key, element): (Bound<'py, PyAny>, Bound<'py, PyAny>) = pair?.extract()?;
        dict.set_item(key, element)?;
    }
    Ok(Some(dict))
}

/// A mapping of `str` to `str` (`expression_attribute_names`, `tags`), from any mapping.
#[derive(Debug, Default)]
pub struct PyStrMap(pub BTreeMap<String, String>);

impl<'a, 'py> FromPyObject<'a, 'py> for PyStrMap {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let invalid = || PyTypeError::new_err(format!("expected a mapping of str to str, got {}", type_name(&value)));
        let dict = as_dict(&value)?.ok_or_else(invalid)?;
        let mut map = BTreeMap::new();
        for (key, element) in dict.iter() {
            let (Ok(key), Ok(element)) = (key.cast::<PyString>(), element.cast::<PyString>()) else {
                return Err(invalid());
            };
            map.insert(key.to_str()?.to_owned(), element.to_str()?.to_owned());
        }
        Ok(Self(map))
    }
}

/// `expression_attribute_values`: placeholder to Python value.
#[derive(Debug, Default)]
pub struct PyValues(pub BTreeMap<String, AttributeValue>);

impl<'a, 'py> FromPyObject<'a, 'py> for PyValues {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let dict = as_dict(&value)?.ok_or_else(|| {
            PyTypeError::new_err("expression_attribute_values must be a mapping of placeholder to value")
        })?;
        let mut values = BTreeMap::new();
        for (key, element) in dict.iter() {
            let key: String = key.extract()?;
            let converted = py_to_av(&element, &key)?;
            values.insert(key, converted);
        }
        Ok(Self(values))
    }
}

/// A list of values (`parameters` of PartiQL statements).
#[derive(Debug, Default)]
pub struct PyValueList(pub Vec<AttributeValue>);

impl<'a, 'py> FromPyObject<'a, 'py> for PyValueList {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        // A str is iterable too: one parameter, not one per character.
        if value.is_instance_of::<PyString>() || value.is_instance_of::<PyBytes>() || as_dict(&value)?.is_some() {
            return Err(PyTypeError::new_err(format!(
                "parameters must be a list of values, got {}",
                type_name(&value)
            )));
        }
        value
            .try_iter()?
            .enumerate()
            .map(|(index, element)| to_av(&element?, &ValuePath::Element(&ValuePath::Root("parameters"), index), 1))
            .collect::<PyResult<_>>()
            .map(Self)
    }
}

/// `(attempts, throttles, requests, duration_seconds)`.
pub type Stats = (u32, u32, u32, f64);

/// The stats tuple returned next to every output.
pub fn stats(stats: &OperationStats) -> Stats {
    (stats.attempts, stats.throttles, stats.requests, stats.duration.as_secs_f64())
}

fn capacity_dict<'py>(py: Python<'py>, capacity: &Capacity) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item(intern!(py, "read_capacity_units"), capacity.read_capacity_units)?;
    dict.set_item(intern!(py, "write_capacity_units"), capacity.write_capacity_units)?;
    dict.set_item(intern!(py, "capacity_units"), capacity.capacity_units)?;
    Ok(dict)
}

fn capacity_map<'py>(py: Python<'py>, map: &BTreeMap<String, Capacity>) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (name, capacity) in map {
        dict.set_item(name, capacity_dict(py, capacity)?)?;
    }
    Ok(dict)
}

/// Converts consumed capacity.
pub fn consumed_capacity<'py>(py: Python<'py>, capacity: &ConsumedCapacity) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item(intern!(py, "table_name"), &capacity.table_name)?;
    dict.set_item(intern!(py, "capacity_units"), capacity.capacity_units)?;
    dict.set_item(intern!(py, "read_capacity_units"), capacity.read_capacity_units)?;
    dict.set_item(intern!(py, "write_capacity_units"), capacity.write_capacity_units)?;
    dict.set_item(intern!(py, "table"), capacity.table.as_ref().map(|table| capacity_dict(py, table)).transpose()?)?;
    dict.set_item(intern!(py, "global_secondary_indexes"), capacity_map(py, &capacity.global_secondary_indexes)?)?;
    dict.set_item(intern!(py, "local_secondary_indexes"), capacity_map(py, &capacity.local_secondary_indexes)?)?;
    Ok(dict)
}

/// Converts optional consumed capacity.
pub fn optional_capacity<'py>(py: Python<'py>, capacity: Option<&ConsumedCapacity>) -> PyResult<Bound<'py, PyAny>> {
    match capacity {
        Some(capacity) => Ok(consumed_capacity(py, capacity)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// Converts a list of consumed capacities.
pub fn capacities<'py>(py: Python<'py>, capacities: &[ConsumedCapacity]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for capacity in capacities {
        list.append(consumed_capacity(py, capacity)?)?;
    }
    Ok(list)
}

/// Converts item collection metrics.
pub fn collection_metrics<'py>(
    py: Python<'py>,
    metrics: Option<&ItemCollectionMetrics>,
) -> PyResult<Bound<'py, PyAny>> {
    match metrics {
        Some(metrics) => {
            let dict = PyDict::new(py);
            dict.set_item("item_collection_key", item_to_py(py, &metrics.item_collection_key)?)?;
            dict.set_item("size_estimate_range_gb", metrics.size_estimate_range_gb.clone())?;
            Ok(dict.into_any())
        }
        None => Ok(py.None().into_bound(py)),
    }
}

/// Item collection metrics per table (`BatchWriteItem`, `TransactWriteItems`): a dict of lists.
pub fn collection_metrics_map<'py>(
    py: Python<'py>,
    metrics: &BTreeMap<String, Vec<ItemCollectionMetrics>>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (table, entries) in metrics {
        let list = PyList::empty(py);
        for entry in entries {
            list.append(collection_metrics(py, Some(entry))?)?;
        }
        dict.set_item(table, list)?;
    }
    Ok(dict)
}

/// `(partition_key, sort_key)`.
pub fn key_schema_to_py(schema: &KeySchema) -> (String, Option<String>) {
    (schema.partition_key.clone(), schema.sort_key.clone())
}

fn projection_to_py(projection: &Projection) -> (&'static str, Vec<String>) {
    (projection.projection_type.as_str(), projection.non_key_attributes.clone())
}

fn throughput_to_py(throughput: Option<&ProvisionedThroughput>) -> Option<(i64, i64)> {
    throughput.map(|throughput| (throughput.read_capacity_units, throughput.write_capacity_units))
}

fn index_to_py<'py>(py: Python<'py>, index: &IndexDescription) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("index_name", &index.index_name)?;
    dict.set_item("key_schema", key_schema_to_py(&index.key_schema))?;
    dict.set_item("projection", projection_to_py(&index.projection))?;
    dict.set_item("index_status", index.index_status.map(|status| status.as_str()))?;
    dict.set_item("item_count", index.item_count)?;
    dict.set_item("index_size_bytes", index.index_size_bytes)?;
    dict.set_item("index_arn", &index.index_arn)?;
    dict.set_item("provisioned_throughput", throughput_to_py(index.provisioned_throughput.as_ref()))?;
    dict.set_item("backfilling", index.backfilling)?;
    Ok(dict)
}

/// Converts a table description.
pub fn table_description<'py>(py: Python<'py>, table: &TableDescription) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("table_name", &table.table_name)?;
    dict.set_item("table_arn", &table.table_arn)?;
    dict.set_item("table_id", &table.table_id)?;
    dict.set_item("table_status", table.table_status.as_str())?;
    dict.set_item("creation_date_time", table.creation_date_time)?;
    dict.set_item("key_schema", key_schema_to_py(&table.key_schema))?;
    let definitions: Vec<(String, &str)> = table
        .attribute_definitions
        .iter()
        .map(|definition| (definition.name.clone(), definition.attribute_type.as_str()))
        .collect();
    dict.set_item("attribute_definitions", definitions)?;
    dict.set_item("item_count", table.item_count)?;
    dict.set_item("table_size_bytes", table.table_size_bytes)?;
    dict.set_item("billing_mode", table.billing_mode.as_str())?;
    dict.set_item("provisioned_throughput", throughput_to_py(table.provisioned_throughput.as_ref()))?;
    dict.set_item(
        "on_demand_throughput",
        table.on_demand_throughput.map(|value| (value.max_read_request_units, value.max_write_request_units)),
    )?;
    let globals = PyList::empty(py);
    for index in &table.global_secondary_indexes {
        globals.append(index_to_py(py, index)?)?;
    }
    dict.set_item("global_secondary_indexes", globals)?;
    let locals = PyList::empty(py);
    for index in &table.local_secondary_indexes {
        locals.append(index_to_py(py, index)?)?;
    }
    dict.set_item("local_secondary_indexes", locals)?;
    dict.set_item(
        "stream_specification",
        table.stream_specification.map(|stream| (stream.enabled, stream.view_type.map(|view| view.as_str()))),
    )?;
    dict.set_item("latest_stream_arn", &table.latest_stream_arn)?;
    dict.set_item("latest_stream_label", &table.latest_stream_label)?;
    dict.set_item(
        "sse_description",
        table
            .sse_description
            .as_ref()
            .map(|sse| (sse.status.clone(), sse.sse_type.clone(), sse.kms_master_key_arn.clone())),
    )?;
    dict.set_item("table_class", table.table_class.map(|class| class.as_str()))?;
    dict.set_item("deletion_protection_enabled", table.deletion_protection_enabled)?;
    Ok(dict)
}

/// `(status, attribute_name)`.
pub fn ttl_to_py(ttl: &TimeToLiveDescription) -> (&'static str, Option<String>) {
    (ttl.status.as_str(), ttl.attribute_name.clone())
}

/// Converts backup details.
pub fn backup_details<'py>(py: Python<'py>, details: &BackupDetails) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("backup_arn", &details.backup_arn)?;
    dict.set_item("backup_name", &details.backup_name)?;
    dict.set_item("backup_size_bytes", details.backup_size_bytes)?;
    dict.set_item("backup_status", details.backup_status.as_str())?;
    dict.set_item("backup_type", details.backup_type.as_str())?;
    dict.set_item("backup_creation_date_time", details.backup_creation_date_time)?;
    dict.set_item("backup_expiry_date_time", details.backup_expiry_date_time)?;
    Ok(dict)
}

/// Converts a backup description.
pub fn backup_description<'py>(py: Python<'py>, backup: &BackupDescription) -> PyResult<Bound<'py, PyDict>> {
    let dict = backup_details(py, &backup.details)?;
    dict.set_item("table_name", &backup.table_name)?;
    dict.set_item("table_arn", &backup.table_arn)?;
    dict.set_item("table_id", &backup.table_id)?;
    dict.set_item("key_schema", backup.key_schema.as_ref().map(key_schema_to_py))?;
    dict.set_item("item_count", backup.item_count)?;
    Ok(dict)
}

/// Converts a backup summary.
pub fn backup_summary<'py>(py: Python<'py>, backup: &BackupSummary) -> PyResult<Bound<'py, PyDict>> {
    let dict = backup_details(py, &backup.details)?;
    dict.set_item("table_name", &backup.table_name)?;
    dict.set_item("table_arn", &backup.table_arn)?;
    Ok(dict)
}

/// Converts the continuous backups state.
pub fn continuous_backups<'py>(py: Python<'py>, value: &ContinuousBackupsDescription) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("continuous_backups_status", value.continuous_backups_status.as_str())?;
    dict.set_item("point_in_time_recovery_status", value.point_in_time_recovery_status.as_str())?;
    dict.set_item("recovery_period_in_days", value.recovery_period_in_days)?;
    dict.set_item("earliest_restorable_date_time", value.earliest_restorable_date_time)?;
    dict.set_item("latest_restorable_date_time", value.latest_restorable_date_time)?;
    Ok(dict)
}

/// A write request as `("put", item)` or `("delete", key)`.
pub fn write_request_to_py<'py>(py: Python<'py>, request: &WriteRequest) -> PyResult<Bound<'py, PyTuple>> {
    match request {
        WriteRequest::Put(item) => {
            PyTuple::new(py, [PyString::new(py, "put").into_any(), item_to_py(py, item)?.into_any()])
        }
        WriteRequest::Delete(key) => {
            PyTuple::new(py, [PyString::new(py, "delete").into_any(), item_to_py(py, key)?.into_any()])
        }
    }
}

/// Keys and read settings of one table (`unprocessed_keys`).
pub fn keys_and_attributes<'py>(py: Python<'py>, request: &KeysAndAttributes) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item(intern!(py, "keys"), items_to_py(py, &request.keys)?)?;
    dict.set_item(intern!(py, "consistent_read"), request.consistent_read)?;
    dict.set_item(intern!(py, "projection_expression"), &request.projection_expression)?;
    dict.set_item(intern!(py, "expression_attribute_names"), request.expression_attribute_names.clone())?;
    Ok(dict)
}

/// The keys of DynamoDB's typed JSON shape, looked up once per conversion.
struct WireTags<'py> {
    s: Bound<'py, PyString>,
    n: Bound<'py, PyString>,
    b: Bound<'py, PyString>,
    bool_: Bound<'py, PyString>,
    null: Bound<'py, PyString>,
    l: Bound<'py, PyString>,
    m: Bound<'py, PyString>,
    ss: Bound<'py, PyString>,
    ns: Bound<'py, PyString>,
    bs: Bound<'py, PyString>,
}

impl<'py> WireTags<'py> {
    fn new(py: Python<'py>) -> Self {
        Self {
            s: intern!(py, "S").clone(),
            n: intern!(py, "N").clone(),
            b: intern!(py, "B").clone(),
            bool_: intern!(py, "BOOL").clone(),
            null: intern!(py, "NULL").clone(),
            l: intern!(py, "L").clone(),
            m: intern!(py, "M").clone(),
            ss: intern!(py, "SS").clone(),
            ns: intern!(py, "NS").clone(),
            bs: intern!(py, "BS").clone(),
        }
    }
}

/// Converts values to DynamoDB's typed JSON shape (`{"S": "x"}`, `{"N": "1"}`, ...). Binary
/// values are `bytes`, or base64 text with `base64_binary`.
struct ToWire<'py> {
    py: Python<'py>,
    tags: WireTags<'py>,
    base64_binary: bool,
}

impl<'py> ToWire<'py> {
    fn new(py: Python<'py>, base64_binary: bool) -> Self {
        Self { py, tags: WireTags::new(py), base64_binary }
    }

    /// `{tag: inner}`.
    fn tagged(&self, tag: &Bound<'py, PyString>, inner: impl IntoPyObject<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(self.py);
        dict.set_item(tag, inner)?;
        Ok(dict)
    }

    fn binary(&self, bytes: &[u8]) -> Bound<'py, PyAny> {
        if self.base64_binary {
            PyString::new(self.py, &base64_encode(bytes)).into_any()
        } else {
            PyBytes::new(self.py, bytes).into_any()
        }
    }

    /// A converted attribute value in the typed shape.
    fn value(&self, value: &AttributeValue) -> PyResult<Bound<'py, PyDict>> {
        let py = self.py;
        let tags = &self.tags;
        match value {
            AttributeValue::S(text) => self.tagged(&tags.s, text),
            AttributeValue::N(number) => self.tagged(&tags.n, number.as_str()),
            AttributeValue::B(bytes) => self.tagged(&tags.b, self.binary(bytes)),
            AttributeValue::Bool(flag) => self.tagged(&tags.bool_, *flag),
            AttributeValue::Null => self.tagged(&tags.null, true),
            AttributeValue::L(values) => {
                let list = PyList::empty(py);
                for value in values {
                    list.append(self.value(value)?)?;
                }
                self.tagged(&tags.l, list)
            }
            AttributeValue::M(values) => self.tagged(&tags.m, self.item(values)?),
            AttributeValue::Ss(values) => {
                self.tagged(&tags.ss, PyList::new(py, values.iter().map(|text| PyString::new(py, text)))?)
            }
            AttributeValue::Ns(values) => {
                self.tagged(&tags.ns, PyList::new(py, values.iter().map(|number| PyString::new(py, number.as_str())))?)
            }
            AttributeValue::Bs(values) => {
                self.tagged(&tags.bs, PyList::new(py, values.iter().map(|bytes| self.binary(bytes)))?)
            }
        }
    }

    fn item(&self, item: &Item) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(self.py);
        for (name, value) in item {
            dict.set_item(name, self.value(value)?)?;
        }
        Ok(dict)
    }

    /// A Python value in the typed shape, in one pass for strings, maps and lists, and through
    /// [`to_av`] for the others: the checks, messages and canonical numbers are [`to_av`]'s. The
    /// `str` objects of the value (attribute names, `S` values) are reused.
    fn python(&self, value: &Bound<'py, PyAny>, path: &ValuePath<'_>, depth: usize) -> PyResult<Bound<'py, PyDict>> {
        if depth > MAX_DEPTH {
            return Err(too_deep(path));
        }
        if let Ok(string) = value.cast_exact::<PyString>() {
            check_text(string, path)?;
            return self.tagged(&self.tags.s, string);
        }
        if value.is_instance_of::<PyBool>() || value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
            return self.value(&to_av(value, path, depth)?);
        }
        if let Ok(dict) = value.cast::<PyDict>() {
            return self.tagged(&self.tags.m, self.python_dict(dict, path, depth + 1)?);
        }
        if let Ok(list) = value.cast::<PyList>() {
            let values = PyList::empty(self.py);
            for (index, element) in list.iter().enumerate() {
                values.append(self.python(&element, &ValuePath::Element(path, index), depth + 1)?)?;
            }
            return self.tagged(&self.tags.l, values);
        }
        if let Some(dict) = self.other_mapping(value)? {
            return self.tagged(&self.tags.m, self.python_dict(&dict, path, depth + 1)?);
        }
        self.value(&to_av(value, path, depth)?)
    }

    /// A mapping that is no `dict` (a `MappingProxyType`, a `UserDict`) copied into one, in
    /// its order, when [`to_av`] would take it as a map: it checks the types before `Mapping`
    /// first.
    fn other_mapping(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
        if value.is_none()
            || value.is_instance_of::<PyString>()
            || value.is_instance_of::<PyBytes>()
            || value.is_instance_of::<PyByteArray>()
            || value.is_instance_of::<PyMemoryView>()
            || value.is_instance_of::<PyTuple>()
            || value.is_instance_of::<PySet>()
            || value.is_instance_of::<PyFrozenSet>()
            || value.is_instance(decimal_type(self.py)?)?
        {
            return Ok(None);
        }
        as_dict(value)
    }

    /// The members of a map whose values are `depth` levels deep (see [`dict_to_item`]).
    fn python_dict(
        &self,
        dict: &Bound<'py, PyDict>,
        path: &ValuePath<'_>,
        depth: usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(self.py);
        for (key, value) in dict.iter() {
            let Ok(name) = key.cast::<PyString>() else {
                return Err(ValidationError::new_err(format!(
                    "{path}: attribute names must be str, got {}",
                    type_name(&key)
                )));
            };
            let text = checked_text(name, path)?;
            let converted = self.python(&value, &ValuePath::Member(path, &text), depth)?;
            if name.is_exact_instance_of::<PyString>() {
                out.set_item(name, converted)?;
            } else {
                out.set_item(&*text, converted)?;
            }
        }
        Ok(out)
    }
}

/// A Python value (`path` names it in errors) in DynamoDB's typed JSON shape, checked as
/// [`py_to_av`] checks it.
pub fn py_to_wire<'py>(value: &Bound<'py, PyAny>, path: &str, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    ToWire::new(value.py(), base64_binary).python(value, &ValuePath::Root(path), 1)
}

/// A Python item (or key) in DynamoDB's typed JSON shape, checked as [`py_to_item`] checks it.
pub fn py_item_to_wire<'py>(value: &Bound<'py, PyAny>, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    let convert = ToWire::new(value.py(), base64_binary);
    if let Ok(dict) = value.cast::<PyDict>() {
        return convert.python_dict(dict, &ValuePath::Root(""), 1);
    }
    // Other mappings in their order; members are named `item.<name>` in errors, as py_to_item names them.
    if let Some(dict) = convert.other_mapping(value)? {
        return convert.python_dict(&dict, &ValuePath::Root("item"), 2);
    }
    convert.item(&py_to_item(value, "item")?)
}

/// Whether a `str` is valid Unicode, as [`text`] checks it.
fn check_text(value: &Bound<'_, PyString>, path: &dyn std::fmt::Display) -> PyResult<()> {
    checked_text(value, path).map(|_| ())
}

/// The text of a `str`, borrowed where Python allows it (see [`text`]).
fn checked_text<'a>(
    value: &'a Bound<'_, PyString>,
    path: &dyn std::fmt::Display,
) -> PyResult<std::borrow::Cow<'a, str>> {
    value.to_str().map(std::borrow::Cow::Borrowed).map_err(|_| {
        ValidationError::new_err(format!("{path}: the text is not valid Unicode (it contains a lone surrogate)"))
    })
}

fn wire_binary(value: &Bound<'_, PyAny>, path: &ValuePath<'_>) -> PyResult<Vec<u8>> {
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(bytes.as_bytes().to_vec());
    }
    if let Ok(bytes) = value.cast::<PyByteArray>() {
        return Ok(bytes.to_vec());
    }
    let text: String = value
        .extract()
        .map_err(|_| ValidationError::new_err(format!("{path}: binary values must be bytes or base64 text")))?;
    base64_decode(&text).ok_or_else(|| ValidationError::new_err(format!("{path}: invalid base64 binary value")))
}

/// A binary value of the typed shape as `bytes`: the object itself when it is one.
fn wire_bytes<'py>(value: &Bound<'py, PyAny>, path: &ValuePath<'_>) -> PyResult<Bound<'py, PyAny>> {
    if value.is_exact_instance_of::<PyBytes>() {
        return Ok(value.clone());
    }
    Ok(PyBytes::new(value.py(), &wire_binary(value, path)?).into_any())
}

/// Converts a value in DynamoDB's typed JSON shape to a plain Python value, in one pass.
pub fn wire_value_to_py<'py>(value: &Bound<'py, PyAny>, path: &str) -> PyResult<Bound<'py, PyAny>> {
    wire_to_py(value, &ValuePath::Root(path), 1)
}

/// Converts an item in DynamoDB's typed JSON shape to a `dict` of plain Python values, in one pass.
pub fn wire_item_to_py<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    wire_item(value, &ValuePath::Root(""), 1)
}

/// A typed value `depth` levels deep (see [`MAX_DEPTH`]) as a plain Python value: what the
/// conversion of the parsed attribute value gives (numbers in their canonical form, as `int`
/// or `Decimal`), reusing the `str` and `bytes` objects of the input. Anything that is not a
/// value DynamoDB accepts raises `ValidationError`, with DynamoDB's wording where it has one.
fn wire_to_py<'py>(value: &Bound<'py, PyAny>, path: &ValuePath<'_>, depth: usize) -> PyResult<Bound<'py, PyAny>> {
    if depth > MAX_DEPTH {
        return Err(too_deep(path));
    }
    let py = value.py();
    let invalid = || {
        ValidationError::new_err(format!(
            "{path}: expected a typed DynamoDB value such as {{\"S\": \"text\"}} or {{\"N\": \"1\"}}"
        ))
    };
    let dict = as_dict(value)?.ok_or_else(invalid)?;
    if dict.len() != 1 {
        return Err(invalid());
    }
    let (kind, inner) = dict.iter().next().ok_or_else(invalid)?;
    let kind = kind.cast_into::<PyString>().map_err(|_| invalid())?;
    let kind = kind.to_cow()?;
    let expected =
        |what: &str| ValidationError::new_err(format!("{path}: \"{kind}\" must be {what}, got {}", type_name(&inner)));
    // A text element as a Python `str`: the element itself when it is exactly one.
    let string = |element: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
        let Ok(string) = element.cast::<PyString>() else {
            return Err(expected("text"));
        };
        let text = checked_text(string, path)?;
        Ok(if string.is_exact_instance_of::<PyString>() {
            element.clone()
        } else {
            PyString::new(py, &text).into_any()
        })
    };
    let numeric = |element: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
        let Ok(string) = element.cast::<PyString>() else {
            return Err(expected("text"));
        };
        number_to_py(py, &number(&checked_text(string, path)?, path)?)
    };
    let elements = || -> PyResult<Vec<Bound<'py, PyAny>>> {
        if !(inner.is_instance_of::<PyList>() || inner.is_instance_of::<PyTuple>()) {
            return Err(expected("a list"));
        }
        inner.try_iter()?.collect()
    };
    let set = || -> PyResult<Vec<Bound<'py, PyAny>>> {
        let elements = elements()?;
        if elements.is_empty() {
            // DynamoDB's wording, as the in-memory backend's (`AttributeValue::validate`).
            return Err(ValidationError::new_err(format!(
                "{path}: One or more parameter values were invalid: {}",
                match &*kind {
                    "SS" => "An string set  may not be empty",
                    "NS" => "An number set  may not be empty",
                    _ => "Binary sets should not be empty",
                }
            )));
        }
        Ok(elements)
    };
    Ok(match &*kind {
        "S" => string(&inner)?,
        "N" => numeric(&inner)?,
        "B" => wire_bytes(&inner, path)?,
        "BOOL" => PyBool::new(py, inner.cast::<PyBool>().map_err(|_| expected("true or false"))?.is_true())
            .to_owned()
            .into_any(),
        "NULL" => {
            if !inner.cast::<PyBool>().is_ok_and(|flag| flag.is_true()) {
                return Err(ValidationError::new_err(format!(
                    "{path}: One or more parameter values were invalid: Null attribute value types must have the value of true"
                )));
            }
            py.None().into_bound(py)
        }
        "L" => {
            let list = PyList::empty(py);
            for (index, element) in elements()?.iter().enumerate() {
                list.append(wire_to_py(element, &ValuePath::Element(path, index), depth + 1)?)?;
            }
            list.into_any()
        }
        "M" => wire_item(&inner, path, depth + 1)?.into_any(),
        "SS" => {
            let values = set()?.iter().map(string).collect::<PyResult<Vec<_>>>()?;
            PySet::new(py, values)?.into_any()
        }
        "NS" => {
            let values = set()?.iter().map(numeric).collect::<PyResult<Vec<_>>>()?;
            PySet::new(py, values)?.into_any()
        }
        "BS" => {
            let values = set()?.iter().map(|element| wire_bytes(element, path)).collect::<PyResult<Vec<_>>>()?;
            PySet::new(py, values)?.into_any()
        }
        _ => return Err(invalid()),
    })
}

/// The members of a typed map whose values are `depth` levels deep, as a `dict`.
fn wire_item<'py>(value: &Bound<'py, PyAny>, path: &ValuePath<'_>, depth: usize) -> PyResult<Bound<'py, PyDict>> {
    let what: &dyn std::fmt::Display = if path.is_empty() { &"item" } else { path };
    let dict =
        as_dict(value)?.ok_or_else(|| ValidationError::new_err(format!("{what} must be a mapping of typed values")))?;
    let out = PyDict::new(value.py());
    for (name, value) in dict.iter() {
        let Ok(name) = name.cast::<PyString>() else {
            return Err(ValidationError::new_err(format!(
                "{what}: attribute names must be str, got {}",
                type_name(&name)
            )));
        };
        let text = checked_text(name, what)?;
        let converted = wire_to_py(&value, &ValuePath::Member(path, &text), depth)?;
        if name.is_exact_instance_of::<PyString>() {
            out.set_item(name, converted)?;
        } else {
            out.set_item(&*text, converted)?;
        }
    }
    Ok(out)
}

const BASE64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn base64_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let triple = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for (index, shift) in [18, 12, 6, 0].into_iter().enumerate() {
            if index <= chunk.len() {
                out.push(char::from(BASE64[((triple >> shift) & 0x3f) as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// Strict standard base64: padding (`=`, at most two) only at the end, so `"===="`, `"A==="`
/// and `"AA==AAAA"` are refused rather than read as zero bytes.
fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let clean: Vec<u8> = text.bytes().filter(|byte| !byte.is_ascii_whitespace()).collect();
    if !clean.len().is_multiple_of(4) {
        return None;
    }
    let chunks = clean.len() / 4;
    let mut out = Vec::with_capacity(chunks * 3);
    for (position, chunk) in clean.chunks(4).enumerate() {
        let padded = chunk.iter().filter(|byte| **byte == b'=').count();
        if padded > 2 || (padded > 0 && position + 1 < chunks) {
            return None;
        }
        let mut triple = 0u32;
        let mut padding = 0;
        for &byte in chunk {
            let value = match byte {
                b'=' => {
                    padding += 1;
                    0
                }
                _ if padding > 0 => return None,
                _ => BASE64.iter().position(|candidate| *candidate == byte)? as u32,
            };
            triple = (triple << 6) | value;
        }
        let bytes = [(triple >> 16) as u8, (triple >> 8) as u8, triple as u8];
        out.extend_from_slice(&bytes[..3 - padding.min(2)]);
    }
    Some(out)
}
