//! Python exception hierarchy and conversion from core errors.
//!
//! ```text
//! DynamoxyError                        .retryable .code .message .operation .request_id .status_code
//! ├── ConfigurationError               invalid client configuration
//! ├── ValidationError                  invalid request (local, or DynamoDB's ValidationException)
//! ├── ServiceError                     any other error returned by DynamoDB
//! │   ├── ConditionalCheckFailedError  .item (the old item when requested)
//! │   ├── TransactionCanceledError     .reasons [(code, message, item), ...]
//! │   ├── TransactionConflictError
//! │   ├── TransactionInProgressError
//! │   ├── IdempotentParameterMismatchError
//! │   ├── ThrottlingError
//! │   ├── ResourceNotFoundError
//! │   ├── ResourceInUseError
//! │   ├── LimitExceededError
//! │   ├── ItemCollectionSizeLimitExceededError
//! │   ├── DuplicateItemError
//! │   ├── BackupError
//! │   ├── AccessDeniedError
//! │   └── InternalServerError
//! ├── TransportError                   network failures and timeouts
//! ├── CredentialsError                 AWS credentials missing or invalid
//! └── ShutdownError                    the client or the interpreter is shutting down
//! ```
//!
//! Every instance also has `.attempts`, `.throttles`, `.requests` and `.duration` (seconds) when it
//! comes from a call.
//! Python-only errors (`ItemNotFoundError`, `ItemAlreadyExistsError`, `VersionConflictError`,
//! `UniqueConstraintError`, `ItemDecodeError`) are defined in `python/dynamoxy/errors.py`.
//! The native classes name `dynamoxy.errors` as their module too: it re-exports them, so tracebacks
//! show the public name and `pickle` finds them there.

use dynamoxy_core::{Error, ErrorKind};
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::convert::item_to_py;

create_exception!(dynamoxy.errors, DynamoxyError, PyException, "Base class of all dynamoxy errors.");
create_exception!(dynamoxy.errors, ConfigurationError, DynamoxyError, "Invalid client configuration.");
create_exception!(
    dynamoxy.errors,
    ValidationError,
    DynamoxyError,
    "An invalid request, detected locally or by DynamoDB."
);
create_exception!(dynamoxy.errors, ServiceError, DynamoxyError, "An error returned by DynamoDB.");
create_exception!(
    dynamoxy.errors,
    ConditionalCheckFailedError,
    ServiceError,
    "A condition expression evaluated to false."
);
create_exception!(
    dynamoxy.errors,
    TransactionCanceledError,
    ServiceError,
    "DynamoDB canceled a transaction; see `.reasons`."
);
create_exception!(dynamoxy.errors, TransactionConflictError, ServiceError, "Another transaction is updating the item.");
create_exception!(
    dynamoxy.errors,
    TransactionInProgressError,
    ServiceError,
    "A transaction with the same token is still running."
);
create_exception!(
    dynamoxy.errors,
    IdempotentParameterMismatchError,
    ServiceError,
    "A client request token was reused with other parameters."
);
create_exception!(dynamoxy.errors, ThrottlingError, ServiceError, "DynamoDB throttled the request.");
create_exception!(dynamoxy.errors, ResourceNotFoundError, ServiceError, "The table, index or backup does not exist.");
create_exception!(dynamoxy.errors, ResourceInUseError, ServiceError, "The table exists already or is being changed.");
create_exception!(
    dynamoxy.errors,
    LimitExceededError,
    ServiceError,
    "Too many tables or concurrent control-plane operations."
);
create_exception!(
    dynamoxy.errors,
    ItemCollectionSizeLimitExceededError,
    ServiceError,
    "An item collection exceeds 10 GB (tables with local secondary indexes)."
);
create_exception!(dynamoxy.errors, DuplicateItemError, ServiceError, "A PartiQL INSERT targets an existing item.");
create_exception!(dynamoxy.errors, BackupError, ServiceError, "A backup or point-in-time recovery operation failed.");
create_exception!(
    dynamoxy.errors,
    AccessDeniedError,
    ServiceError,
    "The credentials are not allowed to perform the request."
);
create_exception!(dynamoxy.errors, InternalServerError, ServiceError, "DynamoDB failed internally (retryable).");
create_exception!(dynamoxy.errors, TransportError, DynamoxyError, "A network failure or a timeout.");
create_exception!(
    dynamoxy.errors,
    WriteOutcomeUnknownError,
    TransportError,
    "A write that may have been applied, without an answer that says whether it was."
);
create_exception!(
    dynamoxy.errors,
    CredentialsError,
    DynamoxyError,
    "AWS credentials are missing or could not be loaded."
);
create_exception!(dynamoxy.errors, ShutdownError, DynamoxyError, "The client is closed or the interpreter is exiting.");

