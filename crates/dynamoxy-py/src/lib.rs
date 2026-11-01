//! `dynamoxy._core`: the native module behind the `dynamoxy` Python package.
//!
//! A thin binding layer over `dynamoxy-core`: conversions, the asyncio bridge and exceptions.
//! Keep logic in the core crate. Every public item must be reflected in
//! `python/dynamoxy/_core.pyi` (`scripts/check_consistency.py` compares them).

mod client;
mod convert;
mod errors;
mod runtime;

use dynamoxy_core::expr::{ExpressionInput, apply_update, parse_request, reserved, validate_update};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::convert::{
    PyStrMap, PyValues, item_to_py, py_item_to_wire, py_to_item, py_to_wire, wire_item_to_py, wire_value_to_py,
};
use crate::errors::to_py_err;

/// All event kinds as `(name, default level, description)` tuples.
#[pyfunction]
fn event_kinds() -> Vec<(&'static str, &'static str, &'static str)> {
    dynamoxy_core::events::EventKind::ALL
        .iter()
        .map(|kind| (kind.as_str(), kind.default_level().as_str(), kind.description()))
        .collect()
}

/// Every operation name the metrics report (DynamoDB API names).
#[pyfunction]
fn operation_names() -> Vec<&'static str> {
    dynamoxy_core::metrics::Operation::ALL.iter().map(|operation| operation.as_str()).collect()
}

/// Converts an item of plain Python values to DynamoDB's typed JSON shape (`{"a": {"S": "x"}}`).
#[pyfunction]
#[pyo3(signature = (item, *, base64_binary = false))]
fn to_wire<'py>(item: &Bound<'py, PyAny>, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    py_item_to_wire(item, base64_binary)
}

/// Converts an item in DynamoDB's typed JSON shape (boto3 client, DynamoDB Streams, exports) to
/// plain Python values.
#[pyfunction]
fn from_wire<'py>(item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    wire_item_to_py(item)
}

/// Converts one plain value to DynamoDB's typed JSON shape (`{"N": "1.5"}`).
#[pyfunction]
#[pyo3(signature = (value, *, base64_binary = false))]
fn value_to_wire<'py>(value: &Bound<'py, PyAny>, base64_binary: bool) -> PyResult<Bound<'py, PyDict>> {
    py_to_wire(value, "value", base64_binary)
}

/// Converts one value in DynamoDB's typed JSON shape to a plain value.
#[pyfunction]
fn value_from_wire<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    wire_value_to_py(value, "value")
}

/// Whether `name` is a DynamoDB reserved word (it must be written with a `#placeholder`).
#[pyfunction]
fn is_reserved_word(name: &str) -> bool {
    reserved::is_reserved(name)
}

/// Validates expressions with the same parser as the in-memory backend (raises
/// `ValidationError` like DynamoDB would). Used by tests of the expression builder.
#[pyfunction]
#[pyo3(signature = (*, condition = None, filter = None, key_condition = None, update = None, projection = None, names = None, values = None))]
#[allow(clippy::too_many_arguments)]
fn _check_expressions(
    condition: Option<&str>,
    filter: Option<&str>,
    key_condition: Option<&str>,
    update: Option<&str>,
    projection: Option<&str>,
    names: Option<PyStrMap>,
    values: Option<PyValues>,
) -> PyResult<()> {
    let input = ExpressionInput { condition, filter, key_condition, update, projection };
    let parsed = parse_request(
        input,
        &names.map(|names| names.0).unwrap_or_default(),
        &values.map(|values| values.0).unwrap_or_default(),
    )
    .map_err(to_py_err)?;
    // Overlapping or conflicting paths and the operand types of ADD and DELETE, as for a request.
    if let Some(update) = &parsed.update {
        validate_update(update, &[]).map_err(to_py_err)?;
    }
    Ok(())
}

/// Applies an update expression to an item (plain values) the way DynamoDB would, with the
/// in-memory backend's evaluator, and returns the item after the update.
///
/// Transactions return no attributes; `Transaction.commit()` uses this to refresh updated
/// instances from the item as they were loaded.
#[pyfunction]
#[pyo3(signature = (item, update, *, names = None, values = None))]
fn _apply_update<'py>(
    py: Python<'py>,
    item: &Bound<'py, PyAny>,
    update: &str,
    names: Option<PyStrMap>,
    values: Option<PyValues>,
) -> PyResult<Bound<'py, PyDict>> {
    let item = py_to_item(item, "item")?;
    let input = ExpressionInput { update: Some(update), ..ExpressionInput::default() };
    let parsed = parse_request(
        input,
        &names.map(|names| names.0).unwrap_or_default(),
        &values.map(|values| values.0).unwrap_or_default(),
    )
    .map_err(to_py_err)?;
    let Some(update) = parsed.update else {
        return Err(pyo3::exceptions::PyValueError::new_err("an update expression is required"));
    };
    validate_update(&update, &[]).map_err(to_py_err)?;
    item_to_py(py, &apply_update(&update, &item).map_err(to_py_err)?)
}

/// Forgets the runtime inherited from a parent process (registered with `os.register_at_fork`).
#[pyfunction]
fn _reset_after_fork() {
    runtime::reset_after_fork();
}

/// Stops the Tokio runtime before the interpreter finalizes (registered with `atexit`).
///
/// Afterwards every awaitable raises `ShutdownError`. Only for interpreter exit: the runtime
/// cannot be restarted in the same process.
#[pyfunction]
fn _shutdown(py: Python<'_>) {
    runtime::shutdown(py);
}

// The module declares that it needs the GIL: a free-threaded interpreter (3.13t, 3.14t) turns the
// GIL back on when it imports dynamoxy, with a `RuntimeWarning`, because neither this crate nor
// the Python layer has been tested without it (#178). Turning it off needs that testing first.
#[pymodule(gil_used = true)]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    errors::register(m)?;
    m.add_class::<client::PyCoreClient>()?;
    m.add_class::<client::PyMemoryStore>()?;
    m.add_function(wrap_pyfunction!(event_kinds, m)?)?;
    m.add_function(wrap_pyfunction!(operation_names, m)?)?;
    m.add_function(wrap_pyfunction!(to_wire, m)?)?;
    m.add_function(wrap_pyfunction!(from_wire, m)?)?;
    m.add_function(wrap_pyfunction!(value_to_wire, m)?)?;
    m.add_function(wrap_pyfunction!(value_from_wire, m)?)?;
    m.add_function(wrap_pyfunction!(is_reserved_word, m)?)?;
    m.add_function(wrap_pyfunction!(_check_expressions, m)?)?;
    m.add_function(wrap_pyfunction!(_apply_update, m)?)?;
    m.add_function(wrap_pyfunction!(_reset_after_fork, m)?)?;
    let shutdown = wrap_pyfunction!(_shutdown, m)?;
    m.add_function(shutdown.clone())?;
    // atexit runs hooks in reverse order of registration, so hooks that applications register
    // after importing dynamoxy run first and can still use it. See runtime.rs.
    m.py().import("atexit")?.call_method1("register", (shutdown,))?;
    Ok(())
}
