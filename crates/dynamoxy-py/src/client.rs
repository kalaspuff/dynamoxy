//! `CoreClient`: the native half of `dynamoxy.Client`, and `MemoryStore`, the in-memory DynamoDB.
//!
//! Every operation returns an awaitable resolving to `(output, stats)`: `output` is a dict shaped
//! like the DynamoDB response (snake_case keys, plain Python values) and `stats` is
//! `(attempts, throttles, requests, duration_seconds)`. Errors carry `.attempts` and `.duration`.

use std::collections::BTreeMap;
use std::future::Future;
use std::time::Duration;

use dynamoxy_core::backend::memory::{Fault, FaultKind, MemoryBackend};
use dynamoxy_core::events::{Event, EventKind};
use dynamoxy_core::metrics::LATENCY_BUCKETS_MS;
use dynamoxy_core::types::*;
use dynamoxy_core::{Call, Client, ClientConfig};
use pyo3::IntoPyObjectExt;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use pythonize::depythonize;

use crate::convert::{
    PyItem, PyValueList, PyValues, backup_description, backup_details, backup_summary, capacities, collection_metrics,
    collection_metrics_map, continuous_backups, items_to_py, keys_and_attributes, optional_capacity, optional_item,
    py_to_item, stats, table_description, ttl_to_py, write_request_to_py,
};
use crate::errors::{ConfigurationError, ShutdownError, ValidationError, call_error_in, error_in, to_py_err};
use crate::runtime::{Deferred, future_into_py, future_into_py_in, future_into_py_inline};

/// Parses a DynamoDB string constant (`"ALL_OLD"`, ...), naming the allowed values on error.
macro_rules! constant {
    ($value:expr, $type:ty, $name:literal) => {{
        let value: &str = $value;
        <$type>::parse(value).ok_or_else(|| {
            let allowed: Vec<&str> = <$type>::ALL.iter().map(|constant| constant.as_str()).collect();
            ValidationError::new_err(format!("{} must be one of {}, got {:?}", $name, allowed.join(", "), value))
        })
    }};
}

fn optional_constant<T>(value: Option<&str>, parse: impl FnOnce(&str) -> PyResult<T>) -> PyResult<Option<T>> {
    value.map(parse).transpose()
}

fn duration(seconds: f64, name: &str) -> PyResult<Duration> {
    Duration::try_from_secs_f64(seconds).map_err(|_| {
        ValidationError::new_err(format!("{name} must be a non-negative number of seconds, got {seconds}"))
    })
}

fn names(names: Option<BTreeMap<String, String>>) -> BTreeMap<String, String> {
    names.unwrap_or_default()
}

fn values(values: Option<PyValues>) -> BTreeMap<String, dynamoxy_core::AttributeValue> {
    values.map(|values| values.0).unwrap_or_default()
}

/// The outcome of a finished call: `(output, stats)` or the exception (with `.attempts`,
/// `.duration`), converted to Python on the event loop's thread.
fn finish<T: Send + 'static>(
    call: Call<T>,
    convert: impl for<'py> FnOnce(Python<'py>, T) -> PyResult<Py<PyAny>> + Send + 'static,
) -> Deferred {
    let call_stats = call.stats;
    Deferred::new(move |py| match call.result {
        Ok(output) => (convert(py, output)?, stats(&call_stats)).into_py_any(py),
        Err(error) => Err(call_error_in(py, error, &call_stats)),
    })
}

/// A dict of output fields (the keys are interned: `intern!(py, "items")`).
fn dict<'py>(py: Python<'py>, entries: &[(&Bound<'py, PyString>, Bound<'py, PyAny>)]) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    for (key, value) in entries {
        dict.set_item(*key, value)?;
    }
    Ok(dict.into_any().unbind())
}

fn write_output(py: Python<'_>, output: WriteItemOutput) -> PyResult<Py<PyAny>> {
    dict(
        py,
        &[
            (intern!(py, "attributes"), optional_item(py, output.attributes.as_ref())?),
            (intern!(py, "consumed_capacity"), optional_capacity(py, output.consumed_capacity.as_ref())?),
            (intern!(py, "item_collection_metrics"), collection_metrics(py, output.item_collection_metrics.as_ref())?),
        ],
    )
}

fn query_output(py: Python<'_>, output: QueryOutput) -> PyResult<Py<PyAny>> {
    dict(
        py,
        &[
            (intern!(py, "items"), items_to_py(py, &output.items)?.into_any()),
            (intern!(py, "count"), output.count.into_pyobject(py)?.into_any()),
            (intern!(py, "scanned_count"), output.scanned_count.into_pyobject(py)?.into_any()),
            (intern!(py, "last_evaluated_key"), optional_item(py, output.last_evaluated_key.as_ref())?),
            (intern!(py, "consumed_capacity"), optional_capacity(py, output.consumed_capacity.as_ref())?),
        ],
    )
}

fn table_output(py: Python<'_>, output: TableDescription) -> PyResult<Py<PyAny>> {
    Ok(table_description(py, &output)?.into_any().unbind())
}

fn key_schema(value: (String, Option<String>)) -> KeySchema {
    KeySchema { partition_key: value.0, sort_key: value.1 }
}

fn projection(value: (String, Vec<String>)) -> PyResult<Projection> {
    Ok(Projection {
        projection_type: constant!(&value.0, ProjectionType, "projection type")?,
        non_key_attributes: value.1,
    })
}

fn throughput(value: Option<(i64, i64)>) -> Option<ProvisionedThroughput> {
    value.map(|(read, write)| ProvisionedThroughput { read_capacity_units: read, write_capacity_units: write })
}

fn on_demand(value: Option<(Option<i64>, Option<i64>)>) -> Option<OnDemandThroughput> {
    value.map(|(read, write)| OnDemandThroughput { max_read_request_units: read, max_write_request_units: write })
}

fn stream(value: Option<(bool, Option<String>)>) -> PyResult<Option<StreamSpecification>> {
    value
        .map(|(enabled, view)| {
            Ok(StreamSpecification {
                enabled,
                view_type: optional_constant(view.as_deref(), |view| {
                    constant!(view, StreamViewType, "stream view type")
                })?,
            })
        })
        .transpose()
}

fn sse(value: Option<(bool, Option<String>, Option<String>)>) -> Option<SseSpecification> {
    value.map(|(enabled, sse_type, kms_master_key_id)| SseSpecification { enabled, sse_type, kms_master_key_id })
}

