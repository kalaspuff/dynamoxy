//! Behaviour of the in-memory backend. Each test documents a DynamoDB rule the backend models.

use std::collections::BTreeMap;
use std::time::Duration;

use super::{Fault, FaultKind, MemoryBackend};
use crate::backend::Backend;
use crate::error::{Error, ErrorKind};
use crate::expr::{Names, Values};
use crate::types::*;
use crate::value::{AttributeValue, Item, item};

fn s(value: &str) -> AttributeValue {
    AttributeValue::s(value)
}

fn n(value: i64) -> AttributeValue {
    AttributeValue::n(value)
}

fn values(pairs: &[(&str, AttributeValue)]) -> Values {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), value.clone())).collect()
}

fn names(pairs: &[(&str, &str)]) -> Names {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), (*value).to_owned())).collect()
}

fn definition(name: &str, kind: ScalarType) -> AttributeDefinition {
    AttributeDefinition { name: name.into(), attribute_type: kind }
}

fn keys(partition: &str, sort: Option<&str>) -> KeySchema {
    KeySchema { partition_key: partition.into(), sort_key: sort.map(str::to_owned) }
}

fn table_input(name: &str) -> CreateTableInput {
    CreateTableInput {
        table_name: name.into(),
        attribute_definitions: vec![
            definition("pk", ScalarType::S),
            definition("sk", ScalarType::N),
            definition("status", ScalarType::S),
            definition("rank", ScalarType::N),
        ],
        key_schema: keys("pk", Some("sk")),
        local_secondary_indexes: vec![LocalSecondaryIndex {
            index_name: "by_rank".into(),
            key_schema: keys("pk", Some("rank")),
            projection: Projection { projection_type: ProjectionType::KeysOnly, non_key_attributes: vec![] },
        }],
        global_secondary_indexes: vec![GlobalSecondaryIndex {
            index_name: "by_status".into(),
            key_schema: keys("status", Some("sk")),
            projection: Projection {
                projection_type: ProjectionType::Include,
                non_key_attributes: vec!["name".into()],
            },
            provisioned_throughput: None,
            on_demand_throughput: None,
        }],
        billing_mode: BillingMode::PayPerRequest,
        provisioned_throughput: None,
        on_demand_throughput: None,
        stream_specification: None,
        sse_specification: None,
        tags: BTreeMap::new(),
        table_class: None,
        deletion_protection_enabled: false,
    }
}

async fn backend_with_table() -> MemoryBackend {
    let backend = MemoryBackend::default();
    backend.create_table(table_input("items")).await.unwrap();
    backend
}

async fn put(backend: &MemoryBackend, item: Item) {
    backend.put_item(PutItemInput { table_name: "items".into(), item, ..Default::default() }).await.unwrap();
}

fn code(error: &Error) -> &str {
    error.as_service().map_or("", |service| service.code.as_str())
}

fn message(error: &Error) -> String {
    error.as_service().map_or_else(|| error.to_string(), |service| service.message.clone())
}

#[tokio::test]
async fn tables_are_created_described_listed_and_deleted() {
    let backend = MemoryBackend::default();
    let created = backend.create_table(table_input("items")).await.unwrap();
    assert_eq!(created.table_status, TableStatus::Creating, "CreateTable reports CREATING, like DynamoDB");
    let described = backend.describe_table("items").await.unwrap();
    assert_eq!(described.table_status, TableStatus::Active);
    assert_eq!(described.global_secondary_indexes[0].index_status, Some(IndexStatus::Active));
    assert!(described.table_arn.unwrap().ends_with(":table/items"));
    let error = backend.create_table(table_input("items")).await.unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::ResourceInUse));
    backend.create_table(table_input("other")).await.unwrap();
    let page = backend.list_tables(ListTablesInput { limit: Some(1), ..Default::default() }).await.unwrap();
    assert_eq!(page.table_names, ["items"]);
    assert_eq!(page.last_evaluated_table_name.as_deref(), Some("items"));
    let deleted = backend.delete_table("items").await.unwrap();
    assert_eq!(deleted.table_status, TableStatus::Deleting);
    let error = backend.describe_table("items").await.unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::ResourceNotFound));
    assert!(message(&error).contains("Table: items not found"));
}

#[tokio::test]
async fn create_table_validates_like_dynamodb() {
    let backend = MemoryBackend::default();
    let mut input = table_input("ok-name");
    input.attribute_definitions.push(definition("unused", ScalarType::S));
    let error = backend.create_table(input).await.unwrap_err();
    assert!(message(&error).contains("Some AttributeDefinitions are not used"), "{error}");

    let mut input = table_input("ok-name");
    input.billing_mode = BillingMode::Provisioned;
    let error = backend.create_table(input).await.unwrap_err();
    assert!(message(&error).contains("ReadCapacityUnits and WriteCapacityUnits must both be specified"));

    let error = backend.create_table(table_input("x")).await.unwrap_err();
    assert!(message(&error).contains("Member must have length greater than or equal to 3"));

    let mut input = table_input("ok-name");
    input.key_schema.sort_key = None;
    input.attribute_definitions.retain(|definition| definition.name != "sk");
    input.global_secondary_indexes.clear();
    let error = backend.create_table(input).await.unwrap_err();
    assert!(message(&error).contains("does not have a range key"), "{error}");

    let mut input = table_input("protected");
    input.deletion_protection_enabled = true;
    backend.create_table(input).await.unwrap();
    let error = backend.delete_table("protected").await.unwrap_err();
    assert!(message(&error).contains("protected against deletion"));
}

#[tokio::test]
async fn keys_are_validated() {
    let backend = backend_with_table().await;
    let put_error = |item: Item| {
        let backend = backend.clone();
        async move {
            backend.put_item(PutItemInput { table_name: "items".into(), item, ..Default::default() }).await.unwrap_err()
        }
    };
    let error = put_error(item([("pk", s("a"))])).await;
    assert!(message(&error).contains("Missing the key sk in the item"));
    let error = put_error(item([("pk", n(1)), ("sk", n(1))])).await;
    assert!(message(&error).contains("Type mismatch for key pk expected: S actual: N"));
    let error = put_error(item([("pk", s("")), ("sk", n(1))])).await;
    assert!(message(&error).contains("cannot contain an empty string value"));
    let error = put_error(item([("pk", s("a")), ("sk", n(1)), ("status", n(5))])).await;
    assert!(message(&error).contains("Type mismatch for Index Key status"), "{error}");
    let error = put_error(item([("pk", s("a")), ("sk", n(1)), ("tags", AttributeValue::Ss(vec![]))])).await;
    assert!(message(&error).contains("may not be empty"));
    let error = backend
        .get_item(GetItemInput { table_name: "items".into(), key: item([("pk", s("a"))]), ..Default::default() })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "The provided key element does not match the schema");
    let error = backend
        .get_item(GetItemInput { table_name: "missing".into(), key: item([("pk", s("a"))]), ..Default::default() })
        .await
        .unwrap_err();
    assert_eq!(code(&error), "ResourceNotFoundException");
}

