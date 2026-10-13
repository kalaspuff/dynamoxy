use std::collections::BTreeMap;
use std::time::Duration;

use super::*;
use crate::backend::memory::{Fault, FaultKind};
use crate::config::RetryConfig;
use crate::events::EventKind;
use crate::types::{
    AttributeDefinition, BillingMode, CreateTableInput, GetItemInput, KeySchema, PutItemInput, ScalarType,
};
use crate::value::{AttributeValue, item};

fn config() -> ClientConfig {
    ClientConfig {
        retry: RetryConfig { max_attempts: 3, base_delay: 0.01, max_delay: 0.1, retry_timeouts: true },
        timeout: 1.0,
        ..ClientConfig::default()
    }
}

async fn client_with_table(config: ClientConfig) -> Client {
    let client = Client::memory(config, None).unwrap();
    client.events().set_enabled(EventKind::ALL);
    client
        .create_table(CreateTableInput {
            table_name: "items".into(),
            attribute_definitions: vec![AttributeDefinition { name: "pk".into(), attribute_type: ScalarType::S }],
            key_schema: KeySchema { partition_key: "pk".into(), sort_key: None },
            local_secondary_indexes: vec![],
            global_secondary_indexes: vec![],
            billing_mode: BillingMode::PayPerRequest,
            provisioned_throughput: None,
            on_demand_throughput: None,
            stream_specification: None,
            sse_specification: None,
            tags: BTreeMap::new(),
            table_class: None,
            deletion_protection_enabled: false,
        })
        .await
        .result
        .unwrap();
    client
}

fn throttle(operation: &str, times: u32) -> Fault {
    Fault {
        operation: Some(operation.into()),
        table: None,
        kind: FaultKind::Error { code: "ProvisionedThroughputExceededException".into(), message: "slow down".into() },
        times,
    }
}

fn put(key: &str) -> PutItemInput {
    PutItemInput { table_name: "items".into(), item: item([("pk", AttributeValue::s(key))]), ..Default::default() }
}

fn kinds(client: &Client) -> Vec<EventKind> {
    client.events().drain(1000).into_iter().map(|event| event.kind).collect()
}

#[tokio::test(start_paused = true)]
async fn throttled_requests_are_retried_with_events_and_metrics() {
    let client = client_with_table(config()).await;
    kinds(&client);
    client.memory_backend().unwrap().inject_fault(throttle("PutItem", 2));
    let call = client.put_item(put("a")).await;
    assert!(call.result.is_ok());
    assert_eq!((call.stats.attempts, call.stats.throttles), (3, 2));
    assert_eq!(
        kinds(&client),
        [EventKind::RequestThrottled, EventKind::RequestRetry, EventKind::RequestThrottled, EventKind::RequestRetry]
    );
    let snapshot = client.metrics().snapshot();
    let (_, put_metrics) = snapshot.operations.iter().find(|(name, _)| *name == "PutItem").unwrap();
    assert_eq!((put_metrics.calls, put_metrics.attempts, put_metrics.throttles, put_metrics.errors), (1, 3, 2, 0));
}

#[tokio::test(start_paused = true)]
async fn retries_stop_at_max_attempts() {
    let client = client_with_table(config()).await;
    kinds(&client);
    client.memory_backend().unwrap().inject_fault(throttle("PutItem", 10));
    let call = client.put_item(put("a")).await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::Throttling));
    assert_eq!(call.stats.attempts, 3);
    assert_eq!(kinds(&client).last(), Some(&EventKind::RequestGaveUp));
}

#[tokio::test(start_paused = true)]
async fn non_retryable_errors_fail_at_once() {
    let client = client_with_table(config()).await;
    let call = client
        .get_item(GetItemInput { table_name: "missing".into(), key: item([("pk", "x".into())]), ..Default::default() })
        .await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::ResourceNotFound));
    assert_eq!(call.stats.attempts, 1);
}

#[tokio::test(start_paused = true)]
async fn attempts_time_out_and_timeouts_are_retried() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    backend.inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Latency(Duration::from_secs(5)),
        times: 1,
    });
    let call = client.put_item(put("a")).await;
    assert!(call.result.is_ok(), "the second attempt succeeds");
    assert_eq!(call.stats.attempts, 2);

    let strict =
        client_with_table(ClientConfig { retry: RetryConfig { retry_timeouts: false, ..config().retry }, ..config() })
            .await;
    strict.memory_backend().unwrap().inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Latency(Duration::from_secs(5)),
        times: 1,
    });
    let call = strict.put_item(put("a")).await;
    assert!(matches!(call.result, Err(Error::Transport { .. })));
    assert_eq!(call.stats.attempts, 1);
}

