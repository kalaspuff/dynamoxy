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

use std::collections::BTreeMap;

use dynamoxy_core::types::{
    BackupDescription, BackupDetails, BackupSummary, Capacity, ConsumedCapacity, ContinuousBackupsDescription,
    IndexDescription, ItemCollectionMetrics, KeySchema, KeysAndAttributes, Projection, ProvisionedThroughput,
    TableDescription, TimeToLiveDescription, WriteRequest,
};
use dynamoxy_core::{AttributeValue, Item, Number, OperationStats};
use pyo3::exceptions::PyTypeError;
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

fn number(text: &str, path: &str) -> PyResult<Number> {
    Number::parse(text).map_err(|error| ValidationError::new_err(format!("{path}: {error}")))
}

fn type_name(value: &Bound<'_, PyAny>) -> String {
    value.get_type().name().map(|name| name.to_string()).unwrap_or_else(|_| "?".into())
}

/// Converts a Python value to an attribute value. `path` names the value in error messages.
pub fn py_to_av(value: &Bound<'_, PyAny>, path: &str) -> PyResult<AttributeValue> {
    let py = value.py();
    if value.is_none() {
        return Ok(AttributeValue::Null);
    }
    if let Ok(text) = value.cast_exact::<PyString>() {
        return Ok(AttributeValue::S(text.to_str()?.to_owned()));
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(AttributeValue::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(small) = value.extract::<i64>() {
            return Ok(AttributeValue::N(Number::from(small)));
        }
        return Ok(AttributeValue::N(number(&value.str()?.to_cow()?, path)?));
    }
    if let Ok(float) = value.cast::<PyFloat>() {
        let float = float.value();
        if !float.is_finite() {
            return Err(ValidationError::new_err(format!(
                "{path}: DynamoDB numbers cannot be NaN or infinite, got {float}"
            )));
        }
        return Ok(AttributeValue::N(number(&float.to_string(), path)?));
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return Ok(AttributeValue::M(dict_to_item(dict, path)?));
    }
    if let Ok(list) = value.cast::<PyList>() {
        return list
            .iter()
            .enumerate()
            .map(|(index, element)| py_to_av(&element, &format!("{path}[{index}]")))
            .collect::<PyResult<_>>()
            .map(AttributeValue::L);
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(AttributeValue::S(text.to_str()?.to_owned()));
    }
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(AttributeValue::B(bytes.as_bytes().to_vec()));
    }
    if let Ok(bytes) = value.cast::<PyByteArray>() {
        return Ok(AttributeValue::B(bytes.to_vec()));
    }
    if value.is_instance_of::<PyMemoryView>() {
        return Ok(AttributeValue::B(value.call_method0("tobytes")?.cast::<PyBytes>()?.as_bytes().to_vec()));
    }
    if value.is_instance(decimal_type(py)?)? {
        let finite: bool = value.call_method0("is_finite")?.extract()?;
        if !finite {
            return Err(ValidationError::new_err(format!(
                "{path}: DynamoDB numbers cannot be NaN or infinite, got Decimal({})",
                value.str()?
            )));
        }
        return Ok(AttributeValue::N(number(&value.str()?.to_cow()?, path)?));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        return tuple
            .iter()
            .enumerate()
            .map(|(index, element)| py_to_av(&element, &format!("{path}[{index}]")))
            .collect::<PyResult<_>>()
            .map(AttributeValue::L);
    }
    if value.is_instance_of::<PySet>() || value.is_instance_of::<PyFrozenSet>() {
        return set_to_av(value, path);
    }
    if value.is_instance(mapping_type(py)?)? {
        let dict = PyDict::new(py);
        for pair in value.call_method0("items")?.try_iter()? {
            let (key, element): (Bound<'_, PyAny>, Bound<'_, PyAny>) = pair?.extract()?;
            dict.set_item(key, element)?;
        }
        return Ok(AttributeValue::M(dict_to_item(&dict, path)?));
    }
    Err(ValidationError::new_err(format!(
        "{path}: cannot store a value of type {} in DynamoDB; use str, int, float, Decimal, bytes, bool, None, list, tuple, dict or a set of str/numbers/bytes",
        type_name(value)
    )))
}