#[tokio::test]
async fn conditional_writes_and_return_values() {
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("count", n(1))])).await;
    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1))]),
            condition_expression: Some("attribute_not_exists(pk)".into()),
            return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
            ..Default::default()
        })
        .await
        .unwrap_err();
    let service = error.as_service().unwrap();
    assert_eq!(service.kind, ErrorKind::ConditionalCheckFailed);
    assert_eq!(service.operation, "PutItem");
    assert_eq!(service.item.as_ref().unwrap()["count"], n(1), "ALL_OLD returns the item that failed the condition");

    let output = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1)), ("count", n(2))]),
            return_values: ReturnValues::AllOld,
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(output.attributes.unwrap()["count"], n(1));

    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1))]),
            return_values: ReturnValues::AllNew,
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert!(message(&error).contains("ReturnValues can only be ALL_OLD or NONE"));

    let deleted = backend
        .delete_item(DeleteItemInput {
            table_name: "items".into(),
            key: item([("pk", s("a")), ("sk", n(1))]),
            condition_expression: Some("#c = :two".into()),
            expression_attribute_names: names(&[("#c", "count")]),
            expression_attribute_values: values(&[(":two", n(2))]),
            return_values: ReturnValues::AllOld,
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(deleted.attributes.unwrap()["count"], n(2));
}

#[tokio::test]
async fn updates_upsert_and_return_updated_attributes() {
    let backend = backend_with_table().await;
    let key = item([("pk", s("u")), ("sk", n(1))]);
    let update = |expression: &str, return_values: ReturnValues, vals: Values| UpdateItemInput {
        table_name: "items".into(),
        key: key.clone(),
        update_expression: Some(expression.into()),
        expression_attribute_values: vals,
        return_values,
        ..Default::default()
    };
    let output = backend
        .update_item(update(
            "SET visits = :one, tags = :tags",
            ReturnValues::AllNew,
            values(&[(":one", n(1)), (":tags", AttributeValue::Ss(vec!["x".into()]))]),
        ))
        .await
        .unwrap();
    let new = output.attributes.unwrap();
    assert_eq!(new["pk"], s("u"), "an update of a missing item creates it");
    assert_eq!(new["visits"], n(1));
    let output = backend
        .update_item(update("ADD visits :one", ReturnValues::UpdatedOld, values(&[(":one", n(1))])))
        .await
        .unwrap();
    assert_eq!(output.attributes.unwrap(), item([("visits", n(1))]));
    let output = backend
        .update_item(update("ADD visits :one", ReturnValues::UpdatedNew, values(&[(":one", n(1))])))
        .await
        .unwrap();
    assert_eq!(output.attributes.unwrap(), item([("visits", n(3))]));
    let error = backend
        .update_item(update("SET profile.city = :one", ReturnValues::None, values(&[(":one", n(1))])))
        .await
        .unwrap_err();
    assert!(message(&error).contains("document path provided in the update expression is invalid"), "{error}");
    let error =
        backend.update_item(update("SET sk = :one", ReturnValues::None, values(&[(":one", n(1))]))).await.unwrap_err();
    assert!(message(&error).contains("Cannot update attribute sk. This attribute is part of the key"));
    let error = backend
        .update_item(UpdateItemInput {
            condition_expression: Some("visits > :ten".into()),
            ..update("REMOVE tags", ReturnValues::None, values(&[(":ten", n(10))]))
        })
        .await
        .unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::ConditionalCheckFailed));
}

async fn seed_query_data(backend: &MemoryBackend) {
    for sort in 1..=10 {
        let status = if sort % 2 == 0 { "even" } else { "odd" };
        put(
            backend,
            item([
                ("pk", s("p")),
                ("sk", n(sort)),
                ("status", s(status)),
                ("rank", n(100 - sort)),
                ("name", s(&format!("item {sort}"))),
                ("secret", s("not projected")),
            ]),
        )
        .await;
    }
    put(backend, item([("pk", s("q")), ("sk", n(1)), ("name", s("other partition, no index keys"))])).await;
}

fn query(expression: &str, vals: Values) -> QueryInput {
    QueryInput {
        table_name: "items".into(),
        key_condition_expression: Some(expression.into()),
        expression_attribute_values: vals,
        scan_index_forward: true,
        ..Default::default()
    }
}

#[tokio::test]
async fn query_orders_filters_and_pages() {
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let all = backend.query(query("pk = :p", values(&[(":p", s("p"))]))).await.unwrap();
    let sorts: Vec<&AttributeValue> = all.items.iter().map(|item| &item["sk"]).collect();
    assert_eq!(sorts, (1..=10).map(n).collect::<Vec<_>>().iter().collect::<Vec<_>>(), "numeric sort key order");
    assert!(all.last_evaluated_key.is_none());

    let descending = backend
        .query(QueryInput {
            scan_index_forward: false,
            ..query("pk = :p AND sk BETWEEN :lo AND :hi", values(&[(":p", s("p")), (":lo", n(3)), (":hi", n(5))]))
        })
        .await
        .unwrap();
    assert_eq!(descending.items.iter().map(|item| item["sk"].clone()).collect::<Vec<_>>(), [n(5), n(4), n(3)]);

    // Limit counts evaluated items, before the filter; a full page reports LastEvaluatedKey.
    let mut input = QueryInput {
        limit: Some(4),
        filter_expression: Some("#s = :even".into()),
        expression_attribute_names: names(&[("#s", "status")]),
        ..query("pk = :p", values(&[(":p", s("p")), (":even", s("even"))]))
    };
    let mut seen = Vec::new();
    let mut pages = 0;
    loop {
        let page = backend.query(input.clone()).await.unwrap();
        pages += 1;
        assert!(page.scanned_count <= 4);
        seen.extend(page.items.iter().map(|item| item["sk"].clone()));
        match page.last_evaluated_key {
            Some(key) => input.exclusive_start_key = Some(key),
            None => break,
        }
    }
    assert_eq!(seen, [n(2), n(4), n(6), n(8), n(10)]);
    assert_eq!(
        pages, 3,
        "10 items with Limit 4: two full pages and a final one (the last full page may be followed by an empty page)"
    );

    let count = backend
        .query(QueryInput { select: Some(Select::Count), ..query("pk = :p", values(&[(":p", s("p"))])) })
        .await
        .unwrap();
    assert_eq!((count.count, count.items.len()), (10, 0));

    let projected = backend
        .query(QueryInput {
            projection_expression: Some("#n".into()),
            expression_attribute_names: names(&[("#n", "name")]),
            ..query("pk = :p AND sk = :one", values(&[(":p", s("p")), (":one", n(1))]))
        })
        .await
        .unwrap();
    assert_eq!(projected.items, [item([("name", s("item 1"))])]);

    let error = backend
        .query(QueryInput {
            filter_expression: Some("sk > :one".into()),
            ..query("pk = :p", values(&[(":p", s("p")), (":one", n(1))]))
        })
        .await
        .unwrap_err();
    assert!(message(&error).contains("Filter Expression can only contain non-primary key attributes"));
}

