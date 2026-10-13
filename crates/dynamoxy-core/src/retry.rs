//! Exponential backoff with full jitter ("Exponential Backoff and Jitter", AWS Architecture Blog).

use std::time::Duration;

use crate::config::RetryConfig;

/// The delay before retry number `retry` (1 for the first retry): a random duration between zero
/// and `min(max_delay, base_delay * 2^(retry - 1))`.
pub fn backoff(config: &RetryConfig, retry: u32) -> Duration {
    let exponent = retry.saturating_sub(1).min(30);
    let ceiling = (config.base_delay * f64::from(1u32 << exponent)).min(config.max_delay);
    Duration::from_secs_f64(ceiling * fastrand::f64())
}

/// Like [`backoff`], but never zero (for polling loops and unprocessed batch items, where an
/// immediate resend tends to be rejected again).
pub fn backoff_at_least(config: &RetryConfig, retry: u32, minimum: Duration) -> Duration {
    backoff(config, retry).max(minimum)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delays_grow_and_are_capped() {
        let config = RetryConfig { max_attempts: 10, base_delay: 0.1, max_delay: 1.0, retry_timeouts: true };
        for retry in 1..=20 {
            let delay = backoff(&config, retry).as_secs_f64();
            let ceiling = (0.1 * 2f64.powi(retry as i32 - 1)).min(1.0);
            assert!(delay <= ceiling + 1e-9, "retry {retry}: {delay} > {ceiling}");
        }
        assert!(backoff_at_least(&config, 1, Duration::from_millis(5)) >= Duration::from_millis(5));
        let zero = RetryConfig { base_delay: 0.0, max_delay: 0.0, ..config };
        assert_eq!(backoff(&zero, 3), Duration::ZERO);
    }
}