fn set_to_av(value: &Bound<'_, PyAny>, path: &str) -> PyResult<AttributeValue> {
    let elements: Vec<Bound<'_, PyAny>> = value.try_iter()?.collect::<PyResult<_>>()?;
    if elements.is_empty() {
        return Err(ValidationError::new_err(format!(
            "{path}: DynamoDB does not store empty sets; leave the attribute out (or use None or an empty list)"
        )));
    }
    let converted = elements.iter().map(|element| py_to_av(element, path)).collect::<PyResult<Vec<_>>>()?;
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
                // 1 and 1.0 are the same DynamoDB number: keep the set valid.
                let mut unique: Vec<Number> = Vec::with_capacity(numbers.len());
                for number in numbers {
                    if !unique.contains(&number) {
                        unique.push(number);
                    }
                }
                AttributeValue::Ns(unique)
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

fn mixed_set(path: &str, value: &AttributeValue) -> PyErr {
    ValidationError::new_err(format!(
        "{path}: all elements of a set must have the same type (str, numbers or bytes); found a {} element",
        value.value_type()
    ))
}

fn dict_to_item(dict: &Bound<'_, PyDict>, path: &str) -> PyResult<Item> {
    let mut item = Item::new();
    for (key, value) in dict.iter() {
        let Ok(name) = key.cast::<PyString>() else {
            return Err(ValidationError::new_err(format!(
                "{path}: attribute names must be str, got {}",
                type_name(&key)
            )));
        };
        let name = name.to_str()?;
        let child = if path.is_empty() { name.to_owned() } else { format!("{path}.{name}") };
        item.insert(name.to_owned(), py_to_av(&value, &child)?);
    }
    Ok(item)
}

/// Converts a Python mapping (an item or a key) to an [`Item`].
pub fn py_to_item(value: &Bound<'_, PyAny>, what: &str) -> PyResult<Item> {
    if let Ok(dict) = value.cast::<PyDict>() {
        return dict_to_item(dict, "");
    }
    match py_to_av(value, what)? {
        AttributeValue::M(item) => Ok(item),
        _ => Err(ValidationError::new_err(format!(
            "{what} must be a dict of attribute values, got {}",
            type_name(value)
        ))),
    }
}

/// Converts an attribute value to a Python object.
pub fn av_to_py<'py>(py: Python<'py>, value: &AttributeValue) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        AttributeValue::S(text) => PyString::new(py, text).into_any(),
        AttributeValue::N(number) => number_to_py(py, number)?,
        AttributeValue::B(bytes) => PyBytes::new(py, bytes).into_any(),
        AttributeValue::Bool(flag) => PyBool::new(py, *flag).to_owned().into_any(),
        AttributeValue::Null => py.None().into_bound(py),
        AttributeValue::L(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(av_to_py(py, value)?)?;
            }
            list.into_any()
        }
        AttributeValue::M(values) => item_to_py(py, values)?.into_any(),
        AttributeValue::Ss(values) => PySet::new(py, values)?.into_any(),
        AttributeValue::Ns(values) => {
            let set = PySet::empty(py)?;
            for number in values {
                set.add(number_to_py(py, number)?)?;
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
    let dict = PyDict::new(py);
    for (name, value) in item {
        dict.set_item(name, av_to_py(py, value)?)?;
    }
    Ok(dict)
}

/// Converts an optional item.
pub fn optional_item<'py>(py: Python<'py>, item: Option<&Item>) -> PyResult<Bound<'py, PyAny>> {
    match item {
        Some(item) => Ok(item_to_py(py, item)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// Converts a list of items.
pub fn items_to_py<'py>(py: Python<'py>, items: &[Item]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for item in items {
        list.append(item_to_py(py, item)?)?;
    }
    Ok(list)
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

/// `expression_attribute_values`: placeholder to Python value.
#[derive(Debug, Default)]
pub struct PyValues(pub BTreeMap<String, AttributeValue>);

impl<'a, 'py> FromPyObject<'a, 'py> for PyValues {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let dict = value
            .cast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("expression_attribute_values must be a dict of placeholder to value"))?;
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
        value
            .try_iter()?
            .enumerate()
            .map(|(index, element)| py_to_av(&element?, &format!("parameters[{index}]")))
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
    dict.set_item("read_capacity_units", capacity.read_capacity_units)?;
    dict.set_item("write_capacity_units", capacity.write_capacity_units)?;
    dict.set_item("capacity_units", capacity.capacity_units)?;
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
    dict.set_item("table_name", &capacity.table_name)?;
    dict.set_item("capacity_units", capacity.capacity_units)?;
    dict.set_item("read_capacity_units", capacity.read_capacity_units)?;
    dict.set_item("write_capacity_units", capacity.write_capacity_units)?;
    dict.set_item("table", capacity.table.as_ref().map(|table| capacity_dict(py, table)).transpose()?)?;
    dict.set_item("global_secondary_indexes", capacity_map(py, &capacity.global_secondary_indexes)?)?;
    dict.set_item("local_secondary_indexes", capacity_map(py, &capacity.local_secondary_indexes)?)?;
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
    dict.set_item("keys", items_to_py(py, &request.keys)?)?;
    dict.set_item("consistent_read", request.consistent_read)?;
    dict.set_item("projection_expression", &request.projection_expression)?;
    dict.set_item("expression_attribute_names", request.expression_attribute_names.clone())?;
    Ok(dict)
}

/// An attribute value in DynamoDB's typed JSON shape (`{"S": "x"}`, `{"N": "1"}`, ...).
/// Binary values are `bytes`, or base64 text with `base64_binary`.
pub fn av_to_wire<'py>(py: Python<'py>, value: &AttributeValue, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    let binary = |bytes: &[u8]| -> PyResult<Bound<'py, PyAny>> {
        if base64_binary {
            Ok(PyString::new(py, &base64_encode(bytes)).into_any())
        } else {
            Ok(PyBytes::new(py, bytes).into_any())
        }
    };
    match value {
        AttributeValue::S(text) => dict.set_item("S", text)?,
        AttributeValue::N(number) => dict.set_item("N", number.as_str())?,
        AttributeValue::B(bytes) => dict.set_item("B", binary(bytes)?)?,
        AttributeValue::Bool(flag) => dict.set_item("BOOL", *flag)?,
        AttributeValue::Null => dict.set_item("NULL", true)?,
        AttributeValue::L(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(av_to_wire(py, value, base64_binary)?)?;
            }
            dict.set_item("L", list)?;
        }
        AttributeValue::M(values) => dict.set_item("M", item_to_wire(py, values, base64_binary)?)?,
        AttributeValue::Ss(values) => dict.set_item("SS", values.clone())?,
        AttributeValue::Ns(values) => dict.set_item("NS", values.iter().map(Number::as_str).collect::<Vec<_>>())?,
        AttributeValue::Bs(values) => {
            let list = PyList::empty(py);
            for bytes in values {
                list.append(binary(bytes)?)?;
            }
            dict.set_item("BS", list)?;
        }
    }
    Ok(dict)
}