#[tokio::test]
async fn global_indexes_are_sparse_projected_and_eventually_consistent_only() {
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let input = QueryInput {
        index_name: Some("by_status".into()),
        expression_attribute_names: names(&[("#s", "status")]),
        ..query("#s = :odd", values(&[(":odd", s("odd"))]))
    };
    let odd = backend.query(input.clone()).await.unwrap();
    assert_eq!(odd.count, 5, "the item without a status is not in the index");
    let first = &odd.items[0];
    assert!(first.contains_key("name"), "INCLUDE projects name");
    assert!(!first.contains_key("secret"), "non-projected attributes are not returned");
    assert!(first.contains_key("pk") && first.contains_key("sk") && first.contains_key("status"));

    let page = backend.query(QueryInput { limit: Some(2), ..input.clone() }).await.unwrap();
    let last = page.last_evaluated_key.unwrap();
    assert_eq!(
        last.keys().cloned().collect::<Vec<_>>(),
        ["pk", "sk", "status"],
        "index pages carry table and index keys"
    );
    let next =
        backend.query(QueryInput { limit: Some(2), exclusive_start_key: Some(last), ..input.clone() }).await.unwrap();
    assert_eq!(next.items[0]["sk"], n(5));

    let error = backend.query(QueryInput { consistent_read: true, ..input.clone() }).await.unwrap_err();
    assert!(message(&error).contains("Consistent reads are not supported on global secondary indexes"));
    let error = backend.query(QueryInput { select: Some(Select::AllAttributes), ..input }).await.unwrap_err();
    assert!(message(&error).contains("Select type ALL_ATTRIBUTES is not supported"));
}

#[tokio::test]
async fn local_indexes_sort_by_their_own_key_and_fetch_from_the_table() {
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let input = QueryInput {
        index_name: Some("by_rank".into()),
        expression_attribute_names: names(&[("#r", "rank")]),
        ..query("pk = :p AND #r < :limit", values(&[(":p", s("p")), (":limit", n(93))]))
    };
    let keys_only = backend.query(input.clone()).await.unwrap();
    assert_eq!(keys_only.items.iter().map(|item| item["rank"].clone()).collect::<Vec<_>>(), [n(90), n(91), n(92)]);
    assert!(!keys_only.items[0].contains_key("name"));
    let full = backend
        .query(QueryInput { select: Some(Select::AllAttributes), consistent_read: true, ..input })
        .await
        .unwrap();
    assert!(full.items[0].contains_key("secret"), "local indexes fetch every attribute from the table");
}

#[tokio::test]
async fn scans_split_into_segments_and_page() {
    let backend = MemoryBackend::default();
    let mut input = table_input("items");
    input.local_secondary_indexes.clear();
    input.attribute_definitions.retain(|definition| definition.name != "rank");
    backend.create_table(input).await.unwrap();
    for partition in 0..50 {
        put(&backend, item([("pk", s(&format!("p{partition}"))), ("sk", n(0))])).await;
    }
    let mut seen = std::collections::BTreeSet::new();
    for segment in 0..4 {
        let mut input = ScanInput {
            table_name: "items".into(),
            segment: Some(segment),
            total_segments: Some(4),
            limit: Some(7),
            ..Default::default()
        };
        loop {
            let page = backend.scan(input.clone()).await.unwrap();
            for item in page.items {
                assert!(seen.insert(item["pk"].as_s().unwrap().to_owned()), "an item appears in exactly one segment");
            }
            match page.last_evaluated_key {
                Some(key) => input.exclusive_start_key = Some(key),
                None => break,
            }
        }
    }
    assert_eq!(seen.len(), 50);
    let error = backend
        .scan(ScanInput { table_name: "items".into(), segment: Some(4), total_segments: Some(4), ..Default::default() })
        .await
        .unwrap_err();
    assert!(message(&error).contains("must be less than parameter TotalSegments"));
}

#[tokio::test]
async fn pages_stop_after_one_megabyte() {
    let backend = backend_with_table().await;
    let big = "x".repeat(300 * 1024);
    for sort in 0..8 {
        put(&backend, item([("pk", s("big")), ("sk", n(sort)), ("blob", s(&big))])).await;
    }
    let page = backend.query(query("pk = :p", values(&[(":p", s("big"))]))).await.unwrap();
    assert_eq!(page.count, 4, "a page reads at most 1 MB (4 items of 300 KB reach it)");
    assert!(page.last_evaluated_key.is_some());
}

#[tokio::test]
async fn item_size_is_limited_to_400_kb() {
    let backend = backend_with_table().await;
    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1)), ("blob", s(&"x".repeat(401 * 1024)))]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Item size has exceeded the maximum allowed size");
}

