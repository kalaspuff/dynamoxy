//! Error model for the core.
//!
//! Every fallible operation returns [`Result<T>`]. Messages say what to change, and
//! [`Error::is_retryable`] tells callers whether retrying the same operation may succeed.
//!
//! * [`Error::Config`] and [`Error::Validation`] are raised locally, before any I/O.
//! * [`Error::Service`] is an error returned by DynamoDB - or by the in-memory backend, which
//!   produces the same codes and messages as DynamoDB for the same situations. Its
//!   [`ErrorKind`] classifies the service error code in one place ([`ErrorKind::from_code`]).
//! * [`Error::Transport`] covers network failures and timeouts.
//! * [`Error::OutcomeUnknown`] is a write that may have been applied, without an answer that
//!   says whether it was (see [`crate::client`]).

use std::fmt;

use crate::value::Item;

/// Convenience alias used throughout the crate.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Everything that can go wrong in the core.
#[derive(Clone, Debug, PartialEq, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    /// Invalid configuration, detected before any I/O.
    #[error("invalid configuration: {0}")]
    Config(String),
    /// A request failed validation before any I/O.
    #[error("{0}")]
    Validation(String),
    /// An error returned by DynamoDB (or by the in-memory backend, modelled on DynamoDB).
    #[error("{0}")]
    Service(Box<ServiceError>),
    /// A network, timeout or protocol failure.
    #[error("{operation} failed: {message}")]
    Transport {
        /// The operation that failed (`PutItem`, ...).
        operation: &'static str,
        /// What happened.
        message: String,
        /// Whether retrying the operation may succeed.
        retryable: bool,
    },
    /// A write that may have been applied, without an answer that says whether it was: an
    /// attempt timed out, lost its connection or got a 5xx, and the write was not sent again (it
    /// could apply twice), or the answer to the next attempt may be the earlier attempt's doing
    /// (a failed condition, or old values).
    #[error("{operation} failed: {message}")]
    OutcomeUnknown {
        /// The operation (`PutItem`, ...).
        operation: &'static str,
        /// What happened.
        message: String,
        /// The item DynamoDB returned to the later attempt (with its failed condition, or as its
        /// old values), when there is one.
        item: Option<Item>,
    },
    /// Credentials are missing or could not be loaded.
    #[error("could not load AWS credentials: {0}")]
    Credentials(String),
    /// The client is shut down.
    #[error("the dynamoxy client is shut down")]
    Shutdown,
}

/// Classification of DynamoDB error codes. Decides retries and the Python exception class.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    /// `ValidationException`: the request is invalid.
    Validation,
    /// `ConditionalCheckFailedException`: a condition expression evaluated to false.
    ConditionalCheckFailed,
    /// `TransactionCanceledException`: a transaction was canceled (see the reasons).
    TransactionCanceled,
    /// `TransactionConflictException`: another transaction is updating the item.
    TransactionConflict,
    /// `TransactionInProgressException`: a transaction with the same token is still running.
    TransactionInProgress,
    /// `IdempotentParameterMismatchException`: a client request token was reused with other parameters.
    IdempotentParameterMismatch,
    /// `ProvisionedThroughputExceededException`, `ThrottlingException`, `RequestLimitExceeded`.
    Throttling,
    /// `ResourceNotFoundException`: the table, index or backup does not exist (or is not active yet).
    ResourceNotFound,
    /// `ResourceInUseException`: the table exists already or is being created, updated or deleted.
    ResourceInUse,
    /// `LimitExceededException`: too many concurrent control-plane operations or too many tables.
    LimitExceeded,
    /// `ItemCollectionSizeLimitExceededException`: an item collection exceeds 10 GB (local indexes).
    ItemCollectionSizeLimitExceeded,
    /// `BackupNotFoundException`, `BackupInUseException`, `TableNotFoundException`, ...
    Backup,
    /// `PointInTimeRecoveryUnavailableException`, `ContinuousBackupsUnavailableException`.
    ContinuousBackups,
    /// `DuplicateItemException`: a PartiQL `INSERT` targets an existing item.
    DuplicateItem,
    /// `AccessDeniedException`, `UnrecognizedClientException`, signature and token errors.
    AccessDenied,
    /// `InternalServerError`, `ServiceUnavailable` and other 5xx errors.
    Internal,
    /// Any other code.
    Other,
}