/// An item in DynamoDB's typed JSON shape.
pub fn item_to_wire<'py>(py: Python<'py>, item: &Item, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (name, value) in item {
        dict.set_item(name, av_to_wire(py, value, base64_binary)?)?;
    }
    Ok(dict)
}

fn wire_binary(value: &Bound<'_, PyAny>, path: &str) -> PyResult<Vec<u8>> {
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

/// Parses an attribute value in DynamoDB's typed JSON shape.
pub fn av_from_wire(value: &Bound<'_, PyAny>, path: &str) -> PyResult<AttributeValue> {
    let invalid = || {
        ValidationError::new_err(format!(
            "{path}: expected a typed DynamoDB value such as {{\"S\": \"text\"}} or {{\"N\": \"1\"}}"
        ))
    };
    let dict = value.cast::<PyDict>().map_err(|_| invalid())?;
    if dict.len() != 1 {
        return Err(invalid());
    }
    let (kind, inner) = dict.iter().next().ok_or_else(invalid)?;
    let kind: String = kind.extract()?;
    Ok(match kind.as_str() {
        "S" => AttributeValue::S(inner.extract()?),
        "N" => AttributeValue::N(number(&inner.extract::<String>()?, path)?),
        "B" => AttributeValue::B(wire_binary(&inner, path)?),
        "BOOL" => AttributeValue::Bool(inner.extract()?),
        "NULL" => AttributeValue::Null,
        "L" => AttributeValue::L(
            inner
                .try_iter()?
                .enumerate()
                .map(|(index, element)| av_from_wire(&element?, &format!("{path}[{index}]")))
                .collect::<PyResult<_>>()?,
        ),
        "M" => AttributeValue::M(item_from_wire(&inner, path)?),
        "SS" => AttributeValue::Ss(inner.extract()?),
        "NS" => AttributeValue::Ns(
            inner.extract::<Vec<String>>()?.iter().map(|text| number(text, path)).collect::<PyResult<_>>()?,
        ),
        "BS" => {
            AttributeValue::Bs(inner.try_iter()?.map(|element| wire_binary(&element?, path)).collect::<PyResult<_>>()?)
        }
        _ => return Err(invalid()),
    })
}

/// Parses an item in DynamoDB's typed JSON shape.
pub fn item_from_wire(value: &Bound<'_, PyAny>, path: &str) -> PyResult<Item> {
    let dict = value.cast::<PyDict>().map_err(|_| {
        ValidationError::new_err(format!(
            "{} must be a dict of typed values",
            if path.is_empty() { "item" } else { path }
        ))
    })?;
    let mut item = Item::new();
    for (name, value) in dict.iter() {
        let name: String = name.extract()?;
        let child = if path.is_empty() { name.clone() } else { format!("{path}.{name}") };
        item.insert(name, av_from_wire(&value, &child)?);
    }
    Ok(item)
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

fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let clean: Vec<u8> = text.bytes().filter(|byte| !byte.is_ascii_whitespace()).collect();
    if !clean.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::with_capacity(clean.len() / 4 * 3);
    for chunk in clean.chunks(4) {
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