#[tokio::test]
async fn batches_validate_limits_and_duplicates() {
    let backend = backend_with_table().await;
    let writes: Vec<WriteRequest> =
        (0..25).map(|sort| WriteRequest::Put(item([("pk", s("b")), ("sk", n(sort))]))).collect();
    backend
        .batch_write_item(BatchWriteItemInput {
            request_items: BTreeMap::from([("items".into(), writes.clone())]),
            ..Default::default()
        })
        .await
        .unwrap();
    let mut too_many = writes.clone();
    too_many.push(WriteRequest::Put(item([("pk", s("b")), ("sk", n(99))])));
    let error = backend
        .batch_write_item(BatchWriteItemInput {
            request_items: BTreeMap::from([("items".into(), too_many)]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert!(message(&error).contains("Too many items requested for the BatchWriteItem call"));
    let duplicates = vec![
        WriteRequest::Put(item([("pk", s("b")), ("sk", n(1))])),
        WriteRequest::Delete(item([("pk", s("b")), ("sk", n(1))])),
    ];
    let error = backend
        .batch_write_item(BatchWriteItemInput {
            request_items: BTreeMap::from([("items".into(), duplicates)]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert!(message(&error).contains("Provided list of item keys contains duplicates"));

    let keys: Vec<Item> = (0..5)
        .map(|sort| item([("pk", s("b")), ("sk", n(sort))]))
        .chain([item([("pk", s("missing")), ("sk", n(0))])])
        .collect();
    let output = backend
        .batch_get_item(BatchGetItemInput {
            request_items: BTreeMap::from([("items".into(), KeysAndAttributes { keys, ..Default::default() })]),
            return_consumed_capacity: ReturnConsumedCapacity::Total,
        })
        .await
        .unwrap();
    assert_eq!(output.responses["items"].len(), 5, "missing keys are simply absent");
    assert_eq!(output.consumed_capacity[0].capacity_units, 3.0, "6 eventually consistent reads of 0.5 units");
}

#[tokio::test]
async fn transactions_are_all_or_nothing_with_reasons() {
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("acct")), ("sk", n(1)), ("balance", n(10))])).await;
    let target = |condition: Option<&str>, vals: Values| TransactTarget {
        table_name: "items".into(),
        condition_expression: condition.map(str::to_owned),
        expression_attribute_values: vals,
        return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
        ..Default::default()
    };
    let transfer = |amount: i64| TransactWriteItemsInput {
        transact_items: vec![
            TransactWriteItem::Update {
                target: target(Some("balance >= :amount"), values(&[(":amount", n(amount))])),
                key: item([("pk", s("acct")), ("sk", n(1))]),
                update_expression: "SET balance = balance - :amount".into(),
            },
            TransactWriteItem::Put {
                target: target(None, Values::new()),
                item: item([("pk", s("ledger")), ("sk", n(amount))]),
            },
        ],
        ..Default::default()
    };
    backend.transact_write_items(transfer(7)).await.unwrap();
    let error = backend.transact_write_items(transfer(5)).await.unwrap_err();
    let service = error.as_service().unwrap();
    assert_eq!(service.kind, ErrorKind::TransactionCanceled);
    assert_eq!(service.cancellation_reasons[0].code, "ConditionalCheckFailed");
    assert_eq!(service.cancellation_reasons[0].item.as_ref().unwrap()["balance"], n(3));
    assert_eq!(service.cancellation_reasons[1].code, "None");
    assert!(service.message.contains("[ConditionalCheckFailed, None]"));
    let ledger = backend
        .get_item(GetItemInput {
            table_name: "items".into(),
            key: item([("pk", s("ledger")), ("sk", n(5))]),
            ..Default::default()
        })
        .await
        .unwrap();
    assert!(ledger.item.is_none(), "nothing of a canceled transaction is written");

    let mut same_item = transfer(1);
    same_item.transact_items[1] = TransactWriteItem::ConditionCheck {
        target: target(Some("attribute_exists(pk)"), Values::new()),
        key: item([("pk", s("acct")), ("sk", n(1))]),
    };
    let error = backend.transact_write_items(same_item).await.unwrap_err();
    assert!(message(&error).contains("cannot include multiple operations on one item"));
}

#[tokio::test]
async fn transaction_tokens_are_idempotent() {
    let backend = backend_with_table().await;
    let input = |value: i64| TransactWriteItemsInput {
        transact_items: vec![TransactWriteItem::Update {
            target: TransactTarget {
                table_name: "items".into(),
                expression_attribute_values: values(&[(":one", n(value))]),
                ..Default::default()
            },
            key: item([("pk", s("counter")), ("sk", n(0))]),
            update_expression: "ADD hits :one".into(),
        }],
        client_request_token: Some("token-1".into()),
        ..Default::default()
    };
    backend.transact_write_items(input(1)).await.unwrap();
    backend.transact_write_items(input(1)).await.unwrap();
    let item = backend
        .get_item(GetItemInput {
            table_name: "items".into(),
            key: item([("pk", s("counter")), ("sk", n(0))]),
            ..Default::default()
        })
        .await
        .unwrap()
        .item
        .unwrap();
    assert_eq!(item["hits"], n(1), "a replay with the same token is not applied twice");
    let error = backend.transact_write_items(input(2)).await.unwrap_err();
    assert_eq!(code(&error), "IdempotentParameterMismatchException");
}

#[tokio::test]
async fn transaction_tokens_compare_binary_values() {
    let backend = backend_with_table().await;
    let input = |bytes: &[u8]| TransactWriteItemsInput {
        transact_items: vec![TransactWriteItem::Put {
            target: TransactTarget { table_name: "items".into(), ..Default::default() },
            item: item([("pk", s("blob")), ("sk", n(0)), ("data", AttributeValue::b(bytes.to_vec()))]),
        }],
        client_request_token: Some("token-binary".into()),
        ..Default::default()
    };
    backend.transact_write_items(input(b"one")).await.unwrap();
    backend.transact_write_items(input(b"one")).await.unwrap();
    let error = backend.transact_write_items(input(b"two")).await.unwrap_err();
    assert_eq!(code(&error), "IdempotentParameterMismatchException", "same length, other bytes");
}

#[tokio::test]
async fn transact_get_returns_items_in_order() {
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("v", n(1))])).await;
    let output = backend
        .transact_get_items(TransactGetItemsInput {
            transact_items: vec![
                TransactGet {
                    table_name: "items".into(),
                    key: item([("pk", s("missing")), ("sk", n(1))]),
                    ..Default::default()
                },
                TransactGet {
                    table_name: "items".into(),
                    key: item([("pk", s("a")), ("sk", n(1))]),
                    projection_expression: Some("v".into()),
                    ..Default::default()
                },
            ],
            return_consumed_capacity: ReturnConsumedCapacity::Total,
        })
        .await
        .unwrap();
    assert_eq!(output.responses, [None, Some(item([("v", n(1))]))]);
    assert_eq!(output.consumed_capacity[0].capacity_units, 4.0, "transactional reads cost 2 units each");
}

#[tokio::test]
async fn time_to_live_settings_and_purging() {
    let backend = backend_with_table().await;
    assert_eq!(backend.describe_time_to_live("items").await.unwrap().status, TimeToLiveStatus::Disabled);
    let input = UpdateTimeToLiveInput { table_name: "items".into(), enabled: true, attribute_name: "expires".into() };
    backend.update_time_to_live(input.clone()).await.unwrap();
    let error = backend.update_time_to_live(input).await.unwrap_err();
    assert!(message(&error).contains("TimeToLive is already enabled"));
    put(&backend, item([("pk", s("old")), ("sk", n(1)), ("expires", n(1_000))])).await;
    put(&backend, item([("pk", s("new")), ("sk", n(1)), ("expires", n(4_000_000_000))])).await;
    put(&backend, item([("pk", s("none")), ("sk", n(1))])).await;
    assert_eq!(backend.purge_expired(None), 1);
    let left = backend.scan(ScanInput { table_name: "items".into(), ..Default::default() }).await.unwrap();
    assert_eq!(left.count, 2);
}