fn service_class(kind: ErrorKind) -> fn(String) -> PyErr {
    match kind {
        ErrorKind::Validation => ValidationError::new_err,
        ErrorKind::ConditionalCheckFailed => ConditionalCheckFailedError::new_err,
        ErrorKind::TransactionCanceled => TransactionCanceledError::new_err,
        ErrorKind::TransactionConflict => TransactionConflictError::new_err,
        ErrorKind::TransactionInProgress => TransactionInProgressError::new_err,
        ErrorKind::IdempotentParameterMismatch => IdempotentParameterMismatchError::new_err,
        ErrorKind::Throttling => ThrottlingError::new_err,
        ErrorKind::ResourceNotFound => ResourceNotFoundError::new_err,
        ErrorKind::ResourceInUse => ResourceInUseError::new_err,
        ErrorKind::LimitExceeded => LimitExceededError::new_err,
        ErrorKind::ItemCollectionSizeLimitExceeded => ItemCollectionSizeLimitExceededError::new_err,
        ErrorKind::DuplicateItem => DuplicateItemError::new_err,
        ErrorKind::Backup | ErrorKind::ContinuousBackups => BackupError::new_err,
        ErrorKind::AccessDenied => AccessDeniedError::new_err,
        ErrorKind::Internal => InternalServerError::new_err,
        _ => ServiceError::new_err,
    }
}

/// Converts a core error into a Python exception with structured attributes.
pub fn to_py_err(error: Error) -> PyErr {
    Python::attach(|py| error_in(py, error))
}

/// [`to_py_err`] for callers that are attached to Python already.
pub fn error_in(py: Python<'_>, error: Error) -> PyErr {
    build(py, error).unwrap_or_else(|err| err)
}

/// Like [`error_in`], also recording what the call cost.
pub fn call_error_in(py: Python<'_>, error: Error, stats: &dynamoxy_core::OperationStats) -> PyErr {
    let err = error_in(py, error);
    let value = err.value(py);
    let _ = value.setattr(intern!(py, "attempts"), stats.attempts);
    let _ = value.setattr(intern!(py, "throttles"), stats.throttles);
    let _ = value.setattr(intern!(py, "requests"), stats.requests);
    let _ = value.setattr(intern!(py, "duration"), stats.duration.as_secs_f64());
    err
}

fn build(py: Python<'_>, error: Error) -> PyResult<PyErr> {
    let retryable = error.is_retryable();
    let err = match &error {
        Error::Config(message) => ConfigurationError::new_err(message.clone()),
        Error::Validation(message) => ValidationError::new_err(message.clone()),
        Error::Service(service) => service_class(service.kind)(service.to_string()),
        Error::Transport { .. } => TransportError::new_err(error.to_string()),
        Error::OutcomeUnknown { .. } => WriteOutcomeUnknownError::new_err(error.to_string()),
        Error::Credentials(_) => CredentialsError::new_err(error.to_string()),
        Error::Shutdown => ShutdownError::new_err(error.to_string()),
        other => DynamoxyError::new_err(other.to_string()),
    };
    let value = err.value(py);
    value.setattr(intern!(py, "retryable"), retryable)?;
    match error {
        Error::Service(service) => {
            value.setattr(intern!(py, "code"), &service.code)?;
            value.setattr(intern!(py, "message"), &service.message)?;
            value.setattr(intern!(py, "operation"), service.operation)?;
            value.setattr(intern!(py, "request_id"), &service.request_id)?;
            value.setattr(intern!(py, "status_code"), service.status)?;
            if let Some(item) = &service.item {
                value.setattr(intern!(py, "item"), item_to_py(py, item)?)?;
            }
            if service.kind == ErrorKind::TransactionCanceled {
                let reasons = PyList::empty(py);
                for reason in &service.cancellation_reasons {
                    let item = reason.item.as_ref().map(|item| item_to_py(py, item)).transpose()?;
                    reasons.append((reason.code.as_str(), reason.message.as_deref(), item))?;
                }
                value.setattr(intern!(py, "reasons"), reasons)?;
            }
        }
        Error::Transport { operation, message, .. } => {
            value.setattr(intern!(py, "operation"), operation)?;
            value.setattr(intern!(py, "message"), message)?;
        }
        Error::OutcomeUnknown { operation, message, item } => {
            value.setattr(intern!(py, "operation"), operation)?;
            value.setattr(intern!(py, "message"), message)?;
            if let Some(item) = &item {
                value.setattr(intern!(py, "item"), item_to_py(py, item)?)?;
            }
        }
        Error::Validation(message) | Error::Config(message) | Error::Credentials(message) => {
            value.setattr(intern!(py, "message"), message)?;
        }
        _ => {}
    }
    Ok(err)
}

