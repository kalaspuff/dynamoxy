use std::collections::BTreeMap;
use std::time::Duration;

use super::*;
use crate::backend::memory::{Fault, FaultKind};
use crate::config::RetryConfig;
use crate::events::EventKind;
use crate::types::{
    AttributeDefinition, BatchExecuteStatementInput, BillingMode, CreateTableInput, DeleteItemInput,
    ExecuteStatementInput, GetItemInput, KeySchema, ParameterizedStatement, PutItemInput, ReturnValues,
    ReturnValuesOnConditionCheckFailure, ScalarType, TransactTarget, TransactWriteItem, TransactWriteItemsInput,
    UpdateItemInput,
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

/// The next call of `operation` runs, and its response is lost.
fn lose(operation: &str) -> Fault {
    Fault { operation: Some(operation.into()), table: None, kind: FaultKind::LoseResponse, times: 1 }
}

/// The next `times` calls of `operation` fail before sending anything (a refused connection).
fn refuse(operation: &str, times: u32) -> Fault {
    Fault { operation: Some(operation.into()), table: None, kind: FaultKind::RefuseConnection, times }
}

async fn stored(client: &Client, key: &str) -> Option<Item> {
    let input = GetItemInput {
        table_name: "items".into(),
        key: item([("pk", AttributeValue::s(key))]),
        consistent_read: true,
        ..Default::default()
    };
    client.get_item(input).await.result.unwrap().item
}

fn update(key: &str, expression: &str, value: AttributeValue) -> UpdateItemInput {
    UpdateItemInput {
        table_name: "items".into(),
        key: item([("pk", AttributeValue::s(key))]),
        update_expression: Some(expression.into()),
        expression_attribute_values: BTreeMap::from([(":v".to_owned(), value)]),
        ..Default::default()
    }
}

fn unknown_item<T: std::fmt::Debug>(call: &Call<T>) -> Option<Item> {
    match &call.result {
        Err(Error::OutcomeUnknown { item, .. }) => item.clone(),
        other => panic!("expected an unknown outcome, got {other:?}"),
    }
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
    assert_eq!(call.stats.requests, 1, "one request, sent three times");
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
async fn a_failed_condition_after_an_attempt_that_may_have_been_applied_is_an_unknown_outcome() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    let create = |key: &str| PutItemInput {
        condition_expression: Some("attribute_not_exists(pk)".into()),
        return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
        ..put(key)
    };
    // The first attempt creates the item and its response is lost; the second one finds the item
    // the first one created.
    backend.inject_fault(lose("PutItem"));
    kinds(&client);
    let call = client.put_item(create("a")).await;
    assert_eq!(unknown_item(&call), Some(item([("pk", AttributeValue::s("a"))])));
    assert_eq!(call.stats.attempts, 2);
    assert!(kinds(&client).contains(&EventKind::RequestOutcomeUnknown));
    assert!(stored(&client, "a").await.is_some());

    // Without a lost response, and after throttling (which applies nothing), the failure stands.
    let call = client.put_item(create("a")).await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::ConditionalCheckFailed));
    backend.inject_fault(throttle("PutItem", 1));
    let call = client.put_item(create("a")).await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::ConditionalCheckFailed));
    assert_eq!(call.stats.attempts, 2);

    // A 5xx may have been applied too ("the operation may have succeeded or failed").
    backend.inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Error { code: "InternalServerError".into(), message: "internal".into() },
        times: 1,
    });
    let call = client.put_item(create("a")).await;
    assert!(matches!(call.result, Err(Error::OutcomeUnknown { .. })), "{:?}", call.result);
}

#[tokio::test(start_paused = true)]
async fn updates_that_would_apply_twice_are_not_sent_again() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    client
        .put_item(PutItemInput {
            item: item([("pk", AttributeValue::s("a")), ("n", AttributeValue::n(0))]),
            ..put("a")
        })
        .await
        .result
        .unwrap();
    backend.inject_fault(lose("UpdateItem"));
    let call = client.update_item(update("a", "ADD n :v", AttributeValue::n(1))).await;
    assert_eq!(unknown_item(&call), None);
    assert_eq!(call.stats.attempts, 1, "not sent again");
    assert_eq!(stored(&client, "a").await.unwrap()["n"], AttributeValue::n(1), "applied once");

    backend.inject_fault(lose("UpdateItem"));
    let call = client.update_item(update("a", "SET label = :v", AttributeValue::s("x"))).await;
    assert!(call.result.is_ok(), "an idempotent update is sent again");
    assert_eq!(call.stats.attempts, 2);
}