#[tokio::test]
async fn tags_backups_restores_and_point_in_time_recovery() {
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1))])).await;
    let arn = backend.describe_table("items").await.unwrap().table_arn.unwrap();
    backend
        .tag_resource(&arn, BTreeMap::from([("team".into(), "core".into()), ("env".into(), "test".into())]))
        .await
        .unwrap();
    backend.untag_resource(&arn, vec!["env".into()]).await.unwrap();
    assert_eq!(
        backend.list_tags_of_resource(&arn, None).await.unwrap().tags,
        BTreeMap::from([("team".into(), "core".into())])
    );

    let details = backend.create_backup("items", "nightly").await.unwrap();
    assert_eq!(details.backup_status, BackupStatus::Available);
    put(&backend, item([("pk", s("after-backup")), ("sk", n(1))])).await;
    let restored = backend
        .restore_table_from_backup(RestoreTableFromBackupInput {
            target_table_name: "items-restored".into(),
            backup_arn: details.backup_arn.clone(),
            overrides: RestoreOverrides::default(),
        })
        .await
        .unwrap();
    assert_eq!(restored.item_count, 1, "the restore has the items of the backup");
    assert_eq!(
        backend
            .list_backups(ListBackupsInput { table_name: Some("items".into()), ..Default::default() })
            .await
            .unwrap()
            .backups
            .len(),
        1
    );
    backend.delete_backup(&details.backup_arn).await.unwrap();
    assert_eq!(code(&backend.describe_backup(&details.backup_arn).await.unwrap_err()), "BackupNotFoundException");

    let pitr = |target: &str| RestoreTableToPointInTimeInput {
        source_table_name: "items".into(),
        target_table_name: target.into(),
        use_latest_restorable_time: true,
        restore_date_time: None,
        overrides: RestoreOverrides::default(),
    };
    let error = backend.restore_table_to_point_in_time(pitr("items-pitr")).await.unwrap_err();
    assert_eq!(code(&error), "PointInTimeRecoveryUnavailableException");
    let status = backend.update_continuous_backups("items", true, Some(7)).await.unwrap();
    assert_eq!(
        (status.point_in_time_recovery_status, status.recovery_period_in_days),
        (ContinuousBackupsStatus::Enabled, Some(7))
    );
    assert_eq!(backend.restore_table_to_point_in_time(pitr("items-pitr")).await.unwrap().item_count, 2);
}

#[tokio::test]
async fn update_table_adds_and_backfills_global_indexes() {
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("email", s("a@example.com"))])).await;
    put(&backend, item([("pk", s("b")), ("sk", n(1)), ("email", n(5))])).await;
    let description = backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            attribute_definitions: vec![definition("email", ScalarType::S)],
            global_secondary_index_updates: vec![GlobalSecondaryIndexUpdate::Create(GlobalSecondaryIndex {
                index_name: "by_email".into(),
                key_schema: keys("email", None),
                projection: Projection::all(),
                provisioned_throughput: None,
                on_demand_throughput: None,
            })],
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(description.table_status, TableStatus::Updating);
    let found = backend
        .query(QueryInput {
            index_name: Some("by_email".into()),
            ..query("email = :e", values(&[(":e", s("a@example.com"))]))
        })
        .await
        .unwrap();
    assert_eq!(found.count, 1, "existing items are backfilled; items with a wrongly typed key are skipped");
    backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            global_secondary_index_updates: vec![GlobalSecondaryIndexUpdate::Delete { index_name: "by_email".into() }],
            ..Default::default()
        })
        .await
        .unwrap();
    let description = backend.describe_table("items").await.unwrap();
    assert!(description.attribute_definitions.iter().all(|definition| definition.name != "email"));
}

/// Statements are parsed on the threads of the runtime (2 MiB stacks), for every backend
/// (`partiql::effect`): nesting is bounded, so that a deep recursion cannot kill the process.
#[tokio::test]
async fn deeply_nested_partiql_statements_are_refused() {
    let backend = backend_with_table().await;
    let run = |text: String| {
        let input = ExecuteStatementInput { statement: text, ..Default::default() };
        backend.execute_statement(input)
    };
    let condition =
        |levels: usize| format!("SELECT * FROM items WHERE {}pk = 'a'{}", "(".repeat(levels), ")".repeat(levels));
    run(condition(100)).await.unwrap();
    let value = |levels: usize| {
        format!("INSERT INTO items VALUE {{'pk': 'a', 'sk': 1, 'v': {}1{}}}", "[".repeat(levels), "]".repeat(levels))
    };
    for text in [
        condition(101),
        condition(4000),
        value(4000),
        format!("SELECT * FROM items WHERE {}pk = 'a'", "NOT ".repeat(2000)),
    ] {
        let error = run(text).await.unwrap_err();
        assert_eq!(
            message(&error),
            "Statement wasn't well formed, can't be processed: it nests more than 100 levels deep"
        );
    }
    let deep = format!("UPDATE items SET v = 1 WHERE {}pk = 'a'{}", "(".repeat(4000), ")".repeat(4000));
    assert_eq!(super::partiql::effect(&deep, &[]), super::partiql::Effect::Once, "not known: sent once");

    // DynamoDB's 8,192 characters per statement also bound AND and OR chains, which form nested
    // trees without any parentheses: the longest one runs, and longer statements are refused.
    let chain = |statement: &str, terms: usize| format!("{statement} WHERE pk = 'a'{}", " AND sk = 1".repeat(terms));
    let longest = chain("SELECT * FROM items", 740);
    assert!(longest.len() <= 8192);
    run(longest).await.unwrap();
    let error = run(chain("SELECT * FROM items", 20_000)).await.unwrap_err();
    assert_eq!(
        message(&error),
        "1 validation error detected: Value at 'statement' failed to satisfy constraint: Member must have length less \
         than or equal to 8192"
    );
    let update = chain("UPDATE items SET v = 1", 20_000);
    assert_eq!(
        super::partiql::effect(&update, &[]),
        super::partiql::Effect::Once,
        "sent to DynamoDB, which refuses it"
    );
}

