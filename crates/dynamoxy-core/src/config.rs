//! Client configuration, deserialized from the dictionaries that the Python layer builds
//! (`to_core()`): field names are the Python names, durations are float seconds.

use std::fmt;
use std::time::Duration;

use serde::Deserialize;

use crate::error::{Error, Result};

/// How failed requests are retried.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct RetryConfig {
    /// Attempts per request, including the first (1 disables retries).
    pub max_attempts: u32,
    /// Delay before the first retry, in seconds (doubles each time, with full jitter).
    pub base_delay: f64,
    /// Longest delay between attempts, in seconds.
    pub max_delay: f64,
    /// Retry requests that timed out or lost their connection. Such a write may already have
    /// been applied: writes that would apply twice are not sent again (see [`crate::client`]).
    pub retry_timeouts: bool,
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self { max_attempts: 4, base_delay: 0.05, max_delay: 5.0, retry_timeouts: true }
    }
}

/// Client settings.
#[derive(Clone, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct ClientConfig {
    /// AWS region (default: the region of the AWS configuration chain).
    pub region: Option<String>,
    /// Endpoint URL (DynamoDB Local, MiniStack, VPC endpoints, ...).
    pub endpoint_url: Option<String>,
    /// Named profile of the AWS configuration files.
    pub profile: Option<String>,
    /// Static access key id (default: the AWS credential chain).
    pub access_key_id: Option<String>,
    /// Static secret access key.
    pub secret_access_key: Option<String>,
    /// Static session token.
    pub session_token: Option<String>,
    /// Time limit for establishing a connection, in seconds.
    pub connect_timeout: f64,
    /// Time limit of one attempt of a request, in seconds.
    pub timeout: f64,
    /// Retry policy.
    pub retry: RetryConfig,
    /// Rounds of `BatchWriteItem`/`BatchGetItem` for unprocessed items before giving up.
    pub batch_max_rounds: u32,
    /// Requests of one batch operation sent in parallel.
    pub batch_concurrency: usize,
    /// Operations slower than this (seconds) emit `operation.slow`; 0 disables it.
    pub slow_operation_threshold: f64,
    /// Events buffered for the Python layer before new ones are dropped.
    pub event_capacity: usize,
}

impl Default for ClientConfig {
    fn default() -> Self {
        Self {
            region: None,
            endpoint_url: None,
            profile: None,
            access_key_id: None,
            secret_access_key: None,
            session_token: None,
            connect_timeout: 3.1,
            timeout: 10.0,
            retry: RetryConfig::default(),
            batch_max_rounds: 8,
            batch_concurrency: 4,
            slow_operation_threshold: 0.0,
            event_capacity: 10_000,
        }
    }
}

impl fmt::Debug for ClientConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let redact = |value: &Option<String>| value.as_ref().map(|_| "<redacted>");
        f.debug_struct("ClientConfig")
            .field("region", &self.region)
            .field("endpoint_url", &self.endpoint_url)
            .field("profile", &self.profile)
            .field("access_key_id", &self.access_key_id)
            .field("secret_access_key", &redact(&self.secret_access_key))
            .field("session_token", &redact(&self.session_token))
            .field("connect_timeout", &self.connect_timeout)
            .field("timeout", &self.timeout)
            .field("retry", &self.retry)
            .field("batch_max_rounds", &self.batch_max_rounds)
            .field("batch_concurrency", &self.batch_concurrency)
            .field("slow_operation_threshold", &self.slow_operation_threshold)
            .field("event_capacity", &self.event_capacity)
            .finish()
    }
}

fn positive(name: &str, value: f64) -> Result<()> {
    if value.is_finite() && value > 0.0 {
        Ok(())
    } else {
        Err(Error::Config(format!("{name} must be a positive number of seconds, got {value}")))
    }
}

impl ClientConfig {
    /// Checks every field. Messages name the field and what is allowed.
    pub fn validate(&self) -> Result<()> {
        positive("timeout", self.timeout)?;
        positive("connect_timeout", self.connect_timeout)?;
        if self.retry.max_attempts == 0 {
            return Err(Error::Config("retry.max_attempts must be at least 1 (1 disables retries)".into()));
        }
        if !(self.retry.base_delay.is_finite() && self.retry.base_delay >= 0.0) {
            return Err(Error::Config(format!(
                "retry.base_delay must be a non-negative number of seconds, got {}",
                self.retry.base_delay
            )));
        }
        if !(self.retry.max_delay.is_finite() && self.retry.max_delay >= self.retry.base_delay) {
            return Err(Error::Config(format!(
                "retry.max_delay must be a number of seconds >= retry.base_delay, got {}",
                self.retry.max_delay
            )));
        }
        if self.batch_max_rounds == 0 {
            return Err(Error::Config("batch_max_rounds must be at least 1".into()));
        }
        if self.batch_concurrency == 0 {
            return Err(Error::Config("batch_concurrency must be at least 1".into()));
        }
        if !(self.slow_operation_threshold.is_finite() && self.slow_operation_threshold >= 0.0) {
            return Err(Error::Config(format!(
                "slow_operation_threshold must be a non-negative number of seconds, got {}",
                self.slow_operation_threshold
            )));
        }
        if self.event_capacity == 0 {
            return Err(Error::Config("event_capacity must be at least 1".into()));
        }
        if self.access_key_id.is_some() != self.secret_access_key.is_some() {
            return Err(Error::Config("access_key_id and secret_access_key must be given together".into()));
        }
        if self.session_token.is_some() && self.access_key_id.is_none() {
            return Err(Error::Config("session_token requires access_key_id and secret_access_key".into()));
        }
        if let Some(endpoint) = &self.endpoint_url
            && !(endpoint.starts_with("http://") || endpoint.starts_with("https://"))
        {
            return Err(Error::Config(format!("endpoint_url must start with http:// or https://, got {endpoint:?}")));
        }
        Ok(())
    }

    /// [`Self::timeout`] as a duration.
    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.timeout)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_validate_and_bad_values_are_rejected() {
        assert_eq!(ClientConfig::default().validate(), Ok(()));
        let config = ClientConfig { timeout: -1.0, ..ClientConfig::default() };
        assert!(config.validate().unwrap_err().to_string().contains("timeout must be a positive"));
        let config = ClientConfig { access_key_id: Some("a".into()), ..ClientConfig::default() };
        assert!(config.validate().unwrap_err().to_string().contains("given together"));
        let config = ClientConfig { endpoint_url: Some("localhost:4566".into()), ..ClientConfig::default() };
        assert!(config.validate().unwrap_err().to_string().contains("http://"));
    }

    #[test]
    fn secrets_are_not_printed() {
        let config = ClientConfig {
            access_key_id: Some("AKIA".into()),
            secret_access_key: Some("very-secret".into()),
            ..ClientConfig::default()
        };
        let printed = format!("{config:?}");
        assert!(printed.contains("AKIA") && !printed.contains("very-secret"));
    }
}