/// A global secondary index given as a dict (`index_name`, `key_schema`, `projection`, ...).
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct GlobalIndexArg {
    index_name: String,
    key_schema: (String, Option<String>),
    projection: (String, Vec<String>),
    #[pyo3(default)]
    provisioned_throughput: Option<(i64, i64)>,
    #[pyo3(default)]
    on_demand_throughput: Option<(Option<i64>, Option<i64>)>,
}

impl GlobalIndexArg {
    fn into_core(self) -> PyResult<GlobalSecondaryIndex> {
        Ok(GlobalSecondaryIndex {
            index_name: self.index_name,
            key_schema: key_schema(self.key_schema),
            projection: projection(self.projection)?,
            provisioned_throughput: throughput(self.provisioned_throughput),
            on_demand_throughput: on_demand(self.on_demand_throughput),
        })
    }
}

/// A local secondary index given as a dict.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct LocalIndexArg {
    index_name: String,
    key_schema: (String, Option<String>),
    projection: (String, Vec<String>),
}

/// One statement of `batch_execute_statement` / `execute_transaction`.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct StatementArg {
    statement: String,
    #[pyo3(default)]
    parameters: Option<PyValueList>,
    #[pyo3(default)]
    consistent_read: Option<bool>,
    #[pyo3(default)]
    return_values_on_condition_check_failure: Option<String>,
}

impl StatementArg {
    fn into_core(self) -> PyResult<ParameterizedStatement> {
        Ok(ParameterizedStatement {
            statement: self.statement,
            parameters: self.parameters.map(|parameters| parameters.0).unwrap_or_default(),
            consistent_read: self.consistent_read.unwrap_or(false),
            return_values_on_condition_check_failure: optional_constant(
                self.return_values_on_condition_check_failure.as_deref(),
                |value| {
                    constant!(value, ReturnValuesOnConditionCheckFailure, "return_values_on_condition_check_failure")
                },
            )?
            .unwrap_or_default(),
        })
    }
}

/// One read of `transact_get_items`.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct TransactGetArg {
    table_name: String,
    key: PyItem,
    #[pyo3(default)]
    projection_expression: Option<String>,
    #[pyo3(default)]
    expression_attribute_names: Option<BTreeMap<String, String>>,
}

/// One operation of `transact_write_items` (`kind` is put, update, delete or condition_check).
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct TransactWriteArg {
    kind: String,
    table_name: String,
    #[pyo3(default)]
    item: Option<PyItem>,
    #[pyo3(default)]
    key: Option<PyItem>,
    #[pyo3(default)]
    update_expression: Option<String>,
    #[pyo3(default)]
    condition_expression: Option<String>,
    #[pyo3(default)]
    expression_attribute_names: Option<BTreeMap<String, String>>,
    #[pyo3(default)]
    expression_attribute_values: Option<PyValues>,
    #[pyo3(default)]
    return_values_on_condition_check_failure: Option<String>,
}

impl TransactWriteArg {
    fn into_core(self, position: usize) -> PyResult<TransactWriteItem> {
        let target = TransactTarget {
            table_name: self.table_name,
            condition_expression: self.condition_expression,
            expression_attribute_names: names(self.expression_attribute_names),
            expression_attribute_values: values(self.expression_attribute_values),
            return_values_on_condition_check_failure: optional_constant(
                self.return_values_on_condition_check_failure.as_deref(),
                |value| {
                    constant!(value, ReturnValuesOnConditionCheckFailure, "return_values_on_condition_check_failure")
                },
            )?
            .unwrap_or_default(),
        };
        let missing = |field: &str| {
            ValidationError::new_err(format!("transaction operation {position} ({}) needs `{field}`", self.kind))
        };
        Ok(match self.kind.as_str() {
            "put" => TransactWriteItem::Put { target, item: self.item.ok_or_else(|| missing("item"))?.0 },
            "delete" => TransactWriteItem::Delete { target, key: self.key.ok_or_else(|| missing("key"))?.0 },
            "condition_check" => {
                TransactWriteItem::ConditionCheck { target, key: self.key.ok_or_else(|| missing("key"))?.0 }
            }
            "update" => TransactWriteItem::Update {
                key: self.key.ok_or_else(|| missing("key"))?.0,
                update_expression: self.update_expression.clone().ok_or_else(|| missing("update_expression"))?,
                target,
            },
            other => {
                return Err(ValidationError::new_err(format!(
                    "transaction operation {position}: kind must be put, update, delete or condition_check, got {other:?}"
                )));
            }
        })
    }
}

/// Keys of one table for `batch_get_item` / `batch_get`.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct KeysArg {
    keys: Vec<PyItem>,
    #[pyo3(default)]
    consistent_read: Option<bool>,
    #[pyo3(default)]
    projection_expression: Option<String>,
    #[pyo3(default)]
    expression_attribute_names: Option<BTreeMap<String, String>>,
}

impl KeysArg {
    fn into_core(self) -> KeysAndAttributes {
        KeysAndAttributes {
            keys: self.keys.into_iter().map(|key| key.0).collect(),
            consistent_read: self.consistent_read.unwrap_or(false),
            projection_expression: self.projection_expression,
            expression_attribute_names: names(self.expression_attribute_names),
        }
    }
}

fn write_request(kind: &str, item: &Bound<'_, PyAny>) -> PyResult<WriteRequest> {
    match kind {
        "put" => Ok(WriteRequest::Put(py_to_item(item, "item")?)),
        "delete" => Ok(WriteRequest::Delete(py_to_item(item, "key")?)),
        other => {
            Err(ValidationError::new_err(format!("batch write requests are \"put\" or \"delete\", got {other:?}")))
        }
    }
}