#[tokio::test]
async fn partiql_statements() {
    let backend = backend_with_table().await;
    let statement = |text: &str, parameters: Vec<AttributeValue>| ExecuteStatementInput {
        statement: text.into(),
        parameters,
        ..Default::default()
    };
    backend
        .execute_statement(statement(
            "INSERT INTO \"items\" VALUE {'pk': ?, 'sk': 1, 'name': 'Ada', 'tags': <<'a', 'b'>>}",
            vec![s("p1")],
        ))
        .await
        .unwrap();
    let error = backend
        .execute_statement(statement("INSERT INTO items VALUE {'pk': 'p1', 'sk': 1}", vec![]))
        .await
        .unwrap_err();
    assert_eq!(code(&error), "DuplicateItemException");
    let found = backend
        .execute_statement(statement("SELECT name, tags FROM items WHERE pk = ? AND sk = 1", vec![s("p1")]))
        .await
        .unwrap();
    assert_eq!(found.items, [item([("name", s("Ada")), ("tags", AttributeValue::Ss(vec!["a".into(), "b".into()]))])]);
    let updated = backend
        .execute_statement(statement(
            "UPDATE items SET visits = 1 SET name = ? WHERE pk = 'p1' AND sk = 1 RETURNING ALL NEW *",
            vec![s("Grace")],
        ))
        .await
        .unwrap();
    assert_eq!(updated.items[0]["name"], s("Grace"));
    let error = backend
        .execute_statement(statement("UPDATE items SET visits = 2 WHERE pk = 'nope' AND sk = 1", vec![]))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::ConditionalCheckFailed), "UPDATE needs an existing item");
    let error =
        backend.execute_statement(statement("UPDATE items SET visits = 2 WHERE pk = 'p1'", vec![])).await.unwrap_err();
    assert!(message(&error).contains("mandatory equality on all key attributes"));
    let scanned = backend
        .execute_statement(statement("SELECT * FROM items WHERE visits >= 1 AND tags IS NOT MISSING", vec![]))
        .await
        .unwrap();
    assert_eq!(scanned.items.len(), 1);
    backend.execute_statement(statement("DELETE FROM items WHERE pk = 'p1' AND sk = 1", vec![])).await.unwrap();
    assert!(backend.execute_statement(statement("SELECT * FROM items", vec![])).await.unwrap().items.is_empty());

    let batch = backend
        .batch_execute_statement(BatchExecuteStatementInput {
            statements: vec![
                ParameterizedStatement {
                    statement: "INSERT INTO items VALUE {'pk': 'x', 'sk': 1}".into(),
                    ..Default::default()
                },
                ParameterizedStatement {
                    statement: "INSERT INTO items VALUE {'pk': 'x', 'sk': 1}".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        })
        .await
        .unwrap();
    assert!(batch.responses[0].error.is_none());
    assert_eq!(batch.responses[1].error.as_ref().unwrap().code, "DuplicateItem");

    let error = backend
        .execute_transaction(ExecuteTransactionInput {
            transact_statements: vec![
                ParameterizedStatement {
                    statement: "INSERT INTO items VALUE {'pk': 'y', 'sk': 1}".into(),
                    ..Default::default()
                },
                ParameterizedStatement {
                    statement: "INSERT INTO items VALUE {'pk': 'x', 'sk': 1}".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        })
        .await
        .unwrap_err();
    let reasons: Vec<&str> =
        error.as_service().unwrap().cancellation_reasons.iter().map(|reason| reason.code.as_str()).collect();
    assert_eq!(reasons, ["None", "DuplicateItem"]);
}

#[tokio::test]
async fn partiql_order_by_runs_one_query_per_partition_key_value() {
    // The rules and messages of DynamoDB (and DynamoDB Local): the WHERE clause names the
    // partition key values, ORDER BY names key attributes of the table or index.
    let backend = backend_with_table().await;
    for pk in ["a", "b", "c"] {
        for sk in 1..=3 {
            let x = n(sk % 2);
            put(&backend, item([("pk", s(pk)), ("sk", n(sk)), ("status", s("open")), ("rank", n(10 - sk)), ("x", x)]))
                .await;
        }
    }
    let input = |text: &str, limit: Option<u32>, next_token: Option<String>| ExecuteStatementInput {
        statement: text.into(),
        limit,
        next_token,
        ..Default::default()
    };
    let rows = |output: &ExecuteStatementOutput| -> Vec<String> {
        output
            .items
            .iter()
            .map(|item| match (&item["pk"], &item["sk"]) {
                (AttributeValue::S(pk), AttributeValue::N(sk)) => format!("{pk}{}", sk.as_str()),
                other => panic!("unexpected key {other:?}"),
            })
            .collect()
    };
    for (statement, expected) in [
        ("SELECT * FROM items WHERE pk = 'a' ORDER BY sk DESC", vec!["a3", "a2", "a1"]),
        ("SELECT * FROM items WHERE pk = 'a' order by sk asc", vec!["a1", "a2", "a3"]),
        ("SELECT * FROM items WHERE pk = 'a' AND x = 1 ORDER BY sk DESC", vec!["a3", "a1"]),
        ("SELECT * FROM items WHERE pk IN ['c', 'a'] ORDER BY pk DESC, sk", vec!["c1", "c2", "c3", "a1", "a2", "a3"]),
        (
            "SELECT * FROM items WHERE pk = 'b' OR pk = 'a' ORDER BY pk, sk DESC",
            vec!["a3", "a2", "a1", "b3", "b2", "b1"],
        ),
        ("SELECT * FROM items WHERE pk IN ['a'] ORDER BY sk DESC", vec!["a3", "a2", "a1"]),
        ("SELECT * FROM items.\"by_rank\" WHERE pk = 'a' ORDER BY rank", vec!["a3", "a2", "a1"]),
    ] {
        let output = backend.execute_statement(input(statement, None, None)).await.unwrap();
        assert_eq!(rows(&output), expected, "{statement}");
    }
    let by_status = backend
        .execute_statement(input(
            "SELECT * FROM items.\"by_status\" WHERE status = 'open' ORDER BY sk DESC",
            None,
            None,
        ))
        .await
        .unwrap();
    let sort_keys: Vec<String> = rows(&by_status).iter().map(|row| row[1..].to_owned()).collect();
    assert_eq!(sort_keys, ["3", "3", "3", "2", "2", "2", "1", "1", "1"], "the index's own sort key");

    // Without ORDER BY too, only the named partitions are read: `Limit` counts their items.
    let named =
        backend.execute_statement(input("SELECT * FROM items WHERE pk IN ['a', 'b']", Some(4), None)).await.unwrap();
    assert_eq!(named.items.len(), 4);
    assert!(rows(&named).iter().all(|row| !row.starts_with('c')));

    // Pages continue in the order.
    let ordered = "SELECT * FROM items WHERE pk IN ['a', 'b'] ORDER BY pk DESC, sk DESC";
    let first = backend.execute_statement(input(ordered, Some(4), None)).await.unwrap();
    assert_eq!(rows(&first), ["b3", "b2", "b1", "a3"]);
    let second = backend.execute_statement(input(ordered, Some(4), first.next_token.clone())).await.unwrap();
    assert_eq!(rows(&second), ["a2", "a1"]);
    assert!(second.next_token.is_none());

    for (statement, expected) in [
        ("SELECT * FROM items ORDER BY sk", "Must have WHERE clause in the statement when using ORDER BY clause."),
        (
            "SELECT * FROM items WHERE x = 1 ORDER BY x",
            "Must have at least one non-optional hash key condition in WHERE clause when using ORDER BY clause.",
        ),
        (
            "SELECT * FROM items WHERE pk = 'a' OR x = 1 ORDER BY sk",
            "Must have at least one non-optional hash key condition in WHERE clause when using ORDER BY clause.",
        ),
        (
            "SELECT * FROM items WHERE pk BETWEEN 'a' AND 'b' ORDER BY pk",
            "Must have at least one non-optional hash key condition in WHERE clause when using ORDER BY clause.",
        ),
        (
            "SELECT * FROM items WHERE pk = 'a' ORDER BY x",
            "Variable reference x in ORDER BY clause must be part of the primary key",
        ),
        (
            "SELECT * FROM items.\"by_rank\" WHERE pk = 'a' ORDER BY sk",
            "Variable reference sk in ORDER BY clause must be part of the primary key",
        ),
        (
            "SELECT * FROM items WHERE pk = 'a' OR pk = 'b' ORDER BY sk",
            "Must have hash key in ORDER BY clause when more than one hash key condition specified in WHERE clause.",
        ),
        (
            "SELECT * FROM items WHERE pk = 'a' ORDER BY a.b",
            "Argument to ORDER BY must be of type VariableReference. Type: PATH",
        ),
    ] {
        let error = backend.execute_statement(input(statement, None, None)).await.unwrap_err();
        assert_eq!((code(&error), message(&error)), ("ValidationException", expected.to_owned()), "{statement}");
    }
    let error = backend
        .execute_statement(input("SELECT * FROM items WHERE pk = 'a' ORDER BY sk DESC ASC", None, None))
        .await
        .unwrap_err();
    assert!(message(&error).starts_with("Statement wasn't well formed"), "{}", message(&error));
}

#[tokio::test(start_paused = true)]
async fn faults_inject_errors_unprocessed_batches_and_latency() {
    let backend = backend_with_table().await;
    backend.inject_fault(Fault {
        operation: Some("PutItem".into()),
        table: None,
        kind: FaultKind::Error { code: "ProvisionedThroughputExceededException".into(), message: "slow down".into() },
        times: 1,
    });
    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1))]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert!(error.is_retryable());
    put(&backend, item([("pk", s("a")), ("sk", n(1))])).await;

    backend.inject_fault(Fault {
        operation: Some("BatchWriteItem".into()),
        table: None,
        kind: FaultKind::Unprocessed,
        times: 1,
    });
    let input = BatchWriteItemInput {
        request_items: BTreeMap::from([(
            "items".into(),
            vec![WriteRequest::Put(item([("pk", s("b")), ("sk", n(1))]))],
        )]),
        ..Default::default()
    };
    let output = backend.batch_write_item(input).await.unwrap();
    assert_eq!(output.unprocessed_items["items"].len(), 1);

    backend.inject_fault(Fault {
        operation: None,
        table: Some("items".into()),
        kind: FaultKind::Latency(Duration::from_secs(3)),
        times: 1,
    });
    let started = tokio::time::Instant::now();
    backend.describe_table("items").await.unwrap();
    assert!(started.elapsed() >= Duration::from_secs(3));
}