#[tokio::test(start_paused = true)]
async fn slow_requests_emit_an_event() {
    let client = client_with_table(ClientConfig { slow_operation_threshold: 0.5, ..config() }).await;
    kinds(&client);
    client.memory_backend().unwrap().inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Latency(Duration::from_millis(700)),
        times: 1,
    });
    assert!(client.put_item(put("a")).await.result.is_ok());
    assert_eq!(kinds(&client), [EventKind::RequestSlow]);
}

#[tokio::test(start_paused = true)]
async fn managed_batches_chunk_and_resend_unprocessed_items() {
    let client = client_with_table(ClientConfig { batch_concurrency: 2, ..config() }).await;
    kinds(&client);
    client.memory_backend().unwrap().inject_fault(Fault {
        operation: Some("BatchWriteItem".into()),
        table: None,
        kind: FaultKind::Unprocessed,
        times: 2,
    });
    let writes: Vec<(String, WriteRequest)> = (0..60)
        .map(|index| ("items".to_owned(), WriteRequest::Put(item([("pk", AttributeValue::s(format!("k{index}")))]))))
        .collect();
    let call = client.batch_write(writes, ReturnConsumedCapacity::Total).await;
    let result = call.result.unwrap();
    assert!(result.unprocessed.is_empty());
    assert_eq!(call.stats.requests, 5, "3 chunks, 2 of them sent twice because of unprocessed items");
    assert_eq!(result.consumed_capacity[0].capacity_units, 60.0);
    assert_eq!(kinds(&client), [EventKind::BatchUnprocessed, EventKind::BatchUnprocessed]);

    let keys: Vec<Item> = (0..150).map(|index| item([("pk", AttributeValue::s(format!("k{index}")))])).collect();
    let request = BTreeMap::from([("items".to_owned(), KeysAndAttributes { keys, ..Default::default() })]);
    let call = client.batch_get(request, ReturnConsumedCapacity::None).await;
    assert_eq!(call.result.unwrap().items["items"].len(), 60, "keys that do not exist are not returned");
    assert_eq!(call.stats.requests, 2, "150 keys need two requests");
}

#[tokio::test(start_paused = true)]
async fn batches_report_items_still_unprocessed_after_the_last_round() {
    let client = client_with_table(ClientConfig { batch_max_rounds: 2, ..config() }).await;
    kinds(&client);
    client.memory_backend().unwrap().inject_fault(Fault {
        operation: Some("BatchWriteItem".into()),
        table: None,
        kind: FaultKind::Unprocessed,
        times: 5,
    });
    let writes = vec![("items".to_owned(), WriteRequest::Put(item([("pk", AttributeValue::s("a"))])))];
    let result = client.batch_write(writes, ReturnConsumedCapacity::None).await.result.unwrap();
    assert_eq!(result.unprocessed.len(), 1);
    assert_eq!(kinds(&client), [EventKind::BatchUnprocessed, EventKind::BatchIncomplete]);
}

#[tokio::test(start_paused = true)]
async fn closed_clients_refuse_calls() {
    let client = client_with_table(config()).await;
    client.close();
    assert!(client.is_closed());
    assert_eq!(client.put_item(put("a")).await.result.unwrap_err(), Error::Shutdown);
}

#[tokio::test(start_paused = true)]
async fn waiting_for_tables() {
    let client = client_with_table(config()).await;
    let description = client.wait_for_table("items", true, Duration::from_secs(5)).await.unwrap();
    assert_eq!(description.unwrap().table_status, crate::types::TableStatus::Active);
    let error = client.wait_for_table("missing", true, Duration::from_secs(1)).await.unwrap_err();
    assert!(error.to_string().contains("did not become ACTIVE within 1 seconds"));
    client.delete_table("items").await.result.unwrap();
    assert!(client.wait_for_table("items", false, Duration::from_secs(1)).await.unwrap().is_none());
}

#[test]
fn invalid_config_is_rejected_before_any_io() {
    let error = Client::memory(ClientConfig { event_capacity: 0, ..ClientConfig::default() }, None).unwrap_err();
    assert_eq!(error, Error::Config("event_capacity must be at least 1".into()));
}

#[test]
fn request_tokens_look_like_uuids() {
    let token = request_token();
    assert_eq!(token.len(), 36);
    assert_eq!(token.matches('-').count(), 4);
}