#[tokio::test(start_paused = true)]
async fn old_values_after_an_attempt_that_may_have_been_applied_are_unknown() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    let versioned = |version: i64| item([("pk", AttributeValue::s("b")), ("v", AttributeValue::n(version))]);
    client.put_item(PutItemInput { item: versioned(1), ..put("b") }).await.result.unwrap();

    // The second attempt returns, as the old item, what the first one wrote.
    backend.inject_fault(lose("PutItem"));
    let call =
        client.put_item(PutItemInput { item: versioned(2), return_values: ReturnValues::AllOld, ..put("b") }).await;
    assert_eq!(unknown_item(&call), Some(versioned(2)));

    // New values are the same however often an idempotent update was applied.
    backend.inject_fault(lose("UpdateItem"));
    let new =
        UpdateItemInput { return_values: ReturnValues::AllNew, ..update("b", "SET v = :v", AttributeValue::n(3)) };
    assert_eq!(client.update_item(new).await.result.unwrap().attributes, Some(versioned(3)));

    // The first attempt deleted the item: the second one finds nothing to return.
    backend.inject_fault(lose("DeleteItem"));
    let delete = DeleteItemInput {
        table_name: "items".into(),
        key: item([("pk", AttributeValue::s("b"))]),
        return_values: ReturnValues::AllOld,
        ..Default::default()
    };
    let call = client.delete_item(delete).await;
    assert_eq!(unknown_item(&call), None);
    assert!(stored(&client, "b").await.is_none());
}

#[tokio::test(start_paused = true)]
async fn transactions_are_sent_again_with_their_token() {
    let client = client_with_table(config()).await;
    client.memory_backend().unwrap().inject_fault(lose("TransactWriteItems"));
    let target = TransactTarget {
        table_name: "items".into(),
        condition_expression: Some("attribute_not_exists(pk)".into()),
        ..Default::default()
    };
    let input = TransactWriteItemsInput {
        transact_items: vec![TransactWriteItem::Put { target, item: item([("pk", AttributeValue::s("t"))]) }],
        ..Default::default()
    };
    let call = client.transact_write_items(input).await;
    assert!(call.result.is_ok(), "the token makes the second attempt return the first one's success");
    assert_eq!(call.stats.attempts, 2);
}

#[tokio::test(start_paused = true)]
async fn partiql_writes_follow_the_same_rules() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    let statement = |text: &str| ExecuteStatementInput { statement: text.into(), ..Default::default() };

    backend.inject_fault(lose("ExecuteStatement"));
    let call = client.execute_statement(statement("INSERT INTO items VALUE {'pk': 'p', 'n': 0}")).await;
    unknown_item(&call);
    assert_eq!(call.stats.attempts, 2, "an INSERT is sent again; its DuplicateItem settles nothing");

    backend.inject_fault(lose("ExecuteStatement"));
    let call = client.execute_statement(statement("UPDATE items SET n = n + 1 WHERE pk = 'p'")).await;
    unknown_item(&call);
    assert_eq!(call.stats.attempts, 1);
    assert_eq!(stored(&client, "p").await.unwrap()["n"], AttributeValue::n(1));

    backend.inject_fault(lose("ExecuteStatement"));
    let call = client.execute_statement(statement("SELECT * FROM items WHERE pk = 'p'")).await;
    assert_eq!(call.result.unwrap().items.len(), 1);

    backend.inject_fault(lose("BatchExecuteStatement"));
    let insert =
        ParameterizedStatement { statement: "INSERT INTO items VALUE {'pk': 'q'}".into(), ..Default::default() };
    let call = client
        .batch_execute_statement(BatchExecuteStatementInput { statements: vec![insert], ..Default::default() })
        .await;
    unknown_item(&call);
    assert_eq!(call.stats.attempts, 1, "a batch with writes is not sent again");
}