#[tokio::test]
async fn consumed_capacity_follows_dynamodb_units() {
    let backend = backend_with_table().await;
    let output = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1)), ("status", s("x")), ("blob", s(&"x".repeat(1500)))]),
            return_consumed_capacity: ReturnConsumedCapacity::Indexes,
            ..Default::default()
        })
        .await
        .unwrap();
    let capacity = output.consumed_capacity.unwrap();
    assert_eq!(capacity.table.unwrap().write_capacity_units, 2.0, "1.5 KB rounds up to 2 write units");
    assert_eq!(capacity.global_secondary_indexes["by_status"].write_capacity_units, 1.0);
    let read = backend
        .get_item(GetItemInput {
            table_name: "items".into(),
            key: item([("pk", s("a")), ("sk", n(1))]),
            consistent_read: true,
            return_consumed_capacity: ReturnConsumedCapacity::Total,
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(read.consumed_capacity.unwrap().capacity_units, 1.0);
}

/// Queries and scans page through keys like DynamoDB in every direction and with every sort key
/// condition: compared, for many random requests, with a model that sorts and filters every item.
#[tokio::test]
async fn queries_and_scans_match_a_reference_model() {
    use super::table::StoredKey;
    use crate::expr::{Comparator, SortKeyCondition, sort_key_matches};

    let backend = MemoryBackend::default();
    let projection = Projection { projection_type: ProjectionType::All, non_key_attributes: vec![] };
    backend
        .create_table(CreateTableInput {
            attribute_definitions: vec![
                definition("pk", ScalarType::S),
                definition("sk", ScalarType::S),
                definition("group", ScalarType::S),
                definition("rank", ScalarType::N),
                definition("score", ScalarType::N),
            ],
            key_schema: keys("pk", Some("sk")),
            local_secondary_indexes: vec![LocalSecondaryIndex {
                index_name: "by_score".into(),
                key_schema: keys("pk", Some("score")),
                projection: projection.clone(),
            }],
            global_secondary_indexes: vec![GlobalSecondaryIndex {
                index_name: "by_group".into(),
                key_schema: keys("group", Some("rank")),
                projection,
                provisioned_throughput: None,
                on_demand_throughput: None,
            }],
            ..table_input("diff")
        })
        .await
        .unwrap();
    let mut rng = fastrand::Rng::with_seed(7);
    let mut items = Vec::new();
    for partition in ["a", "b", "c", "d", "e"] {
        let mut sorts: Vec<u32> = (0..300).collect();
        rng.shuffle(&mut sorts);
        for sort in &sorts[..60] {
            let prefix = if sort % 7 == 0 { "t" } else { "s" };
            let mut entry = item([("pk", s(partition)), ("sk", s(&format!("{prefix}{sort:03}")))]);
            if rng.u8(0..4) != 0 {
                entry.insert("group".into(), s(["g1", "g2"][rng.usize(0..2)]));
                entry.insert("rank".into(), n(rng.i64(0..15)));
            }
            if rng.bool() {
                entry.insert("score".into(), n(rng.i64(-50..50)));
            }
            backend
                .put_item(PutItemInput { table_name: "diff".into(), item: entry.clone(), ..Default::default() })
                .await
                .unwrap();
            items.push(entry);
        }
    }
    let table_key = |item: &Item| StoredKey::new(item["pk"].clone(), Some(item["sk"].clone()));

    for round in 0..400 {
        // Target: (index, partition attribute, sort attribute, numeric sort key).
        let (index, partition_name, sort_name, numeric) = match round % 3 {
            0 => (None, "pk", "sk", false),
            1 => (Some("by_group"), "group", "rank", true),
            _ => (Some("by_score"), "pk", "score", true),
        };
        let partition = if partition_name == "group" {
            s(["g1", "g2"][rng.usize(0..2)])
        } else {
            s(["a", "b", "c", "d", "e"][rng.usize(0..5)])
        };
        let bound = |rng: &mut fastrand::Rng| {
            if numeric {
                n(rng.i64(-60..60))
            } else {
                s(&format!("{}{:03}", ["s", "t"][rng.usize(0..2)], rng.u32(0..300)))
            }
        };
        let condition = match rng.u8(0..8) {
            0 => None,
            1 => Some(SortKeyCondition::Compare(Comparator::Eq, bound(&mut rng))),
            2 => Some(SortKeyCondition::Compare(Comparator::Lt, bound(&mut rng))),
            3 => Some(SortKeyCondition::Compare(Comparator::Le, bound(&mut rng))),
            4 => Some(SortKeyCondition::Compare(Comparator::Gt, bound(&mut rng))),
            5 => Some(SortKeyCondition::Compare(Comparator::Ge, bound(&mut rng))),
            6 => {
                let (low, high) = (bound(&mut rng), bound(&mut rng));
                let (low, high) =
                    if low.cmp_scalar(&high) == Some(std::cmp::Ordering::Greater) { (high, low) } else { (low, high) };
                Some(SortKeyCondition::Between(low, high))
            }
            _ if !numeric => {
                Some(SortKeyCondition::BeginsWith(s(["s", "t", "s0", "s1", "s12", "t0", "x"][rng.usize(0..7)])))
            }
            _ => None,
        };
        let forward = rng.bool();
        let limit = if rng.u8(0..4) == 0 { None } else { Some(rng.u32(1..12)) };

        // The reference: every item of the partition that has the target's keys, in key order.
        let mut expected: Vec<(StoredKey, StoredKey, &Item)> = items
            .iter()
            .filter(|item| item.get(partition_name) == Some(&partition) && item.contains_key(sort_name))
            .filter(|item| condition.as_ref().is_none_or(|condition| sort_key_matches(&item[sort_name], condition)))
            .map(|item| {
                (StoredKey::new(item[partition_name].clone(), Some(item[sort_name].clone())), table_key(item), item)
            })
            .collect();
        expected.sort_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
        if !forward {
            expected.reverse();
        }
        let expected: Vec<&Item> = expected.into_iter().map(|(_, _, item)| item).collect();

        let mut expression = "#p = :p".to_owned();
        let mut vals = values(&[(":p", partition.clone())]);
        match &condition {
            None => {}
            Some(SortKeyCondition::Compare(comparator, value)) => {
                expression += &format!(" AND #s {} :v", comparator.as_str());
                vals.insert(":v".into(), value.clone());
            }
            Some(SortKeyCondition::Between(low, high)) => {
                expression += " AND #s BETWEEN :lo AND :hi";
                vals.insert(":lo".into(), low.clone());
                vals.insert(":hi".into(), high.clone());
            }
            Some(SortKeyCondition::BeginsWith(prefix)) => {
                expression += " AND begins_with(#s, :v)";
                vals.insert(":v".into(), prefix.clone());
            }
        }
        let mut input = QueryInput {
            table_name: "diff".into(),
            index_name: index.map(str::to_owned),
            key_condition_expression: Some(expression),
            expression_attribute_names: if condition.is_some() {
                names(&[("#p", partition_name), ("#s", sort_name)])
            } else {
                names(&[("#p", partition_name)])
            },
            expression_attribute_values: vals,
            scan_index_forward: forward,
            limit,
            ..Default::default()
        };
        let mut found = Vec::new();
        loop {
            let page = backend.query(input.clone()).await.unwrap();
            let full = limit.is_some_and(|limit| page.items.len() == limit as usize);
            assert_eq!(page.last_evaluated_key.is_some(), full, "round {round}: LastEvaluatedKey only on full pages");
            found.extend(page.items);
            match page.last_evaluated_key {
                Some(key) => input.exclusive_start_key = Some(key),
                None => break,
            }
        }
        assert_eq!(found.iter().collect::<Vec<_>>(), expected, "round {round}: {input:?}");
    }

    for round in 0..60 {
        let index = [None, Some("by_group"), Some("by_score")][round % 3];
        let segment = (round % 2 == 1).then(|| (rng.u32(0..3), 3));
        let limit = if rng.bool() { None } else { Some(rng.u32(1..40)) };
        let mut expected: Vec<(StoredKey, StoredKey, &Item)> = items
            .iter()
            .filter_map(|item| {
                let index_key = match index {
                    None => table_key(item),
                    Some("by_group") => StoredKey::new(item.get("group")?.clone(), Some(item.get("rank")?.clone())),
                    Some(_) => StoredKey::new(item["pk"].clone(), Some(item.get("score")?.clone())),
                };
                Some((index_key, table_key(item), item))
            })
            .filter(|(index_key, _, _)| segment.is_none_or(|(segment, total)| index_key.segment(total) == segment))
            .collect();
        expected.sort_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
        let expected: Vec<&Item> = expected.into_iter().map(|(_, _, item)| item).collect();
        let mut input = ScanInput {
            table_name: "diff".into(),
            index_name: index.map(str::to_owned),
            limit,
            segment: segment.map(|(segment, _)| segment),
            total_segments: segment.map(|(_, total)| total),
            ..Default::default()
        };
        let mut found = Vec::new();
        loop {
            let page = backend.scan(input.clone()).await.unwrap();
            found.extend(page.items);
            match page.last_evaluated_key {
                Some(key) => input.exclusive_start_key = Some(key),
                None => break,
            }
        }
        assert_eq!(found.iter().collect::<Vec<_>>(), expected, "scan round {round}: {input:?}");
    }
}