impl ErrorKind {
    /// Classifies a service error code (the `__type` of a DynamoDB error response).
    pub fn from_code(code: &str) -> Self {
        match code {
            "ValidationException" | "SerializationException" => Self::Validation,
            "ConditionalCheckFailedException" => Self::ConditionalCheckFailed,
            "TransactionCanceledException" => Self::TransactionCanceled,
            "TransactionConflictException" => Self::TransactionConflict,
            "TransactionInProgressException" => Self::TransactionInProgress,
            "IdempotentParameterMismatchException" => Self::IdempotentParameterMismatch,
            "ProvisionedThroughputExceededException" | "ThrottlingException" | "RequestLimitExceeded" => {
                Self::Throttling
            }
            "ResourceNotFoundException" => Self::ResourceNotFound,
            "ResourceInUseException" => Self::ResourceInUse,
            "LimitExceededException" => Self::LimitExceeded,
            "ItemCollectionSizeLimitExceededException" => Self::ItemCollectionSizeLimitExceeded,
            "BackupNotFoundException"
            | "BackupInUseException"
            | "TableNotFoundException"
            | "TableInUseException"
            | "TableAlreadyExistsException"
            | "InvalidRestoreTimeException" => Self::Backup,
            "PointInTimeRecoveryUnavailableException" | "ContinuousBackupsUnavailableException" => {
                Self::ContinuousBackups
            }
            "DuplicateItemException" => Self::DuplicateItem,
            "AccessDeniedException"
            | "MissingAuthenticationTokenException"
            | "UnrecognizedClientException"
            | "InvalidSignatureException"
            | "IncompleteSignatureException"
            | "ExpiredTokenException"
            | "InvalidClientTokenId" => Self::AccessDenied,
            "InternalServerError" | "InternalFailure" | "ServiceUnavailable" | "ServiceUnavailableException" => {
                Self::Internal
            }
            _ => Self::Other,
        }
    }

    /// Whether errors of this kind are transient.
    pub fn is_retryable(self) -> bool {
        matches!(self, Self::Throttling | Self::Internal | Self::TransactionConflict | Self::TransactionInProgress)
    }

    /// Stable lower-case name (`"conditional_check_failed"`), used across the Python boundary.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Validation => "validation",
            Self::ConditionalCheckFailed => "conditional_check_failed",
            Self::TransactionCanceled => "transaction_canceled",
            Self::TransactionConflict => "transaction_conflict",
            Self::TransactionInProgress => "transaction_in_progress",
            Self::IdempotentParameterMismatch => "idempotent_parameter_mismatch",
            Self::Throttling => "throttling",
            Self::ResourceNotFound => "resource_not_found",
            Self::ResourceInUse => "resource_in_use",
            Self::LimitExceeded => "limit_exceeded",
            Self::ItemCollectionSizeLimitExceeded => "item_collection_size_limit_exceeded",
            Self::Backup => "backup",
            Self::ContinuousBackups => "continuous_backups",
            Self::DuplicateItem => "duplicate_item",
            Self::AccessDenied => "access_denied",
            Self::Internal => "internal",
            Self::Other => "other",
        }
    }
}

/// Why one operation of a canceled transaction failed (`CancellationReasons` in DynamoDB).
#[derive(Clone, Debug, PartialEq)]
pub struct CancellationReason {
    /// `None` (this operation did not fail), `ConditionalCheckFailed`, `TransactionConflict`,
    /// `ValidationError`, `ThrottlingError`, `ProvisionedThroughputExceeded`, ...
    pub code: String,
    /// Details, when DynamoDB provides them.
    pub message: Option<String>,
    /// The item as it was, when the operation asked for `ReturnValuesOnConditionCheckFailure=ALL_OLD`.
    pub item: Option<Item>,
}

impl CancellationReason {
    /// The reason of an operation that did not fail.
    pub fn none() -> Self {
        Self { code: "None".into(), message: None, item: None }
    }

    /// Whether this operation failed.
    pub fn failed(&self) -> bool {
        self.code != "None"
    }
}

/// An error returned by DynamoDB (or the in-memory backend).
#[derive(Clone, Debug, PartialEq)]
pub struct ServiceError {
    /// Classification of [`Self::code`].
    pub kind: ErrorKind,
    /// The service error code, e.g. `ConditionalCheckFailedException`.
    pub code: String,
    /// The service's message.
    pub message: String,
    /// The operation that failed (`PutItem`, `Query`, ...).
    pub operation: &'static str,
    /// AWS request id, when known.
    pub request_id: Option<String>,
    /// HTTP status code, when known.
    pub status: Option<u16>,
    /// For `ConditionalCheckFailed` with `ReturnValuesOnConditionCheckFailure=ALL_OLD`: the item.
    pub item: Option<Item>,
    /// For `TransactionCanceled`: one reason per operation of the transaction, in order.
    pub cancellation_reasons: Vec<CancellationReason>,
}