/// Registers the exception classes on the module, with class-level attribute defaults.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    let classes = [
        ("DynamoxyError", py.get_type::<DynamoxyError>()),
        ("ConfigurationError", py.get_type::<ConfigurationError>()),
        ("ValidationError", py.get_type::<ValidationError>()),
        ("ServiceError", py.get_type::<ServiceError>()),
        ("ConditionalCheckFailedError", py.get_type::<ConditionalCheckFailedError>()),
        ("TransactionCanceledError", py.get_type::<TransactionCanceledError>()),
        ("TransactionConflictError", py.get_type::<TransactionConflictError>()),
        ("TransactionInProgressError", py.get_type::<TransactionInProgressError>()),
        ("IdempotentParameterMismatchError", py.get_type::<IdempotentParameterMismatchError>()),
        ("ThrottlingError", py.get_type::<ThrottlingError>()),
        ("ResourceNotFoundError", py.get_type::<ResourceNotFoundError>()),
        ("ResourceInUseError", py.get_type::<ResourceInUseError>()),
        ("LimitExceededError", py.get_type::<LimitExceededError>()),
        ("ItemCollectionSizeLimitExceededError", py.get_type::<ItemCollectionSizeLimitExceededError>()),
        ("DuplicateItemError", py.get_type::<DuplicateItemError>()),
        ("BackupError", py.get_type::<BackupError>()),
        ("AccessDeniedError", py.get_type::<AccessDeniedError>()),
        ("InternalServerError", py.get_type::<InternalServerError>()),
        ("TransportError", py.get_type::<TransportError>()),
        ("WriteOutcomeUnknownError", py.get_type::<WriteOutcomeUnknownError>()),
        ("CredentialsError", py.get_type::<CredentialsError>()),
        ("ShutdownError", py.get_type::<ShutdownError>()),
    ];
    for (name, class) in classes {
        m.add(name, &class)?;
    }
    // Class-level defaults on the base class; instances created by `to_py_err` override them.
    let base = py.get_type::<DynamoxyError>();
    base.setattr("retryable", false)?;
    for attribute in
        ["code", "message", "operation", "request_id", "status_code", "attempts", "throttles", "requests", "duration"]
    {
        base.setattr(attribute, py.None())?;
    }
    py.get_type::<ConditionalCheckFailedError>().setattr("item", py.None())?;
    py.get_type::<TransactionCanceledError>().setattr("reasons", PyList::empty(py))?;
    py.get_type::<ThrottlingError>().setattr("retryable", true)?;
    py.get_type::<InternalServerError>().setattr("retryable", true)?;
    py.get_type::<TransactionConflictError>().setattr("retryable", true)?;
    py.get_type::<TransportError>().setattr("retryable", true)?;
    // Sending a write again after an unknown outcome may apply it twice.
    py.get_type::<WriteOutcomeUnknownError>().setattr("retryable", false)?;
    py.get_type::<WriteOutcomeUnknownError>().setattr("item", py.None())?;
    Ok(())
}