fn batch_get_output(py: Python<'_>, output: BatchGetItemOutput) -> PyResult<Py<PyAny>> {
    let responses = PyDict::new(py);
    for (table, items) in &output.responses {
        responses.set_item(table, items_to_py(py, items)?)?;
    }
    let unprocessed = PyDict::new(py);
    for (table, keys) in &output.unprocessed_keys {
        unprocessed.set_item(table, keys_and_attributes(py, keys)?)?;
    }
    dict(
        py,
        &[
            (intern!(py, "responses"), responses.into_any()),
            (intern!(py, "unprocessed_keys"), unprocessed.into_any()),
            (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
        ],
    )
}

fn events_to_py(py: Python<'_>, events: Vec<Event>) -> PyResult<Bound<'_, PyList>> {
    let list = PyList::empty(py);
    for event in events {
        let fields = PyDict::new(py);
        for (name, value) in &event.fields {
            fields.set_item(name, value)?;
        }
        list.append((event.kind.as_str(), event.message, fields))?;
    }
    Ok(list)
}

/// The in-memory DynamoDB behind `memory` clients. Clients created with the same store share
/// their tables; the store also controls test faults and Time to Live deletion.
#[pyclass(name = "MemoryStore", module = "dynamoxy._core", frozen)]
pub struct PyMemoryStore {
    backend: MemoryBackend,
}

#[pymethods]
impl PyMemoryStore {
    /// An empty store. `region` and `account_id` appear in ARNs.
    #[new]
    #[pyo3(signature = (*, region = "us-east-1", account_id = "000000000000"))]
    fn new(region: &str, account_id: &str) -> Self {
        Self { backend: MemoryBackend::new(region, account_id) }
    }

    /// Makes the next `times` matching calls fail, return unprocessed batch items, wait, or run
    /// and lose their response.
    #[pyo3(signature = (*, operation = None, table = None, error_code = None, message = None, unprocessed = false, latency = None, lose_response = false, times = 1))]
    #[allow(clippy::too_many_arguments)]
    fn inject_fault(
        &self,
        operation: Option<String>,
        table: Option<String>,
        error_code: Option<String>,
        message: Option<String>,
        unprocessed: bool,
        latency: Option<f64>,
        lose_response: bool,
        times: u32,
    ) -> PyResult<()> {
        let kind = match (error_code, unprocessed, latency, lose_response) {
            (Some(code), false, None, false) => {
                FaultKind::Error { message: message.unwrap_or_else(|| format!("injected {code}")), code }
            }
            (None, true, None, false) => FaultKind::Unprocessed,
            (None, false, Some(seconds), false) => FaultKind::Latency(duration(seconds, "latency")?),
            (None, false, None, true) => FaultKind::LoseResponse,
            _ => {
                return Err(ValidationError::new_err(
                    "inject_fault needs exactly one of error_code=..., unprocessed=True, latency=... or lose_response=True",
                ));
            }
        };
        self.backend.inject_fault(Fault { operation, table, kind, times });
        Ok(())
    }

    /// Removes every pending fault.
    fn clear_faults(&self) {
        self.backend.clear_faults();
    }

    /// Deletes items whose TTL attribute is at or before `at` (seconds since the epoch; default
    /// now) in tables with TTL enabled. Returns how many were deleted.
    #[pyo3(signature = (at = None))]
    fn purge_expired(&self, py: Python<'_>, at: Option<f64>) -> usize {
        py.detach(|| self.backend.purge_expired(at))
    }

    /// Deletes every table and backup.
    fn reset(&self) {
        self.backend.reset();
    }
}

#[pyclass(name = "CoreClient", module = "dynamoxy._core", frozen)]
pub struct PyCoreClient {
    client: Client,
    /// The process that created the client (a fork() child must create its own).
    pid: u32,
    /// Poll calls once on the calling thread before handing them to the runtime (in-memory
    /// backend: most calls finish without a thread hop).
    inline: bool,
}

impl PyCoreClient {
    fn client(&self) -> PyResult<Client> {
        if std::process::id() != self.pid {
            return Err(ShutdownError::new_err(
                "this dynamoxy client was created before fork(); create a new Client in the child process",
            ));
        }
        Ok(self.client.clone())
    }

    /// An awaitable for a call's outcome (after `client()` checked that this is the client's
    /// process).
    fn run<'py, F>(&self, py: Python<'py>, fut: F) -> PyResult<Bound<'py, PyAny>>
    where
        F: Future<Output = Deferred> + Send + 'static,
    {
        if self.inline { future_into_py_inline(py, self.pid, fut) } else { future_into_py_in(py, self.pid, fut) }
    }
}

fn parse_config(config: &Bound<'_, PyAny>) -> PyResult<ClientConfig> {
    depythonize(config).map_err(|err| ConfigurationError::new_err(format!("invalid client configuration: {err}")))
}

#[pymethods]
impl PyCoreClient {
    /// Creates a client backed by the in-memory DynamoDB (`store`, or a new one).
    #[staticmethod]
    #[pyo3(signature = (config, store = None))]
    fn memory(config: &Bound<'_, PyAny>, store: Option<&PyMemoryStore>) -> PyResult<Self> {
        let config = parse_config(config)?;
        let client = Client::memory(config, store.map(|store| store.backend.clone())).map_err(to_py_err)?;
        Ok(Self { client, pid: std::process::id(), inline: true })
    }

    /// Connects to DynamoDB through the AWS SDK (awaitable, returns a `CoreClient`).
    #[staticmethod]
    fn connect<'py>(py: Python<'py>, config: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let config = parse_config(config)?;
        config.validate().map_err(to_py_err)?;
        future_into_py(py, async move {
            match Client::connect(config).await {
                Ok(client) => Deferred::value(PyCoreClient { client, pid: std::process::id(), inline: false }),
                Err(error) => Deferred::new(move |py| Err(error_in(py, error))),
            }
        })
    }

    /// `"memory"` or `"aws"`.
    #[getter]
    fn backend(&self) -> &'static str {
        self.client.backend_name()
    }

    /// The in-memory store (`None` for AWS clients).
    #[getter]
    fn store(&self) -> Option<PyMemoryStore> {
        self.client.memory_backend().map(|backend| PyMemoryStore { backend: backend.clone() })
    }

    /// Refuses new calls from now on (calls in progress finish, retries included).
    fn close(&self) {
        self.client.close();
    }

    /// Whether `close()` was called.
    #[getter]
    fn closed(&self) -> bool {
        self.client.is_closed()
    }

    /// `GetItem`.
    #[pyo3(signature = (table_name, key, *, consistent_read = false, projection_expression = None, expression_attribute_names = None, return_consumed_capacity = "NONE"))]
    #[allow(clippy::too_many_arguments)]
    fn get_item<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        key: PyItem,
        consistent_read: bool,
        projection_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = GetItemInput {
            table_name,
            key: key.0,
            consistent_read,
            projection_expression,
            expression_attribute_names: names(expression_attribute_names),
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move {
            finish(client.get_item(input).await, |py, output| {
                dict(
                    py,
                    &[
                        (intern!(py, "item"), optional_item(py, output.item.as_ref())?),
                        (intern!(py, "consumed_capacity"), optional_capacity(py, output.consumed_capacity.as_ref())?),
                    ],
                )
            })
        })
    }

    /// `PutItem`.
    #[pyo3(signature = (table_name, item, *, condition_expression = None, expression_attribute_names = None, expression_attribute_values = None, return_values = "NONE", return_values_on_condition_check_failure = "NONE", return_consumed_capacity = "NONE", return_item_collection_metrics = false))]
    #[allow(clippy::too_many_arguments)]
    fn put_item<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        item: PyItem,
        condition_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        expression_attribute_values: Option<PyValues>,
        return_values: &str,
        return_values_on_condition_check_failure: &str,
        return_consumed_capacity: &str,
        return_item_collection_metrics: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = PutItemInput {
            table_name,
            item: item.0,
            condition_expression,
            expression_attribute_names: names(expression_attribute_names),
            expression_attribute_values: values(expression_attribute_values),
            return_values: constant!(return_values, ReturnValues, "return_values")?,
            return_values_on_condition_check_failure: constant!(
                return_values_on_condition_check_failure,
                ReturnValuesOnConditionCheckFailure,
                "return_values_on_condition_check_failure"
            )?,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_item_collection_metrics,
        };
        self.run(py, async move { finish(client.put_item(input).await, write_output) })
    }

    /// `UpdateItem`.
    #[pyo3(signature = (table_name, key, *, update_expression = None, condition_expression = None, expression_attribute_names = None, expression_attribute_values = None, return_values = "NONE", return_values_on_condition_check_failure = "NONE", return_consumed_capacity = "NONE", return_item_collection_metrics = false))]
    #[allow(clippy::too_many_arguments)]
    fn update_item<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        key: PyItem,
        update_expression: Option<String>,
        condition_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        expression_attribute_values: Option<PyValues>,
        return_values: &str,
        return_values_on_condition_check_failure: &str,
        return_consumed_capacity: &str,
        return_item_collection_metrics: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = UpdateItemInput {
            table_name,
            key: key.0,
            update_expression,
            condition_expression,
            expression_attribute_names: names(expression_attribute_names),
            expression_attribute_values: values(expression_attribute_values),
            return_values: constant!(return_values, ReturnValues, "return_values")?,
            return_values_on_condition_check_failure: constant!(
                return_values_on_condition_check_failure,
                ReturnValuesOnConditionCheckFailure,
                "return_values_on_condition_check_failure"
            )?,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_item_collection_metrics,
        };
        self.run(py, async move { finish(client.update_item(input).await, write_output) })
    }

    /// `DeleteItem`.
    #[pyo3(signature = (table_name, key, *, condition_expression = None, expression_attribute_names = None, expression_attribute_values = None, return_values = "NONE", return_values_on_condition_check_failure = "NONE", return_consumed_capacity = "NONE", return_item_collection_metrics = false))]
    #[allow(clippy::too_many_arguments)]
    fn delete_item<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        key: PyItem,
        condition_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        expression_attribute_values: Option<PyValues>,
        return_values: &str,
        return_values_on_condition_check_failure: &str,
        return_consumed_capacity: &str,
        return_item_collection_metrics: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = DeleteItemInput {
            table_name,
            key: key.0,
            condition_expression,
            expression_attribute_names: names(expression_attribute_names),
            expression_attribute_values: values(expression_attribute_values),
            return_values: constant!(return_values, ReturnValues, "return_values")?,
            return_values_on_condition_check_failure: constant!(
                return_values_on_condition_check_failure,
                ReturnValuesOnConditionCheckFailure,
                "return_values_on_condition_check_failure"
            )?,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_item_collection_metrics,
        };
        self.run(py, async move { finish(client.delete_item(input).await, write_output) })
    }

    /// `Query` (one page).
    #[pyo3(signature = (table_name, *, key_condition_expression, index_name = None, filter_expression = None, projection_expression = None, expression_attribute_names = None, expression_attribute_values = None, select = None, limit = None, consistent_read = false, scan_index_forward = true, exclusive_start_key = None, return_consumed_capacity = "NONE"))]
    #[allow(clippy::too_many_arguments)]
    fn query<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        key_condition_expression: String,
        index_name: Option<String>,
        filter_expression: Option<String>,
        projection_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        expression_attribute_values: Option<PyValues>,
        select: Option<&str>,
        limit: Option<u32>,
        consistent_read: bool,
        scan_index_forward: bool,
        exclusive_start_key: Option<PyItem>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = QueryInput {
            table_name,
            index_name,
            key_condition_expression: Some(key_condition_expression),
            filter_expression,
            projection_expression,
            expression_attribute_names: names(expression_attribute_names),
            expression_attribute_values: values(expression_attribute_values),
            select: optional_constant(select, |value| constant!(value, Select, "select"))?,
            limit,
            consistent_read,
            scan_index_forward,
            exclusive_start_key: exclusive_start_key.map(|key| key.0),
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move { finish(client.query(input).await, query_output) })
    }

    /// `Scan` (one page).
    #[pyo3(signature = (table_name, *, index_name = None, filter_expression = None, projection_expression = None, expression_attribute_names = None, expression_attribute_values = None, select = None, limit = None, consistent_read = false, exclusive_start_key = None, segment = None, total_segments = None, return_consumed_capacity = "NONE"))]
    #[allow(clippy::too_many_arguments)]
    fn scan<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        index_name: Option<String>,
        filter_expression: Option<String>,
        projection_expression: Option<String>,
        expression_attribute_names: Option<BTreeMap<String, String>>,
        expression_attribute_values: Option<PyValues>,
        select: Option<&str>,
        limit: Option<u32>,
        consistent_read: bool,
        exclusive_start_key: Option<PyItem>,
        segment: Option<u32>,
        total_segments: Option<u32>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = ScanInput {
            table_name,
            index_name,
            filter_expression,
            projection_expression,
            expression_attribute_names: names(expression_attribute_names),
            expression_attribute_values: values(expression_attribute_values),
            select: optional_constant(select, |value| constant!(value, Select, "select"))?,
            limit,
            consistent_read,
            exclusive_start_key: exclusive_start_key.map(|key| key.0),
            segment,
            total_segments,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move { finish(client.scan(input).await, query_output) })
    }

    /// `BatchGetItem`: one request (at most 100 keys). See `batch_get` for any number of keys.
    #[pyo3(signature = (request_items, *, return_consumed_capacity = "NONE"))]
    fn batch_get_item<'py>(
        &self,
        py: Python<'py>,
        request_items: BTreeMap<String, KeysArg>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = BatchGetItemInput {
            request_items: request_items.into_iter().map(|(table, keys)| (table, keys.into_core())).collect(),
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move { finish(client.batch_get_item(input).await, batch_get_output) })
    }

    /// `BatchWriteItem`: one request (at most 25 writes) of `("put", item)` / `("delete", key)`
    /// per table. See `batch_write` for any number of writes.
    #[pyo3(signature = (request_items, *, return_consumed_capacity = "NONE", return_item_collection_metrics = false))]
    fn batch_write_item<'py>(
        &self,
        py: Python<'py>,
        request_items: &Bound<'py, PyDict>,
        return_consumed_capacity: &str,
        return_item_collection_metrics: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let mut items = BTreeMap::new();
        for (table, requests) in request_items.iter() {
            let table: String = table.extract()?;
            let mut converted = Vec::new();
            for request in requests.try_iter()? {
                let (kind, item): (String, Bound<'py, PyAny>) = request?.extract()?;
                converted.push(write_request(&kind, &item)?);
            }
            items.insert(table, converted);
        }
        let input = BatchWriteItemInput {
            request_items: items,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_item_collection_metrics,
        };
        self.run(py, async move {
            finish(client.batch_write_item(input).await, |py, output| {
                let unprocessed = PyDict::new(py);
                for (table, requests) in &output.unprocessed_items {
                    let list = PyList::empty(py);
                    for request in requests {
                        list.append(write_request_to_py(py, request)?)?;
                    }
                    unprocessed.set_item(table, list)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "unprocessed_items"), unprocessed.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                        (
                            intern!(py, "item_collection_metrics"),
                            collection_metrics_map(py, &output.item_collection_metrics)?.into_any(),
                        ),
                    ],
                )
            })
        })
    }

    /// Reads any number of keys (managed `BatchGetItem`: chunks, concurrency, unprocessed keys).
    #[pyo3(signature = (request_items, *, return_consumed_capacity = "NONE"))]
    fn batch_get<'py>(
        &self,
        py: Python<'py>,
        request_items: BTreeMap<String, KeysArg>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let request_items = request_items.into_iter().map(|(table, keys)| (table, keys.into_core())).collect();
        let capacity = constant!(return_consumed_capacity, ReturnConsumedCapacity, "return_consumed_capacity")?;
        self.run(py, async move {
            finish(client.batch_get(request_items, capacity).await, |py, output| {
                let items = PyDict::new(py);
                for (table, found) in &output.items {
                    items.set_item(table, items_to_py(py, found)?)?;
                }
                let unprocessed = PyDict::new(py);
                for (table, keys) in &output.unprocessed {
                    unprocessed.set_item(table, keys_and_attributes(py, keys)?)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "items"), items.into_any()),
                        (intern!(py, "unprocessed_keys"), unprocessed.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                    ],
                )
            })
        })
    }

    /// Writes any number of `(table, "put" | "delete", item_or_key)` requests (managed
    /// `BatchWriteItem`: chunks of 25, concurrency, unprocessed items resent).
    #[pyo3(signature = (requests, *, return_consumed_capacity = "NONE"))]
    fn batch_write<'py>(
        &self,
        py: Python<'py>,
        requests: &Bound<'py, PyAny>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let mut converted = Vec::new();
        for request in requests.try_iter()? {
            let (table, kind, item): (String, String, Bound<'py, PyAny>) = request?.extract()?;
            converted.push((table, write_request(&kind, &item)?));
        }
        let capacity = constant!(return_consumed_capacity, ReturnConsumedCapacity, "return_consumed_capacity")?;
        self.run(py, async move {
            finish(client.batch_write(converted, capacity).await, |py, output| {
                let unprocessed = PyList::empty(py);
                for (table, request) in &output.unprocessed {
                    let pair = write_request_to_py(py, request)?;
                    unprocessed.append((table.as_str(), pair.get_item(0)?, pair.get_item(1)?))?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "unprocessed"), unprocessed.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                    ],
                )
            })
        })
    }

    /// `TransactGetItems`.
    #[pyo3(signature = (transact_items, *, return_consumed_capacity = "NONE"))]
    fn transact_get_items<'py>(
        &self,
        py: Python<'py>,
        transact_items: Vec<TransactGetArg>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = TransactGetItemsInput {
            transact_items: transact_items
                .into_iter()
                .map(|get| TransactGet {
                    table_name: get.table_name,
                    key: get.key.0,
                    projection_expression: get.projection_expression,
                    expression_attribute_names: names(get.expression_attribute_names),
                })
                .collect(),
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move {
            finish(client.transact_get_items(input).await, |py, output| {
                let responses = PyList::empty(py);
                for item in &output.responses {
                    responses.append(optional_item(py, item.as_ref())?)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "responses"), responses.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                    ],
                )
            })
        })
    }

    /// `TransactWriteItems`. Without `client_request_token` one is generated (idempotent retries).
    #[pyo3(signature = (transact_items, *, client_request_token = None, return_consumed_capacity = "NONE", return_item_collection_metrics = false))]
    fn transact_write_items<'py>(
        &self,
        py: Python<'py>,
        transact_items: Vec<TransactWriteArg>,
        client_request_token: Option<String>,
        return_consumed_capacity: &str,
        return_item_collection_metrics: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = TransactWriteItemsInput {
            transact_items: transact_items
                .into_iter()
                .enumerate()
                .map(|(position, item)| item.into_core(position))
                .collect::<PyResult<_>>()?,
            client_request_token,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_item_collection_metrics,
        };
        self.run(py, async move {
            finish(client.transact_write_items(input).await, |py, output| {
                dict(
                    py,
                    &[
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                        (
                            intern!(py, "item_collection_metrics"),
                            collection_metrics_map(py, &output.item_collection_metrics)?.into_any(),
                        ),
                    ],
                )
            })
        })
    }

    /// `ExecuteStatement` (PartiQL).
    #[pyo3(signature = (statement, *, parameters = None, consistent_read = false, next_token = None, limit = None, return_consumed_capacity = "NONE", return_values_on_condition_check_failure = "NONE"))]
    #[allow(clippy::too_many_arguments)]
    fn execute_statement<'py>(
        &self,
        py: Python<'py>,
        statement: String,
        parameters: Option<PyValueList>,
        consistent_read: bool,
        next_token: Option<String>,
        limit: Option<u32>,
        return_consumed_capacity: &str,
        return_values_on_condition_check_failure: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = ExecuteStatementInput {
            statement,
            parameters: parameters.map(|parameters| parameters.0).unwrap_or_default(),
            consistent_read,
            next_token,
            limit,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
            return_values_on_condition_check_failure: constant!(
                return_values_on_condition_check_failure,
                ReturnValuesOnConditionCheckFailure,
                "return_values_on_condition_check_failure"
            )?,
        };
        self.run(py, async move {
            finish(client.execute_statement(input).await, |py, output| {
                dict(
                    py,
                    &[
                        (intern!(py, "items"), items_to_py(py, &output.items)?.into_any()),
                        (intern!(py, "next_token"), output.next_token.into_pyobject(py)?.into_any()),
                        (intern!(py, "last_evaluated_key"), optional_item(py, output.last_evaluated_key.as_ref())?),
                        (intern!(py, "consumed_capacity"), optional_capacity(py, output.consumed_capacity.as_ref())?),
                    ],
                )
            })
        })
    }

    /// `BatchExecuteStatement` (PartiQL).
    #[pyo3(signature = (statements, *, return_consumed_capacity = "NONE"))]
    fn batch_execute_statement<'py>(
        &self,
        py: Python<'py>,
        statements: Vec<StatementArg>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = BatchExecuteStatementInput {
            statements: statements.into_iter().map(StatementArg::into_core).collect::<PyResult<_>>()?,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move {
            finish(client.batch_execute_statement(input).await, |py, output| {
                let responses = PyList::empty(py);
                for response in &output.responses {
                    let error = match &response.error {
                        Some(error) => {
                            let item =
                                error.item.as_ref().map(|item| crate::convert::item_to_py(py, item)).transpose()?;
                            (error.code.as_str(), error.message.as_deref(), item).into_pyobject(py)?.into_any()
                        }
                        None => py.None().into_bound(py),
                    };
                    let entry = PyDict::new(py);
                    entry.set_item("error", error)?;
                    entry.set_item("table_name", &response.table_name)?;
                    entry.set_item("item", optional_item(py, response.item.as_ref())?)?;
                    responses.append(entry)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "responses"), responses.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                    ],
                )
            })
        })
    }

    /// `ExecuteTransaction` (PartiQL).
    #[pyo3(signature = (statements, *, client_request_token = None, return_consumed_capacity = "NONE"))]
    fn execute_transaction<'py>(
        &self,
        py: Python<'py>,
        statements: Vec<StatementArg>,
        client_request_token: Option<String>,
        return_consumed_capacity: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = ExecuteTransactionInput {
            transact_statements: statements.into_iter().map(StatementArg::into_core).collect::<PyResult<_>>()?,
            client_request_token,
            return_consumed_capacity: constant!(
                return_consumed_capacity,
                ReturnConsumedCapacity,
                "return_consumed_capacity"
            )?,
        };
        self.run(py, async move {
            finish(client.execute_transaction(input).await, |py, output| {
                let responses = PyList::empty(py);
                for item in &output.responses {
                    responses.append(optional_item(py, item.as_ref())?)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "responses"), responses.into_any()),
                        (intern!(py, "consumed_capacity"), capacities(py, &output.consumed_capacity)?.into_any()),
                    ],
                )
            })
        })
    }

    /// `CreateTable`.
    #[pyo3(signature = (table_name, *, attribute_definitions, key_schema, local_secondary_indexes = Vec::new(), global_secondary_indexes = Vec::new(), billing_mode = "PAY_PER_REQUEST", provisioned_throughput = None, on_demand_throughput = None, stream_specification = None, sse_specification = None, tags = BTreeMap::new(), table_class = None, deletion_protection_enabled = false))]
    #[allow(clippy::too_many_arguments)]
    fn create_table<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        attribute_definitions: Vec<(String, String)>,
        key_schema: (String, Option<String>),
        local_secondary_indexes: Vec<LocalIndexArg>,
        global_secondary_indexes: Vec<GlobalIndexArg>,
        billing_mode: &str,
        provisioned_throughput: Option<(i64, i64)>,
        on_demand_throughput: Option<(Option<i64>, Option<i64>)>,
        stream_specification: Option<(bool, Option<String>)>,
        sse_specification: Option<(bool, Option<String>, Option<String>)>,
        tags: BTreeMap<String, String>,
        table_class: Option<&str>,
        deletion_protection_enabled: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = CreateTableInput {
            table_name,
            attribute_definitions: attribute_definitions
                .into_iter()
                .map(|(name, kind)| {
                    Ok(AttributeDefinition { name, attribute_type: constant!(&kind, ScalarType, "attribute type")? })
                })
                .collect::<PyResult<_>>()?,
            key_schema: self::key_schema(key_schema),
            local_secondary_indexes: local_secondary_indexes
                .into_iter()
                .map(|index| {
                    Ok(LocalSecondaryIndex {
                        index_name: index.index_name,
                        key_schema: self::key_schema(index.key_schema),
                        projection: projection(index.projection)?,
                    })
                })
                .collect::<PyResult<_>>()?,
            global_secondary_indexes: global_secondary_indexes
                .into_iter()
                .map(GlobalIndexArg::into_core)
                .collect::<PyResult<_>>()?,
            billing_mode: constant!(billing_mode, BillingMode, "billing_mode")?,
            provisioned_throughput: throughput(provisioned_throughput),
            on_demand_throughput: on_demand(on_demand_throughput),
            stream_specification: stream(stream_specification)?,
            sse_specification: sse(sse_specification),
            tags,
            table_class: optional_constant(table_class, |value| constant!(value, TableClass, "table_class"))?,
            deletion_protection_enabled,
        };
        self.run(py, async move { finish(client.create_table(input).await, table_output) })
    }

    /// `DeleteTable`.
    fn delete_table<'py>(&self, py: Python<'py>, table_name: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move { finish(client.delete_table(&table_name).await, table_output) })
    }

    /// `DescribeTable`.
    fn describe_table<'py>(&self, py: Python<'py>, table_name: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move { finish(client.describe_table(&table_name).await, table_output) })
    }

    /// `UpdateTable`. Index changes are dicts: `{"create": {...}}`, `{"update": {"index_name": ...,
    /// "provisioned_throughput": ...}}` or `{"delete": "index_name"}`.
    #[pyo3(signature = (table_name, *, attribute_definitions = Vec::new(), billing_mode = None, provisioned_throughput = None, on_demand_throughput = None, global_secondary_index_updates = None, stream_specification = None, sse_specification = None, table_class = None, deletion_protection_enabled = None))]
    #[allow(clippy::too_many_arguments)]
    fn update_table<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        attribute_definitions: Vec<(String, String)>,
        billing_mode: Option<&str>,
        provisioned_throughput: Option<(i64, i64)>,
        on_demand_throughput: Option<(Option<i64>, Option<i64>)>,
        global_secondary_index_updates: Option<&Bound<'py, PyList>>,
        stream_specification: Option<(bool, Option<String>)>,
        sse_specification: Option<(bool, Option<String>, Option<String>)>,
        table_class: Option<&str>,
        deletion_protection_enabled: Option<bool>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let mut updates = Vec::new();
        for update in global_secondary_index_updates.into_iter().flatten() {
            let update = update
                .cast_into::<PyDict>()
                .map_err(|_| ValidationError::new_err("global_secondary_index_updates entries must be dicts"))?;
            if let Some(create) = update.get_item("create")? {
                updates.push(GlobalSecondaryIndexUpdate::Create(create.extract::<GlobalIndexArg>()?.into_core()?));
            } else if let Some(change) = update.get_item("update")? {
                let change =
                    change.cast_into::<PyDict>().map_err(|_| ValidationError::new_err("\"update\" must be a dict"))?;
                let index_name: String = change
                    .get_item("index_name")?
                    .ok_or_else(|| ValidationError::new_err("\"update\" needs index_name"))?
                    .extract()?;
                let provisioned: Option<(i64, i64)> =
                    change.get_item("provisioned_throughput")?.map(|value| value.extract()).transpose()?.flatten();
                let demand: Option<(Option<i64>, Option<i64>)> =
                    change.get_item("on_demand_throughput")?.map(|value| value.extract()).transpose()?.flatten();
                updates.push(GlobalSecondaryIndexUpdate::Update {
                    index_name,
                    provisioned_throughput: throughput(provisioned),
                    on_demand_throughput: on_demand(demand),
                });
            } else if let Some(index_name) = update.get_item("delete")? {
                updates.push(GlobalSecondaryIndexUpdate::Delete { index_name: index_name.extract()? });
            } else {
                return Err(ValidationError::new_err(
                    "global_secondary_index_updates entries need a \"create\", \"update\" or \"delete\" key",
                ));
            }
        }
        let input = UpdateTableInput {
            table_name,
            attribute_definitions: attribute_definitions
                .into_iter()
                .map(|(name, kind)| {
                    Ok(AttributeDefinition { name, attribute_type: constant!(&kind, ScalarType, "attribute type")? })
                })
                .collect::<PyResult<_>>()?,
            billing_mode: optional_constant(billing_mode, |value| constant!(value, BillingMode, "billing_mode"))?,
            provisioned_throughput: throughput(provisioned_throughput),
            on_demand_throughput: on_demand(on_demand_throughput),
            global_secondary_index_updates: updates,
            stream_specification: stream(stream_specification)?,
            sse_specification: sse(sse_specification),
            table_class: optional_constant(table_class, |value| constant!(value, TableClass, "table_class"))?,
            deletion_protection_enabled,
        };
        self.run(py, async move { finish(client.update_table(input).await, table_output) })
    }

    /// `ListTables` (one page).
    #[pyo3(signature = (*, exclusive_start_table_name = None, limit = None))]
    fn list_tables<'py>(
        &self,
        py: Python<'py>,
        exclusive_start_table_name: Option<String>,
        limit: Option<u32>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = ListTablesInput { exclusive_start_table_name, limit };
        self.run(py, async move {
            finish(client.list_tables(input).await, |py, output| {
                dict(
                    py,
                    &[
                        (intern!(py, "table_names"), output.table_names.into_pyobject(py)?.into_any()),
                        (
                            intern!(py, "last_evaluated_table_name"),
                            output.last_evaluated_table_name.into_pyobject(py)?.into_any(),
                        ),
                    ],
                )
            })
        })
    }

    /// Waits until the table is ACTIVE with every index ACTIVE (`exists=True`) or until it no
    /// longer exists (`exists=False`). Awaitable: the description, or `None` once deleted.
    #[pyo3(signature = (table_name, *, exists = true, timeout = 300.0))]
    fn wait_for_table<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        exists: bool,
        timeout: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let timeout = duration(timeout, "timeout")?;
        self.run(py, async move {
            let waited = client.wait_for_table(&table_name, exists, timeout).await;
            Deferred::new(move |py| match waited {
                Ok(Some(description)) => Ok(table_description(py, &description)?.into_any().unbind()),
                Ok(None) => Ok(py.None()),
                Err(error) => Err(error_in(py, error)),
            })
        })
    }

    /// `UpdateTimeToLive`; returns `(status, attribute_name)`.
    #[pyo3(signature = (table_name, *, enabled, attribute_name))]
    fn update_time_to_live<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        enabled: bool,
        attribute_name: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = UpdateTimeToLiveInput { table_name, enabled, attribute_name };
        self.run(py, async move {
            finish(client.update_time_to_live(input).await, |py, output| {
                Ok(ttl_to_py(&output).into_pyobject(py)?.into_any().unbind())
            })
        })
    }

    /// `DescribeTimeToLive`; returns `(status, attribute_name)`.
    fn describe_time_to_live<'py>(&self, py: Python<'py>, table_name: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.describe_time_to_live(&table_name).await, |py, output| {
                Ok(ttl_to_py(&output).into_pyobject(py)?.into_any().unbind())
            })
        })
    }

    /// `TagResource`.
    fn tag_resource<'py>(
        &self,
        py: Python<'py>,
        resource_arn: String,
        tags: BTreeMap<String, String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        future_into_py(
            py,
            async move { finish(client.tag_resource(&resource_arn, tags).await, |py, ()| Ok(py.None())) },
        )
    }

    /// `UntagResource`.
    fn untag_resource<'py>(
        &self,
        py: Python<'py>,
        resource_arn: String,
        tag_keys: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(
            py,
            async move { finish(client.untag_resource(&resource_arn, tag_keys).await, |py, ()| Ok(py.None())) },
        )
    }

    /// `ListTagsOfResource` (every page); returns a dict of tags.
    fn list_tags_of_resource<'py>(&self, py: Python<'py>, resource_arn: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.list_tags_of_resource(&resource_arn).await, |py, output| {
                Ok(output.tags.into_pyobject(py)?.into_any().unbind())
            })
        })
    }

    /// `DescribeContinuousBackups`.
    fn describe_continuous_backups<'py>(&self, py: Python<'py>, table_name: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.describe_continuous_backups(&table_name).await, |py, output| {
                Ok(continuous_backups(py, &output)?.into_any().unbind())
            })
        })
    }

    /// `UpdateContinuousBackups` (point-in-time recovery).
    #[pyo3(signature = (table_name, *, enabled, recovery_period_in_days = None))]
    fn update_continuous_backups<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(
                client.update_continuous_backups(&table_name, enabled, recovery_period_in_days).await,
                |py, output| Ok(continuous_backups(py, &output)?.into_any().unbind()),
            )
        })
    }

    /// `CreateBackup`.
    fn create_backup<'py>(
        &self,
        py: Python<'py>,
        table_name: String,
        backup_name: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.create_backup(&table_name, &backup_name).await, |py, output| {
                Ok(backup_details(py, &output)?.into_any().unbind())
            })
        })
    }

    /// `DescribeBackup`.
    fn describe_backup<'py>(&self, py: Python<'py>, backup_arn: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.describe_backup(&backup_arn).await, |py, output| {
                Ok(backup_description(py, &output)?.into_any().unbind())
            })
        })
    }

    /// `DeleteBackup`.
    fn delete_backup<'py>(&self, py: Python<'py>, backup_arn: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        self.run(py, async move {
            finish(client.delete_backup(&backup_arn).await, |py, output| {
                Ok(backup_description(py, &output)?.into_any().unbind())
            })
        })
    }

    /// `ListBackups` (one page).
    #[pyo3(signature = (*, table_name = None, limit = None, exclusive_start_backup_arn = None, backup_type = None, time_range_lower_bound = None, time_range_upper_bound = None))]
    #[allow(clippy::too_many_arguments)]
    fn list_backups<'py>(
        &self,
        py: Python<'py>,
        table_name: Option<String>,
        limit: Option<u32>,
        exclusive_start_backup_arn: Option<String>,
        backup_type: Option<&str>,
        time_range_lower_bound: Option<f64>,
        time_range_upper_bound: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = ListBackupsInput {
            table_name,
            limit,
            exclusive_start_backup_arn,
            backup_type: optional_constant(backup_type, |value| constant!(value, BackupTypeFilter, "backup_type"))?,
            time_range_lower_bound,
            time_range_upper_bound,
        };
        self.run(py, async move {
            finish(client.list_backups(input).await, |py, output| {
                let backups = PyList::empty(py);
                for backup in &output.backups {
                    backups.append(backup_summary(py, backup)?)?;
                }
                dict(
                    py,
                    &[
                        (intern!(py, "backups"), backups.into_any()),
                        (
                            intern!(py, "last_evaluated_backup_arn"),
                            output.last_evaluated_backup_arn.into_pyobject(py)?.into_any(),
                        ),
                    ],
                )
            })
        })
    }

    /// `RestoreTableFromBackup`.
    #[pyo3(signature = (target_table_name, backup_arn, *, billing_mode = None, provisioned_throughput = None, sse_specification = None))]
    fn restore_table_from_backup<'py>(
        &self,
        py: Python<'py>,
        target_table_name: String,
        backup_arn: String,
        billing_mode: Option<&str>,
        provisioned_throughput: Option<(i64, i64)>,
        sse_specification: Option<(bool, Option<String>, Option<String>)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = RestoreTableFromBackupInput {
            target_table_name,
            backup_arn,
            overrides: RestoreOverrides {
                billing_mode: optional_constant(billing_mode, |value| constant!(value, BillingMode, "billing_mode"))?,
                provisioned_throughput: throughput(provisioned_throughput),
                sse_specification: sse(sse_specification),
            },
        };
        self.run(py, async move { finish(client.restore_table_from_backup(input).await, table_output) })
    }

    /// `RestoreTableToPointInTime`.
    #[pyo3(signature = (source_table_name, target_table_name, *, use_latest_restorable_time = true, restore_date_time = None, billing_mode = None, provisioned_throughput = None, sse_specification = None))]
    #[allow(clippy::too_many_arguments)]
    fn restore_table_to_point_in_time<'py>(
        &self,
        py: Python<'py>,
        source_table_name: String,
        target_table_name: String,
        use_latest_restorable_time: bool,
        restore_date_time: Option<f64>,
        billing_mode: Option<&str>,
        provisioned_throughput: Option<(i64, i64)>,
        sse_specification: Option<(bool, Option<String>, Option<String>)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let input = RestoreTableToPointInTimeInput {
            source_table_name,
            target_table_name,
            use_latest_restorable_time,
            restore_date_time,
            overrides: RestoreOverrides {
                billing_mode: optional_constant(billing_mode, |value| constant!(value, BillingMode, "billing_mode"))?,
                provisioned_throughput: throughput(provisioned_throughput),
                sse_specification: sse(sse_specification),
            },
        };
        self.run(py, async move { finish(client.restore_table_to_point_in_time(input).await, table_output) })
    }

    /// Records exactly these event kinds (names); all others cost one atomic load.
    fn set_event_filter(&self, kinds: Vec<String>) -> PyResult<()> {
        let kinds = kinds
            .iter()
            .map(|name| {
                EventKind::parse(name).ok_or_else(|| ValidationError::new_err(format!("unknown event kind {name:?}")))
            })
            .collect::<PyResult<Vec<_>>>()?;
        self.client.events().set_enabled(&kinds);
        Ok(())
    }

    /// Buffered events as `(kind, message, fields)` tuples, without waiting.
    fn drain_events<'py>(&self, py: Python<'py>, max: usize) -> PyResult<Bound<'py, PyList>> {
        events_to_py(py, self.client.events().drain(max))
    }

    /// Waits up to `timeout` seconds until events are buffered (awaitable, returns whether
    /// they are); take them with `drain_events`.
    fn wait_events<'py>(&self, py: Python<'py>, timeout: f64) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client()?;
        let timeout = duration(timeout, "timeout")?;
        self.run(py, async move { Deferred::value(client.events().wait(timeout).await) })
    }

    /// Returns and resets the number of events dropped because the buffer was full.
    fn take_dropped_events(&self) -> u64 {
        self.client.events().take_dropped()
    }

    /// A snapshot of the metrics: `{"in_flight": n, "latency_buckets_ms": [...], "operations": {name: {...}}}`.
    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let snapshot = self.client.metrics().snapshot();
        let operations = PyDict::new(py);
        for (name, metrics) in &snapshot.operations {
            let entry = PyDict::new(py);
            entry.set_item("calls", metrics.calls)?;
            entry.set_item("errors", metrics.errors)?;
            entry.set_item("attempts", metrics.attempts)?;
            entry.set_item("throttles", metrics.throttles)?;
            entry.set_item("duration", metrics.duration)?;
            entry.set_item("read_capacity_units", metrics.read_capacity_units)?;
            entry.set_item("write_capacity_units", metrics.write_capacity_units)?;
            entry.set_item("latency_buckets", metrics.latency_buckets.clone())?;
            operations.set_item(*name, entry)?;
        }
        let result = PyDict::new(py);
        result.set_item("in_flight", snapshot.in_flight)?;
        let bounds: Vec<Option<f64>> =
            LATENCY_BUCKETS_MS.iter().map(|bound| bound.is_finite().then_some(*bound)).collect();
        result.set_item("latency_buckets_ms", bounds)?;
        result.set_item("operations", operations)?;
        Ok(result)
    }

    /// Resets every metric counter.
    fn reset_metrics(&self) {
        self.client.metrics().reset();
    }
}