impl ServiceError {
    /// A service error with `code` (classified with [`ErrorKind::from_code`]).
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        let code = code.into();
        Self {
            kind: ErrorKind::from_code(&code),
            code,
            message: message.into(),
            operation: "",
            request_id: None,
            status: Some(400),
            item: None,
            cancellation_reasons: Vec::new(),
        }
    }

    /// `ValidationException` with `message`.
    pub fn validation(message: impl Into<String>) -> Self {
        Self::new("ValidationException", message)
    }

    /// `ConditionalCheckFailedException`, with the old item if it was requested.
    pub fn conditional_check_failed(item: Option<Item>) -> Self {
        Self { item, ..Self::new("ConditionalCheckFailedException", "The conditional request failed") }
    }

    /// `ResourceNotFoundException` with `message`.
    pub fn resource_not_found(message: impl Into<String>) -> Self {
        Self::new("ResourceNotFoundException", message)
    }

    /// `ResourceInUseException` with `message`.
    pub fn resource_in_use(message: impl Into<String>) -> Self {
        Self::new("ResourceInUseException", message)
    }

    /// `TransactionCanceledException` with one reason per operation.
    pub fn transaction_canceled(reasons: Vec<CancellationReason>) -> Self {
        let codes: Vec<&str> = reasons.iter().map(|reason| reason.code.as_str()).collect();
        let message = format!(
            "Transaction cancelled, please refer cancellation reasons for specific reasons [{}]",
            codes.join(", ")
        );
        Self { cancellation_reasons: reasons, ..Self::new("TransactionCanceledException", message) }
    }

    /// Sets the operation name.
    #[must_use]
    pub fn with_operation(mut self, operation: &'static str) -> Self {
        self.operation = operation;
        self
    }

    /// Whether retrying the same request unchanged may succeed.
    ///
    /// A canceled transaction is retryable when every failed operation failed for a transient
    /// reason (conflicts and throttling), never when a condition failed.
    pub fn is_retryable(&self) -> bool {
        if self.kind == ErrorKind::TransactionCanceled {
            let mut failed = self.cancellation_reasons.iter().filter(|reason| reason.failed()).peekable();
            return failed.peek().is_some()
                && failed.all(|reason| {
                    matches!(
                        reason.code.as_str(),
                        "TransactionConflict" | "ThrottlingError" | "ProvisionedThroughputExceeded"
                    )
                });
        }
        self.kind.is_retryable()
    }
}

impl fmt::Display for ServiceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.operation.is_empty() {
            write!(f, "{}: {}", self.code, self.message)
        } else {
            write!(f, "{} failed: {}: {}", self.operation, self.code, self.message)
        }
    }
}

impl From<ServiceError> for Error {
    fn from(error: ServiceError) -> Self {
        Self::Service(Box::new(error))
    }
}

impl Error {
    /// Whether retrying the same operation unchanged may succeed.
    pub fn is_retryable(&self) -> bool {
        match self {
            Self::Service(error) => error.is_retryable(),
            Self::Transport { retryable, .. } => *retryable,
            _ => false,
        }
    }

    /// Whether the request may have been applied although it failed: a timeout, a lost
    /// connection or a 5xx (DynamoDB: "the operation may have succeeded or failed"). DynamoDB
    /// applies nothing of a request it throttles or rejects.
    pub fn may_have_been_applied(&self) -> bool {
        match self {
            Self::Transport { .. } => true,
            Self::Service(error) => error.kind == ErrorKind::Internal,
            _ => false,
        }
    }

    /// A `ValidationException`-style service error (what DynamoDB returns for invalid requests).
    pub fn validation_exception(message: impl Into<String>) -> Self {
        ServiceError::validation(message).into()
    }

    /// The service error, if this is one.
    pub fn as_service(&self) -> Option<&ServiceError> {
        match self {
            Self::Service(error) => Some(error),
            _ => None,
        }
    }

    /// The error kind of a service error.
    pub fn kind(&self) -> Option<ErrorKind> {
        self.as_service().map(|error| error.kind)
    }

    /// Sets the operation of a service error (other errors are returned unchanged).
    #[must_use]
    pub fn with_operation(self, operation: &'static str) -> Self {
        match self {
            Self::Service(mut error) => {
                if error.operation.is_empty() {
                    error.operation = operation;
                }
                Self::Service(error)
            }
            other => other,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_classified() {
        assert_eq!(ErrorKind::from_code("ProvisionedThroughputExceededException"), ErrorKind::Throttling);
        assert_eq!(ErrorKind::from_code("ConditionalCheckFailedException"), ErrorKind::ConditionalCheckFailed);
        assert_eq!(ErrorKind::from_code("MissingAuthenticationTokenException"), ErrorKind::AccessDenied);
        assert_eq!(ErrorKind::from_code("SomethingNew"), ErrorKind::Other);
        assert!(ErrorKind::Throttling.is_retryable());
        assert!(!ErrorKind::Validation.is_retryable());
    }

    #[test]
    fn canceled_transactions_are_retryable_only_for_transient_reasons() {
        let reason = |code: &str| CancellationReason { code: code.into(), message: None, item: None };
        let transient =
            ServiceError::transaction_canceled(vec![CancellationReason::none(), reason("TransactionConflict")]);
        assert!(transient.is_retryable());
        let condition =
            ServiceError::transaction_canceled(vec![reason("ConditionalCheckFailed"), reason("ThrottlingError")]);
        assert!(!condition.is_retryable());
        assert!(transient.message.contains("[None, TransactionConflict]"));
    }

    #[test]
    fn display_includes_the_operation() {
        let error = Error::from(ServiceError::validation("bad").with_operation("PutItem"));
        assert_eq!(error.to_string(), "PutItem failed: ValidationException: bad");
    }
}