fn fail(operation: &str, code: &str, times: u32) -> Fault {
    Fault {
        operation: Some(operation.into()),
        table: None,
        kind: FaultKind::Error { code: code.into(), message: "injected".into() },
        times,
    }
}

#[tokio::test(start_paused = true)]
async fn writes_that_run_out_of_attempts_after_one_that_may_have_been_applied_are_unknown() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();
    let create = PutItemInput { condition_expression: Some("attribute_not_exists(pk)".into()), ..put("a") };

    // The first attempt creates the item and its response is lost; the others are throttled.
    backend.inject_fault(lose("PutItem"));
    backend.inject_fault(throttle("PutItem", 2));
    kinds(&client);
    let call = client.put_item(create.clone()).await;
    assert_eq!(unknown_item(&call), None);
    assert_eq!(call.stats.attempts, 3);
    assert!(kinds(&client).ends_with(&[EventKind::RequestGaveUp, EventKind::RequestOutcomeUnknown]));
    assert!(stored(&client, "a").await.is_some());

    // A transaction whose first attempt is still being applied (DynamoDB answers the same token
    // with TransactionInProgressException) is applied, whatever the later attempts say.
    backend.inject_fault(lose("TransactWriteItems"));
    backend.inject_fault(fail("TransactWriteItems", "TransactionInProgressException", 2));
    let target = TransactTarget { table_name: "items".into(), ..Default::default() };
    let input = TransactWriteItemsInput {
        transact_items: vec![TransactWriteItem::Put { target, item: item([("pk", AttributeValue::s("t"))]) }],
        ..Default::default()
    };
    let call = client.transact_write_items(input).await;
    assert!(matches!(call.result, Err(Error::OutcomeUnknown { .. })), "{:?}", call.result);
    assert!(stored(&client, "t").await.is_some());

    // The last attempt may have been applied too: a 5xx on an update that is not sent again is
    // unknown whichever attempt gets it, also without retries.
    let add = || update("a", "ADD n :v", AttributeValue::n(1));
    backend.inject_fault(throttle("UpdateItem", 2));
    backend.inject_fault(fail("UpdateItem", "InternalServerError", 1));
    let call = client.update_item(add()).await;
    assert!(matches!(call.result, Err(Error::OutcomeUnknown { .. })), "{:?}", call.result);
    assert_eq!(call.stats.attempts, 3);
    let once =
        client_with_table(ClientConfig { retry: RetryConfig { max_attempts: 1, ..config().retry }, ..config() }).await;
    once.memory_backend().unwrap().inject_fault(fail("UpdateItem", "InternalServerError", 1));
    let call = once.update_item(add()).await;
    assert!(matches!(call.result, Err(Error::OutcomeUnknown { .. })), "{:?}", call.result);

    // Throttling alone applies nothing, and reads have no outcome to lose.
    backend.inject_fault(throttle("PutItem", 3));
    let call = client.put_item(put("b")).await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::Throttling));
    backend.inject_fault(lose("GetItem"));
    backend.inject_fault(throttle("GetItem", 2));
    let call = client.get_item(GetItemInput { table_name: "items".into(), key: put("a").item, ..Default::default() });
    assert_eq!(call.await.result.unwrap_err().kind(), Some(ErrorKind::Throttling));
}

#[tokio::test(start_paused = true)]
async fn calls_in_progress_finish_their_retries_after_close() {
    let client = client_with_table(config()).await;
    // The first attempt takes longer than the timeout; the client is closed meanwhile.
    client.memory_backend().unwrap().inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Latency(Duration::from_secs(5)),
        times: 1,
    });
    let call = tokio::spawn({
        let client = client.clone();
        async move { client.put_item(put("a")).await }
    });
    tokio::time::sleep(Duration::from_millis(500)).await;
    client.close();
    let call = call.await.unwrap();
    assert!(call.result.is_ok(), "{:?}", call.result);
    assert_eq!(call.stats.attempts, 2);
    assert_eq!(client.put_item(put("b")).await.result.unwrap_err(), Error::Shutdown, "new calls are refused");
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

