//! Classification of SDK errors: the one place where AWS SDK errors become [`crate::Error`].
//!
//! Service errors keep DynamoDB's code, message, request id and HTTP status; conditional check
//! failures keep the returned item and canceled transactions their reasons. Timeouts and I/O
//! failures are retryable transport errors; credential problems are [`Error::Credentials`].

use aws_sdk_dynamodb::error::{DisplayErrorContext, ProvideErrorMetadata, SdkError};
use aws_sdk_dynamodb::operation::RequestId;
use aws_sdk_dynamodb::types as sdk;

use super::convert::optional_item;
use crate::error::{CancellationReason, Error, ErrorKind, ServiceError};
use crate::value::Item;

/// Details some error codes carry.
pub enum Extra {
    /// Nothing beyond code and message.
    None,
    /// `ConditionalCheckFailedException`: the item, when `ALL_OLD` was requested.
    Item(Option<Item>),
    /// `TransactionCanceledException`: one reason per operation.
    Reasons(Vec<CancellationReason>),
}

/// The item of a `ConditionalCheckFailedException`.
pub fn condition_item(error: &sdk::error::ConditionalCheckFailedException) -> Extra {
    Extra::Item(optional_item(error.item().cloned()).ok().flatten())
}

/// The reasons of a `TransactionCanceledException`.
///
/// Codes are DynamoDB's documented reason codes (`ConditionalCheckFailed`, `DuplicateItem`, ...);
/// an `Exception` suffix that some emulators add is removed.
pub fn cancellation_reasons(error: &sdk::error::TransactionCanceledException) -> Extra {
    Extra::Reasons(
        error
            .cancellation_reasons()
            .iter()
            .map(|reason| CancellationReason {
                code: reason_code(reason.code()),
                message: reason.message().map(str::to_owned),
                item: optional_item(reason.item().cloned()).ok().flatten(),
            })
            .collect(),
    )
}

fn reason_code(code: Option<&str>) -> String {
    let code = code.unwrap_or("None");
    code.strip_suffix("Exception").filter(|stripped| !stripped.is_empty()).unwrap_or(code).to_owned()
}

fn mentions_credentials(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("credential") || lower.contains("no identity") || lower.contains("identity resolver")
}

/// Converts an SDK error. `extra` extracts code-specific details from the modeled error.
pub fn map_error<E, R>(operation: &'static str, error: SdkError<E, R>, extra: impl FnOnce(&E) -> Extra) -> Error
where
    E: ProvideErrorMetadata + RequestId + std::error::Error + Send + Sync + 'static,
    R: std::fmt::Debug + HttpStatus,
{
    // The failure structs inside SdkError do not implement Error; render the whole error.
    let rendered = DisplayErrorContext(&error).to_string();
    match error {
        SdkError::ServiceError(context) => {
            let status = context.raw().status_code();
            let error = context.err();
            let code = error.code().unwrap_or("UnknownError").to_owned();
            let message = error.message().unwrap_or_default().to_owned();
            let mut service = ServiceError::new(code, message).with_operation(operation);
            service.status = Some(status);
            service.request_id = error.request_id().map(str::to_owned);
            if service.kind == ErrorKind::Other && status >= 500 {
                service.kind = ErrorKind::Internal;
            }
            match extra(error) {
                Extra::None => {}
                Extra::Item(item) => service.item = item,
                Extra::Reasons(reasons) => service.cancellation_reasons = reasons,
            }
            service.into()
        }
        SdkError::TimeoutError(_) => {
            Error::Transport { operation, message: "the request timed out".into(), retryable: true }
        }
        SdkError::DispatchFailure(failure) => {
            if mentions_credentials(&rendered) {
                Error::Credentials(rendered)
            } else {
                let retryable = failure.is_io() || failure.is_timeout() || failure.is_other();
                Error::Transport { operation, message: rendered, retryable }
            }
        }
        SdkError::ResponseError(_) => {
            Error::Transport { operation, message: format!("could not read the response: {rendered}"), retryable: true }
        }
        SdkError::ConstructionFailure(_) => {
            if mentions_credentials(&rendered) {
                Error::Credentials(rendered)
            } else {
                Error::Validation(rendered)
            }
        }
        _ => Error::Transport { operation, message: rendered, retryable: false },
    }
}

/// The HTTP status of a raw response (abstracted for testing).
pub trait HttpStatus {
    /// The status code.
    fn status_code(&self) -> u16;
}

impl HttpStatus for aws_sdk_dynamodb::config::http::HttpResponse {
    fn status_code(&self) -> u16 {
        self.status().as_u16()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_messages_are_recognized() {
        assert!(mentions_credentials("failed to load credentials from the environment"));
        assert!(mentions_credentials("no identity resolver was configured"));
        assert!(!mentions_credentials("connection refused"));
    }

    #[test]
    fn reason_codes_use_the_documented_names() {
        assert_eq!(reason_code(None), "None");
        assert_eq!(reason_code(Some("ConditionalCheckFailed")), "ConditionalCheckFailed");
        assert_eq!(reason_code(Some("DuplicateItemException")), "DuplicateItem");
        assert_eq!(reason_code(Some("Exception")), "Exception");
    }
}