#[tokio::test(start_paused = true)]
async fn partiql_writes_count_write_capacity() {
    let client = client_with_table(config()).await;
    let statement = |text: &str| ExecuteStatementInput {
        statement: text.into(),
        return_consumed_capacity: ReturnConsumedCapacity::Total,
        ..Default::default()
    };
    for key in ["a", "b", "c"] {
        let call = client.execute_statement(statement(&format!("INSERT INTO items VALUE {{'pk': '{key}'}}"))).await;
        call.result.unwrap();
    }
    client.execute_statement(statement("SELECT * FROM items WHERE pk = 'a'")).await.result.unwrap();
    let insert = |key: &str| ParameterizedStatement {
        statement: format!("INSERT INTO items VALUE {{'pk': '{key}'}}"),
        ..Default::default()
    };
    let batch = BatchExecuteStatementInput {
        statements: vec![insert("d"), insert("e")],
        return_consumed_capacity: ReturnConsumedCapacity::Total,
    };
    client.batch_execute_statement(batch).await.result.unwrap();
    let snapshot = client.metrics().snapshot();
    let units = |name: &str| {
        let (_, metrics) = snapshot.operations.iter().find(|(operation, _)| *operation == name).unwrap();
        (metrics.read_capacity_units, metrics.write_capacity_units)
    };
    assert_eq!(units("ExecuteStatement"), (0.5, 3.0), "three INSERTs and an eventually consistent SELECT");
    assert_eq!(units("BatchExecuteStatement"), (0.0, 2.0));
}

#[test]
fn capacity_without_a_split_counts_as_the_request_says() {
    let total = |units: f64| ConsumedCapacity { table_name: "t".into(), capacity_units: units, ..Default::default() };
    assert_eq!(split(&[total(2.0)], true), (0.0, 2.0));
    assert_eq!(split(&[total(2.0)], false), (2.0, 0.0));
    let reported =
        ConsumedCapacity { capacity_units: 3.0, read_capacity_units: 1.0, write_capacity_units: 2.0, ..total(0.0) };
    assert_eq!(split(std::slice::from_ref(&reported), false), (1.0, 2.0), "a reported split is kept");
    assert_eq!(split(&[reported, total(1.0)], true), (1.0, 3.0));
}

#[tokio::test(start_paused = true)]
async fn writes_that_were_never_sent_are_sent_again() {
    let client = client_with_table(config()).await;
    let backend = client.memory_backend().unwrap();

    // An increment would apply twice if an attempt that reached DynamoDB were sent again; one
    // that never left the process is safe to send again, and applies once.
    backend.inject_fault(refuse("UpdateItem", 2));
    let call = client.update_item(update("a", "ADD n :v", AttributeValue::n(1))).await;
    assert!(call.result.is_ok(), "{:?}", call.result);
    assert_eq!(call.stats.attempts, 3);
    assert_eq!(stored(&client, "a").await.unwrap()["n"], AttributeValue::n(1));

    // A failed condition after such attempts is the write's own answer, not an unknown outcome.
    backend.inject_fault(refuse("PutItem", 1));
    let call = client
        .put_item(PutItemInput { condition_expression: Some("attribute_not_exists(pk)".into()), ..put("a") })
        .await;
    assert_eq!(call.result.unwrap_err().kind(), Some(ErrorKind::ConditionalCheckFailed));

    // When every attempt fails to connect, the write was not applied: the last attempt's
    // transport error, retryable, and no unknown outcome.
    kinds(&client);
    backend.inject_fault(refuse("UpdateItem", 3));
    let call = client.update_item(update("a", "ADD n :v", AttributeValue::n(1))).await;
    let error = call.result.unwrap_err();
    assert!(matches!(error, Error::Transport { sent: false, .. }), "{error:?}");
    assert!(error.is_retryable() && !error.may_have_been_applied());
    assert_eq!(call.stats.attempts, 3);
    assert!(!kinds(&client).contains(&EventKind::RequestOutcomeUnknown));
    assert_eq!(stored(&client, "a").await.unwrap()["n"], AttributeValue::n(1));

    // An attempt that was sent before them still leaves the outcome unknown.
    backend.inject_fault(lose("PutItem"));
    backend.inject_fault(refuse("PutItem", 1));
    let call = client
        .put_item(PutItemInput { condition_expression: Some("attribute_not_exists(pk)".into()), ..put("b") })
        .await;
    assert!(matches!(call.result, Err(Error::OutcomeUnknown { .. })), "{:?}", call.result);
}
