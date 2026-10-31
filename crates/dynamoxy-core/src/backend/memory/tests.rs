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
    wait_until_active(&backend, "items").await;
    backend
}

/// Polls `DescribeTable` until the table and its global indexes are active, like a waiter: a
/// table or index being created is `CREATING` in the first describe after it.
async fn wait_until_active(backend: &MemoryBackend, table: &str) -> TableDescription {
    for _ in 0..10 {
        let description = backend.describe_table(table).await.unwrap();
        let indexes_ready =
            description.global_secondary_indexes.iter().all(|index| index.index_status == Some(IndexStatus::Active));
        if description.table_status == TableStatus::Active && indexes_ready {
            return description;
        }
    }
    panic!("{table} did not become ACTIVE");
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
    assert_eq!(backend.describe_table("items").await.unwrap().table_status, TableStatus::Creating, "a first poll");
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
async fn local_index_projections_fetch_what_the_index_does_not_hold() {
    // A projection that names an attribute a local index does not hold reads each item from the
    // table, and the reads of the items are charged to the table; a projection of what the index
    // holds reads the index alone.
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let input = QueryInput {
        index_name: Some("by_rank".into()),
        projection_expression: Some("secret, #r".into()),
        expression_attribute_names: names(&[("#r", "rank")]),
        return_consumed_capacity: ReturnConsumedCapacity::Indexes,
        ..query("pk = :p AND #r < :limit", values(&[(":p", s("p")), (":limit", n(93))]))
    };
    let fetched = backend.query(input.clone()).await.unwrap();
    assert_eq!(fetched.items, [90, 91, 92].map(|rank| item([("secret", s("not projected")), ("rank", n(rank))])));
    let capacity = fetched.consumed_capacity.unwrap();
    assert_eq!(capacity.local_secondary_indexes["by_rank"].read_capacity_units, 0.5, "the index entries");
    assert_eq!(capacity.table.unwrap().read_capacity_units, 1.5, "a read of each item, rounded up on its own");
    assert_eq!(capacity.capacity_units, 2.0);

    let held = backend.query(QueryInput { projection_expression: Some("sk, #r".into()), ..input }).await.unwrap();
    assert_eq!(held.items[0], item([("sk", n(10)), ("rank", n(90))]));
    let capacity = held.consumed_capacity.unwrap();
    assert_eq!((capacity.capacity_units, capacity.table.unwrap().read_capacity_units), (0.5, 0.0));

    let scanned = backend
        .scan(ScanInput {
            table_name: "items".into(),
            index_name: Some("by_rank".into()),
            projection_expression: Some("#n".into()),
            expression_attribute_names: names(&[("#n", "name")]),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(scanned.count, 10, "the item without a rank is not in the index");
    assert!(scanned.items.iter().all(|item| item.contains_key("name")), "scans fetch too: {:?}", scanned.items);
}

#[tokio::test]
async fn index_reads_refuse_or_never_match_attributes_the_index_does_not_hold() {
    // Measured against DynamoDB in eu-north-1 on 2026-10-31 (docs/parity): a query filter on
    // attributes the index does not hold is refused, for local indexes too and whatever the query
    // returns, naming each place the filter reads one; a projection of a global index naming them
    // is refused in queries and scans. A scan's filter sees the index entries, so it matches
    // nothing on them; that it does so when the scan fetches the items from the table too (the
    // last two scans) is assumed from DynamoDB's PartiQL SELECT, until round 2 of the probes
    // measures it.
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let local = QueryInput {
        index_name: Some("by_rank".into()),
        filter_expression: Some("secret = :s OR attribute_not_exists(secret)".into()),
        ..query("pk = :p", values(&[(":p", s("p")), (":s", s("not projected"))]))
    };
    for (select, projection) in
        [(None, None), (Some(Select::AllAttributes), None), (Some(Select::Count), None), (None, Some("pk, secret"))]
    {
        let input = QueryInput { select, projection_expression: projection.map(Into::into), ..local.clone() };
        let error = backend.query(input).await.unwrap_err();
        assert_eq!(
            message(&error),
            "One or more parameter values were invalid: Secondary index by_rank does not project one or more filter attributes: [secret, secret]",
            "{select:?} {projection:?}"
        );
    }
    let global = QueryInput {
        index_name: Some("by_status".into()),
        expression_attribute_names: names(&[("#s", "status")]),
        ..query("#s = :odd", values(&[(":odd", s("odd"))]))
    };
    let error = backend
        .query(QueryInput {
            filter_expression: Some("secret = :v AND #n = :v".into()),
            expression_attribute_values: values(&[(":odd", s("odd")), (":v", s("x"))]),
            expression_attribute_names: names(&[("#s", "status"), ("#n", "name")]),
            ..global.clone()
        })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Secondary index by_status does not project one or more filter attributes: [secret]"
    );
    let error = backend
        .query(QueryInput {
            projection_expression: Some("secret, #s, #r".into()),
            expression_attribute_names: names(&[("#s", "status"), ("#r", "rank")]),
            ..global.clone()
        })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Global secondary index by_status does not project [secret, rank]"
    );
    let scan = |index: &str, filter: Option<&str>, projection: Option<&str>, select: Option<Select>| ScanInput {
        table_name: "items".into(),
        index_name: Some(index.into()),
        filter_expression: filter.map(Into::into),
        projection_expression: projection.map(Into::into),
        select,
        expression_attribute_values: if filter.is_some() {
            values(&[(":s", s("not projected"))])
        } else {
            Values::new()
        },
        ..Default::default()
    };
    let error = backend.scan(scan("by_status", None, Some("secret"), None)).await.unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Global secondary index by_status does not project [secret]"
    );
    for (index, projection, select) in [
        ("by_status", None, None),
        ("by_rank", None, None),
        ("by_rank", None, Some(Select::AllAttributes)),
        ("by_rank", Some("pk, secret"), None),
    ] {
        let scanned = backend.scan(scan(index, Some("secret = :s"), projection, select)).await.unwrap();
        assert_eq!((scanned.count, scanned.scanned_count), (0, 10), "{index} {projection:?} {select:?}");
    }
    let fetched = backend.scan(scan("by_rank", None, Some("pk, secret"), None)).await.unwrap();
    assert!(fetched.items.iter().all(|item| item.contains_key("secret")), "a local index fetches its projection");
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
async fn local_index_pages_count_the_items_they_fetch() {
    // A read of a local index that fetches the items from the table stops at 1 MB of the entries
    // (rounded up to 4 KB) and the fetched items (each rounded up to 4 KB), and is charged a read
    // of each fetched item on the table besides the read of the entries on the index.
    let backend = backend_with_table().await;
    let blob = "x".repeat(255 * 1024);
    for sort in 0..10 {
        put(&backend, item([("pk", s("big")), ("sk", n(sort)), ("rank", n(sort)), ("blob", s(&blob))])).await;
    }
    let table = backend.query(query("pk = :p", values(&[(":p", s("big"))]))).await.unwrap();
    assert_eq!(table.count, 5, "4 items of 255 KB stay below 1 MB");
    let input = QueryInput {
        index_name: Some("by_rank".into()),
        select: Some(Select::AllAttributes),
        consistent_read: true,
        return_consumed_capacity: ReturnConsumedCapacity::Indexes,
        ..query("pk = :p", values(&[(":p", s("big"))]))
    };
    let page = backend.query(input.clone()).await.unwrap();
    assert_eq!(page.count, 4, "4 entries (4 KB) and 4 items of 256 KB reach 1 MB");
    assert!(page.items.iter().all(|item| item.contains_key("blob")));
    let capacity = page.consumed_capacity.unwrap();
    assert_eq!(capacity.table.unwrap().read_capacity_units, 4.0 * 64.0, "a read of each item");
    assert_eq!(capacity.local_secondary_indexes["by_rank"].read_capacity_units, 1.0, "the index entries");
    let next = backend.query(QueryInput { exclusive_start_key: page.last_evaluated_key, ..input }).await.unwrap();
    assert_eq!(next.items.first().map(|item| &item["rank"]), Some(&n(4)));

    let projected = backend
        .scan(ScanInput {
            table_name: "items".into(),
            index_name: Some("by_rank".into()),
            projection_expression: Some("#b".into()),
            expression_attribute_names: names(&[("#b", "blob")]),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(projected.count, 4, "scans and projections that fetch count the items too");
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
    // A replay is billed as a transactional read of the item the first call wrote.
    let capacity = |output: TransactWriteItemsOutput| {
        output
            .consumed_capacity
            .iter()
            .map(|report| (report.capacity_units, report.read_capacity_units))
            .collect::<Vec<_>>()
    };
    let replay = TransactWriteItemsInput { return_consumed_capacity: ReturnConsumedCapacity::Total, ..input(1) };
    assert_eq!(capacity(backend.transact_write_items(replay.clone()).await.unwrap()), [(2.0, 2.0)]);
    assert_eq!(backend.transact_write_items(input(1)).await.unwrap().consumed_capacity, [], "unless not asked for");
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
    let at: i64 = 1_800_000_000;
    let year = 365 * 86_400;
    put(&backend, item([("pk", s("old")), ("sk", n(1)), ("expires", n(at - 4 * year))])).await;
    put(&backend, item([("pk", s("now")), ("sk", n(1)), ("expires", n(at))])).await;
    put(&backend, item([("pk", s("new")), ("sk", n(1)), ("expires", n(at + 1))])).await;
    put(&backend, item([("pk", s("none")), ("sk", n(1))])).await;
    // "To be considered for expiry and deletion, the TTL can't be more than five years in the
    // past": a TTL of 0 or of ten years ago is never deleted.
    put(&backend, item([("pk", s("zero")), ("sk", n(1)), ("expires", n(0))])).await;
    put(&backend, item([("pk", s("ancient")), ("sk", n(1)), ("expires", n(at - 10 * year))])).await;
    assert_eq!(backend.purge_expired(Some(at as f64)), 2, "old and now");
    let left = backend.scan(ScanInput { table_name: "items".into(), ..Default::default() }).await.unwrap();
    let mut keys: Vec<_> = left.items.iter().map(|item| item["pk"].clone()).collect();
    keys.sort_by_key(|key| format!("{key:?}"));
    assert_eq!(keys, [s("ancient"), s("new"), s("none"), s("zero")]);
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
    wait_until_active(&backend, "items").await; // waits for the index
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

#[tokio::test]
async fn tables_and_indexes_being_created_refuse_changes_like_dynamodb() {
    // Measured against DynamoDB in eu-north-1 on 2026-10-31 (docs/parity): a table DynamoDB is
    // still creating refuses UpdateTable and answers UpdateTimeToLive as if it did not exist, and
    // a second index cannot be added while one is being created. The in-memory backend creates
    // them at once, and they stay CREATING until a DescribeTable sees them, as a caller that waits.
    let backend = MemoryBackend::default();
    backend.create_table(table_input("items")).await.unwrap();
    let ttl = UpdateTimeToLiveInput { table_name: "items".into(), enabled: true, attribute_name: "expires".into() };
    let error = backend.update_time_to_live(ttl.clone()).await.unwrap_err();
    assert_eq!(
        (code(&error), message(&error)),
        ("ResourceNotFoundException", "Requested resource not found: Table: items not found".into())
    );
    let index = |name: &str, attribute: &str| UpdateTableInput {
        table_name: "items".into(),
        attribute_definitions: vec![definition(attribute, ScalarType::S)],
        global_secondary_index_updates: vec![GlobalSecondaryIndexUpdate::Create(GlobalSecondaryIndex {
            index_name: name.into(),
            key_schema: keys(attribute, None),
            projection: Projection::all(),
            provisioned_throughput: None,
            on_demand_throughput: None,
        })],
        ..Default::default()
    };
    let error = backend.update_table(index("by_email", "email")).await.unwrap_err();
    assert_eq!(
        (code(&error), message(&error)),
        (
            "ResourceInUseException",
            "Attempt to change a resource which is still in use: Table is being created: items".into()
        )
    );
    put(&backend, item([("pk", s("a")), ("sk", n(1))])).await; // items can be written meanwhile
    // A describe that sees it CREATING does not end it (a caller that looked once and did not
    // wait); the next poll sees it ACTIVE.
    assert_eq!(backend.describe_table("items").await.unwrap().table_status, TableStatus::Creating);
    assert_eq!(backend.update_time_to_live(ttl.clone()).await.unwrap_err().kind(), Some(ErrorKind::ResourceNotFound));
    assert_eq!(backend.describe_table("items").await.unwrap().table_status, TableStatus::Active);
    backend.update_time_to_live(ttl).await.unwrap();

    let updating = backend.update_table(index("by_email", "email")).await.unwrap();
    let status = |description: &TableDescription, name: &str| {
        description
            .global_secondary_indexes
            .iter()
            .find(|index| index.index_name == name)
            .and_then(|index| index.index_status)
    };
    assert_eq!(status(&updating, "by_email"), Some(IndexStatus::Creating));
    let error = backend.update_table(index("by_name", "name")).await.unwrap_err();
    assert_eq!(
        (code(&error), message(&error)),
        (
            "LimitExceededException",
            "Subscriber limit exceeded: Only 1 online index can be created or deleted simultaneously per table".into()
        )
    );
    let described = backend.describe_table("items").await.unwrap();
    assert_eq!(described.table_status, TableStatus::Active, "the table is ACTIVE again during the backfill");
    assert_eq!(status(&described, "by_email"), Some(IndexStatus::Creating));
    assert!(backend.update_table(index("by_name", "name")).await.is_err(), "still one index at a time");
    assert_eq!(status(&backend.describe_table("items").await.unwrap(), "by_email"), Some(IndexStatus::Active));
    backend.update_table(index("by_name", "name")).await.unwrap();
}

#[tokio::test]
async fn global_indexes_have_capacity_on_provisioned_tables_only() {
    // Measured against DynamoDB in eu-north-1 on 2026-10-31 (docs/parity).
    let backend = backend_with_table().await;
    let capacity = ProvisionedThroughput { read_capacity_units: 1, write_capacity_units: 1 };
    let provisioned = UpdateTableInput {
        table_name: "items".into(),
        billing_mode: Some(BillingMode::Provisioned),
        provisioned_throughput: Some(capacity),
        ..Default::default()
    };
    let error = backend.update_table(provisioned.clone()).await.unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: ProvisionedThroughput must be specified for index: by_status"
    );
    let index_capacity = GlobalSecondaryIndexUpdate::Update {
        index_name: "by_status".into(),
        provisioned_throughput: Some(capacity),
        on_demand_throughput: None,
    };
    backend
        .update_table(UpdateTableInput { global_secondary_index_updates: vec![index_capacity.clone()], ..provisioned })
        .await
        .unwrap();
    wait_until_active(&backend, "items").await;
    let error = backend
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
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Both ReadCapacityUnits and WriteCapacityUnits must be specified for index: by_email"
    );
    backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            billing_mode: Some(BillingMode::PayPerRequest),
            ..Default::default()
        })
        .await
        .unwrap();
    let described = backend.describe_table("items").await.unwrap();
    let throughput = described.global_secondary_indexes[0].provisioned_throughput;
    assert!(
        throughput.is_none_or(|capacity| (capacity.read_capacity_units, capacity.write_capacity_units) == (0, 0)),
        "on-demand indexes keep no capacity: {throughput:?}"
    );
    let error = backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            global_secondary_index_updates: vec![index_capacity],
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: The only Updates for index: by_status when TableThroughputMode is PAY_PER_REQUEST can be to OnDemandThroughput, WarmThroughput"
    );
}

#[tokio::test]
async fn refused_table_updates_change_nothing() {
    // DynamoDB validates the whole request before it applies any of it.
    let backend = backend_with_table().await;
    let stream = || StreamSpecification { enabled: true, view_type: Some(StreamViewType::NewImage) };
    backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            stream_specification: Some(stream()),
            ..Default::default()
        })
        .await
        .unwrap();
    let before = backend.describe_table("items").await.unwrap();
    let refused = [
        // Provisioned billing, then a stream that is already enabled.
        UpdateTableInput {
            table_name: "items".into(),
            billing_mode: Some(BillingMode::Provisioned),
            provisioned_throughput: Some(ProvisionedThroughput { read_capacity_units: 5, write_capacity_units: 5 }),
            stream_specification: Some(stream()),
            ..Default::default()
        },
        // A new index, then the same stream error.
        UpdateTableInput {
            table_name: "items".into(),
            attribute_definitions: vec![definition("email", ScalarType::S)],
            global_secondary_index_updates: vec![GlobalSecondaryIndexUpdate::Create(GlobalSecondaryIndex {
                index_name: "by_email".into(),
                key_schema: keys("email", None),
                projection: Projection::all(),
                provisioned_throughput: None,
                on_demand_throughput: None,
            })],
            stream_specification: Some(stream()),
            deletion_protection_enabled: Some(true),
            ..Default::default()
        },
        // Provisioned billing without throughput.
        UpdateTableInput {
            table_name: "items".into(),
            billing_mode: Some(BillingMode::Provisioned),
            ..Default::default()
        },
        // Throughput for an index that does not exist.
        UpdateTableInput {
            table_name: "items".into(),
            global_secondary_index_updates: vec![GlobalSecondaryIndexUpdate::Update {
                index_name: "missing".into(),
                provisioned_throughput: Some(ProvisionedThroughput { read_capacity_units: 1, write_capacity_units: 1 }),
                on_demand_throughput: None,
            }],
            deletion_protection_enabled: Some(true),
            ..Default::default()
        },
    ];
    for input in refused {
        backend.update_table(input).await.unwrap_err();
        assert_eq!(backend.describe_table("items").await.unwrap(), before);
    }
    // The table was left as it was, so later updates work.
    let description = backend
        .update_table(UpdateTableInput {
            table_name: "items".into(),
            deletion_protection_enabled: Some(true),
            ..Default::default()
        })
        .await
        .unwrap();
    assert!(description.deletion_protection_enabled);
    assert_eq!(description.billing_mode, BillingMode::PayPerRequest);
}

#[tokio::test]
async fn refused_tag_requests_add_no_tags() {
    let backend = backend_with_table().await;
    let arn = backend.describe_table("items").await.unwrap().table_arn.unwrap();
    let tags = |range: std::ops::Range<usize>| range.map(|index| (format!("k{index}"), "v".to_owned())).collect();
    backend.tag_resource(&arn, tags(0..45)).await.unwrap();
    let error = backend.tag_resource(&arn, tags(40..51)).await.unwrap_err(); // 6 new keys
    assert_eq!(code(&error), "LimitExceededException");
    assert_eq!(backend.list_tags_of_resource(&arn, None).await.unwrap().tags.len(), 45);
    // Tags that replace the values of existing keys count once.
    backend.tag_resource(&arn, tags(40..50)).await.unwrap();
    assert_eq!(backend.list_tags_of_resource(&arn, None).await.unwrap().tags.len(), 50);
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
        .unwrap_err();
    // DynamoDB refuses a batch that names one item twice before running any statement.
    assert_eq!(message(&batch), "Provided list of item keys contains duplicates");
    put(&backend, item([("pk", s("x")), ("sk", n(1))])).await;

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
async fn partiql_transactions_refuse_two_statements_on_one_item() {
    // Like TransactWriteItems and TransactGetItems, DynamoDB refuses a transaction that names an
    // item twice (whatever the key's order, number form or parameters) before it runs any of it.
    let backend = backend_with_table().await;
    backend.create_table(table_input("other")).await.unwrap();
    put(&backend, item([("pk", s("x")), ("sk", n(1)), ("a", n(0)), ("b", n(0))])).await;
    let transaction = |statements: &[(&str, Vec<AttributeValue>)]| ExecuteTransactionInput {
        transact_statements: statements
            .iter()
            .map(|(statement, parameters)| ParameterizedStatement {
                statement: (*statement).into(),
                parameters: parameters.clone(),
                ..Default::default()
            })
            .collect(),
        client_request_token: Some("token-1".into()),
        ..Default::default()
    };
    let set_a = ("UPDATE items SET a = 1 WHERE pk = 'x' AND sk = 1", vec![]);
    for statements in [
        vec![set_a.clone(), ("UPDATE items SET b = 2 WHERE pk = 'x' AND sk = 1", vec![])],
        vec![
            ("INSERT INTO items VALUE {'pk': 'y', 'sk': 1}", vec![]),
            ("DELETE FROM items WHERE pk = 'y' AND sk = 1", vec![]),
        ],
        vec![set_a.clone(), ("DELETE FROM items WHERE sk = 1.0 AND pk = ?", vec![s("x")])],
        vec![
            ("SELECT * FROM items WHERE pk = 'x' AND sk = 1", vec![]),
            ("SELECT a FROM items WHERE pk = ? AND sk = ?", vec![s("x"), n(1)]),
        ],
    ] {
        let error = backend.execute_transaction(transaction(&statements)).await.unwrap_err();
        assert_eq!(code(&error), "ValidationException", "{statements:?}");
        assert_eq!(message(&error), "Transaction request cannot include multiple operations on one item");
    }
    let key = |pk: &str, sk: i64| GetItemInput {
        table_name: "items".into(),
        key: item([("pk", s(pk)), ("sk", n(sk))]),
        ..Default::default()
    };
    let stored = backend.get_item(key("x", 1)).await.unwrap().item;
    assert_eq!(stored, Some(item([("pk", s("x")), ("sk", n(1)), ("a", n(0)), ("b", n(0))])), "nothing is written");
    assert!(backend.get_item(key("y", 1)).await.unwrap().item.is_none());

    // Other items of the partition, and the key in another table, are other items; the refused
    // transactions did not take the token.
    backend
        .execute_transaction(transaction(&[
            set_a,
            ("INSERT INTO items VALUE {'pk': 'x', 'sk': 2}", vec![]),
            ("INSERT INTO other VALUE {'pk': 'x', 'sk': 1}", vec![]),
        ]))
        .await
        .unwrap();
    assert_eq!(backend.get_item(key("x", 1)).await.unwrap().item.unwrap()["a"], n(1));
    assert!(backend.get_item(key("x", 2)).await.unwrap().item.is_some());
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

#[tokio::test]
async fn partiql_pages_continue_after_the_last_evaluated_key() {
    // A NextToken stands for the last item a page evaluated (its LastEvaluatedKey), as a query's
    // ExclusiveStartKey does: rows deleted or inserted before it do not shift the next page.
    let backend = backend_with_table().await;
    for sk in 0..8 {
        put(&backend, item([("pk", s("p")), ("sk", n(sk)), ("status", s("open")), ("rank", n(100 - sk))])).await;
    }
    let select = |text: &str, next_token: Option<String>| {
        let input = ExecuteStatementInput { statement: text.into(), limit: Some(2), next_token, ..Default::default() };
        backend.execute_statement(input)
    };
    let sort_keys = |output: &ExecuteStatementOutput| -> Vec<String> {
        output
            .items
            .iter()
            .map(|item| match &item["sk"] {
                AttributeValue::N(sk) => sk.as_str().to_owned(),
                other => panic!("unexpected sort key {other:?}"),
            })
            .collect()
    };
    let delete = |sk: i64| {
        let key = item([("pk", s("p")), ("sk", n(sk))]);
        backend.delete_item(DeleteItemInput { table_name: "items".into(), key, ..Default::default() })
    };
    for (statement, pages) in [
        ("SELECT * FROM items WHERE pk = 'p'", [["0", "1"], ["2", "3"], ["4", "5"]]),
        ("SELECT * FROM items WHERE pk = 'p' ORDER BY sk DESC", [["7", "6"], ["5", "4"], ["3", "2"]]),
        // The local index orders by rank (100 - sk), the global one by status and sk.
        ("SELECT * FROM items.\"by_rank\" WHERE pk = 'p'", [["7", "6"], ["5", "4"], ["3", "2"]]),
        ("SELECT * FROM items.\"by_status\" WHERE status = 'open'", [["0", "1"], ["2", "3"], ["4", "5"]]),
        ("SELECT * FROM items", [["0", "1"], ["2", "3"], ["4", "5"]]),
    ] {
        let first = select(statement, None).await.unwrap();
        assert_eq!(sort_keys(&first), pages[0], "{statement}");
        let last = first.items.last().unwrap();
        let mut last_key = item([("pk", last["pk"].clone()), ("sk", last["sk"].clone())]);
        if statement.contains("by_rank") {
            last_key.insert("rank".into(), last["rank"].clone());
        } else if statement.contains("by_status") {
            last_key.insert("status".into(), s("open"));
        }
        assert_eq!(first.last_evaluated_key, Some(last_key), "{statement}");
        // Delete what the first page returned: the second page starts where it would have.
        for row in sort_keys(&first) {
            delete(row.parse().unwrap()).await.unwrap();
        }
        let second = select(statement, first.next_token.clone()).await.unwrap();
        assert_eq!(sort_keys(&second), pages[1], "{statement}");
        // Items written again before the position are not read again.
        for row in sort_keys(&first) {
            let sk: i64 = row.parse().unwrap();
            put(&backend, item([("pk", s("p")), ("sk", n(sk)), ("status", s("open")), ("rank", n(100 - sk))])).await;
        }
        let third = select(statement, second.next_token.clone()).await.unwrap();
        assert_eq!(sort_keys(&third), pages[2], "{statement}");
        // Put the table back for the next statement.
        for sk in 0..8 {
            put(&backend, item([("pk", s("p")), ("sk", n(sk)), ("status", s("open")), ("rank", n(100 - sk))])).await;
        }
    }
    for token in ["offset:2", "key:zz", "key", "key:706b.S70"] {
        let error = select("SELECT * FROM items WHERE pk = 'p'", Some(token.into())).await.unwrap_err();
        assert_eq!(message(&error), "Invalid NextToken", "{token}");
    }
}

#[tokio::test]
async fn partiql_selects_of_a_local_index_fetch_what_it_does_not_hold() {
    // Like a query, a SELECT of a local index reads each item from the table when it names an
    // attribute the index does not hold, and is charged a read of each. The WHERE clause still
    // reads the index entries: the fetch serves the projection.
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let select = |text: &str| ExecuteStatementInput {
        statement: text.into(),
        consistent_read: true,
        return_consumed_capacity: ReturnConsumedCapacity::Total,
        ..Default::default()
    };
    let fetched =
        backend.execute_statement(select("SELECT secret, rank FROM items.\"by_rank\" WHERE pk = 'p'")).await.unwrap();
    assert_eq!(fetched.items.len(), 10);
    assert_eq!(fetched.items[0], item([("secret", s("not projected")), ("rank", n(90))]));
    assert_eq!(fetched.consumed_capacity.unwrap().capacity_units, 11.0, "the entries and each of the 10 items");
    let held =
        backend.execute_statement(select("SELECT sk, rank FROM items.\"by_rank\" WHERE pk = 'p'")).await.unwrap();
    assert_eq!(held.items[0], item([("sk", n(10)), ("rank", n(90))]));
    assert_eq!(held.consumed_capacity.unwrap().capacity_units, 1.0, "the entries");
    let entries = backend.execute_statement(select("SELECT * FROM items.\"by_rank\" WHERE pk = 'p'")).await.unwrap();
    assert_eq!(entries.items[0], item([("pk", s("p")), ("sk", n(10)), ("rank", n(90))]), "what the index holds");
    let filtered = backend
        .execute_statement(select("SELECT secret FROM items.\"by_rank\" WHERE secret = 'not projected'"))
        .await
        .unwrap();
    assert!(filtered.items.is_empty(), "the entries do not hold secret: {:?}", filtered.items);
}

#[tokio::test]
async fn partiql_selects_of_an_index_refuse_attributes_it_does_not_hold() {
    // DynamoDB refuses a projection of a global index naming attributes it does not hold, and a
    // WHERE clause keyed on an index's partition key that filters on them, on either kind of
    // index (captured by dynoxide-rs in eu-west-2, 2026-08-15). An unkeyed WHERE clause is a scan,
    // whose filter on them matches nothing, and a local index fetches its projection.
    let backend = backend_with_table().await;
    seed_query_data(&backend).await;
    let select =
        |text: &str| backend.execute_statement(ExecuteStatementInput { statement: text.into(), ..Default::default() });
    for (statement, expected) in [
        (
            "SELECT secret, sk, rank FROM items.\"by_status\" WHERE status = 'odd'",
            "Global secondary index by_status does not project [secret, rank]",
        ),
        (
            "SELECT name FROM items.\"by_status\" WHERE status = 'odd' AND secret = 's' AND rank > 1",
            "Secondary index by_status does not project one or more filter attributes: [secret, rank]",
        ),
        (
            "SELECT * FROM items.\"by_status\" WHERE status IN ['odd', 'even'] AND NOT (secret = 's' OR secret.a = 't')",
            "Secondary index by_status does not project one or more filter attributes: [secret]",
        ),
        (
            "SELECT * FROM items.\"by_rank\" WHERE pk = 'p' AND begins_with(name, 'item')",
            "Secondary index by_rank does not project one or more filter attributes: [name]",
        ),
    ] {
        let error = select(statement).await.unwrap_err();
        assert_eq!(code(&error), "ValidationException", "{statement}");
        assert_eq!(message(&error), format!("One or more parameter values were invalid: {expected}"), "{statement}");
    }
    for (statement, rows) in [
        // Scans: the filter reads the entries, which do not hold the attribute.
        ("SELECT * FROM items.\"by_status\" WHERE secret = 'not projected'", 0),
        ("SELECT * FROM items.\"by_rank\" WHERE pk = 'p' OR name = 'item 1'", 10),
        ("SELECT * FROM items.\"by_status\" WHERE status = 'odd' AND (sk = 1 OR name = 'item 3')", 2),
        // What the index holds, and a local index's projection, which it fetches.
        ("SELECT name, sk FROM items.\"by_status\" WHERE status = 'odd' AND name <> 'item 1'", 4),
        ("SELECT secret FROM items.\"by_rank\" WHERE pk = 'p' AND rank > 95", 4),
    ] {
        assert_eq!(select(statement).await.unwrap().items.len(), rows, "{statement}");
    }
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
async fn faults_for_a_table_apply_to_calls_that_use_it_among_others() {
    let backend = backend_with_table().await;
    backend.create_table(table_input("other")).await.unwrap();
    let fault = |kind: FaultKind| Fault { operation: None, table: Some("other".into()), kind, times: 1 };
    let writes = || BatchWriteItemInput {
        request_items: BTreeMap::from([
            ("items".into(), vec![WriteRequest::Put(item([("pk", s("a")), ("sk", n(1))]))]),
            ("other".into(), vec![WriteRequest::Put(item([("pk", s("b")), ("sk", n(1))]))]),
        ]),
        ..Default::default()
    };
    // Only the requests of the fault's table come back unprocessed; the others are written.
    backend.inject_fault(fault(FaultKind::Unprocessed));
    let output = backend.batch_write_item(writes()).await.unwrap();
    assert_eq!(output.unprocessed_items.keys().collect::<Vec<_>>(), ["other"]);
    let scan = |table: &str| ScanInput { table_name: table.into(), ..Default::default() };
    assert_eq!(
        (backend.scan(scan("items")).await.unwrap().count, backend.scan(scan("other")).await.unwrap().count),
        (1, 0)
    );
    backend.inject_fault(fault(FaultKind::Unprocessed));
    let reads = BatchGetItemInput {
        request_items: BTreeMap::from([
            (
                "items".into(),
                KeysAndAttributes { keys: vec![item([("pk", s("a")), ("sk", n(1))])], ..Default::default() },
            ),
            (
                "other".into(),
                KeysAndAttributes { keys: vec![item([("pk", s("b")), ("sk", n(1))])], ..Default::default() },
            ),
        ]),
        ..Default::default()
    };
    let output = backend.batch_get_item(reads).await.unwrap();
    assert_eq!(output.unprocessed_keys.keys().collect::<Vec<_>>(), ["other"]);
    assert_eq!(output.responses["items"].len(), 1);

    // Errors fire for the second table of a batch, a transaction and a PartiQL statement.
    let throttle = || fault(FaultKind::Error { code: "ThrottlingException".into(), message: "slow down".into() });
    backend.inject_fault(throttle());
    assert_eq!(code(&backend.batch_write_item(writes()).await.unwrap_err()), "ThrottlingException");
    backend.inject_fault(throttle());
    let transaction = TransactWriteItemsInput {
        transact_items: ["items", "other"]
            .into_iter()
            .map(|table| TransactWriteItem::Put {
                target: TransactTarget { table_name: table.into(), ..Default::default() },
                item: item([("pk", s("t")), ("sk", n(1))]),
            })
            .collect(),
        ..Default::default()
    };
    assert_eq!(code(&backend.transact_write_items(transaction).await.unwrap_err()), "ThrottlingException");
    backend.inject_fault(throttle());
    let select = ExecuteStatementInput { statement: "SELECT * FROM other".into(), ..Default::default() };
    assert_eq!(code(&backend.execute_statement(select.clone()).await.unwrap_err()), "ThrottlingException");
    backend.inject_fault(throttle());
    let unrelated = ExecuteStatementInput { statement: "SELECT * FROM items".into(), ..Default::default() };
    backend.execute_statement(unrelated).await.unwrap();
    assert_eq!(code(&backend.execute_statement(select).await.unwrap_err()), "ThrottlingException", "still pending");
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

fn key_query(condition: &str, values_: &[(&str, AttributeValue)]) -> QueryInput {
    QueryInput {
        table_name: "items".into(),
        key_condition_expression: Some(condition.into()),
        expression_attribute_values: values(values_),
        scan_index_forward: true,
        ..Default::default()
    }
}

#[tokio::test]
async fn key_conditions_refuse_empty_key_values() {
    // "One or more parameter values are not valid. The AttributeValue for a key attribute cannot
    // contain an empty string value. Key: PK" (reported against DynamoDB in getmoto/moto#7062).
    let backend = backend_with_table().await;
    let error = backend.query(key_query("pk = :p", &[(":p", s(""))])).await.unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values are not valid. The AttributeValue for a key attribute cannot contain an empty string value. Key: pk"
    );
    let index = |condition: &str, values_: &[(&str, AttributeValue)]| QueryInput {
        index_name: Some("by_status".into()),
        ..key_query(condition, values_)
    };
    let error = backend.query(index("#s = :s", &[(":s", s(""))]).with_names(&[("#s", "status")])).await.unwrap_err();
    assert!(message(&error).ends_with("cannot contain an empty string value. Key: status"), "{}", message(&error));
    backend.create_table(binary_table("blobs")).await.unwrap();
    let error = backend
        .query(QueryInput {
            table_name: "blobs".into(),
            key_condition_expression: Some("pk = :p AND begins_with(sk, :b)".into()),
            expression_attribute_values: values(&[(":p", s("a")), (":b", AttributeValue::B(Vec::new()))]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert!(message(&error).ends_with("cannot contain an empty binary value. Key: sk"), "{}", message(&error));
}

fn binary_table(name: &str) -> CreateTableInput {
    CreateTableInput {
        attribute_definitions: vec![definition("pk", ScalarType::S), definition("sk", ScalarType::B)],
        key_schema: keys("pk", Some("sk")),
        local_secondary_indexes: vec![],
        global_secondary_indexes: vec![],
        ..table_input(name)
    }
}

trait WithNames {
    fn with_names(self, pairs: &[(&str, &str)]) -> Self;
}

impl WithNames for QueryInput {
    fn with_names(self, pairs: &[(&str, &str)]) -> Self {
        QueryInput { expression_attribute_names: names(pairs), ..self }
    }
}

#[tokio::test]
async fn start_keys_must_fit_the_target_and_the_key_condition() {
    // Captured against DynamoDB by dynoxide-rs (tests/query_start_key_and_key_conditions.rs).
    let backend = backend_with_table().await;
    for sk in 1..=3 {
        put(&backend, item([("pk", s("a")), ("sk", n(sk)), ("status", s("open")), ("rank", n(sk))])).await;
    }
    let from = |start: Item| QueryInput { exclusive_start_key: Some(start), ..key_query("pk = :p", &[(":p", s("a"))]) };
    let refused = async |input: QueryInput| message(&backend.query(input).await.unwrap_err());
    const INVALID: &str = "The provided starting key is invalid";
    assert_eq!(refused(from(item([("pk", n(1)), ("sk", n(1))]))).await, INVALID, "a number partition key");
    assert_eq!(refused(from(item([("pk", s("a"))]))).await, INVALID, "no sort key");
    assert_eq!(refused(from(item([("pk", s("a")), ("sk", n(1)), ("extra", s("x"))]))).await, INVALID);
    assert_eq!(
        refused(from(item([("pk", s("")), ("sk", n(1))]))).await,
        "One or more parameter values are not valid. The AttributeValue for a key attribute cannot contain an empty string value. Key: pk"
    );
    // On an index: the table key and the index key, each of its type, and nothing else.
    let on_index = |start: Item| QueryInput {
        index_name: Some("by_status".into()),
        exclusive_start_key: Some(start),
        ..key_query("#s = :s", &[(":s", s("open"))]).with_names(&[("#s", "status")])
    };
    assert_eq!(refused(on_index(item([("pk", s("a")), ("sk", n(1)), ("status", n(5))]))).await, INVALID);
    assert_eq!(refused(on_index(item([("pk", s("a")), ("sk", n(1))]))).await, INVALID, "the table's keys only");
    assert_eq!(
        refused(on_index(item([("pk", s("a")), ("sk", n(1)), ("status", s("open")), ("rank", n(1))]))).await,
        INVALID,
        "another index's key"
    );
    let page = backend.query(on_index(item([("pk", s("a")), ("sk", n(1)), ("status", s("open"))]))).await.unwrap();
    assert_eq!(page.count, 2, "a fitting start key continues");
    // Outside the key condition.
    assert_eq!(
        refused(from(item([("pk", s("b")), ("sk", n(1))]))).await,
        "The provided starting key is outside query boundaries based on provided conditions"
    );
    let bounded = |start: Item| QueryInput {
        exclusive_start_key: Some(start),
        ..key_query("pk = :p AND sk >= :low", &[(":p", s("a")), (":low", n(3))])
    };
    assert_eq!(
        refused(bounded(item([("pk", s("a")), ("sk", n(1))]))).await,
        "The provided starting key does not match the range key predicate"
    );
    assert_eq!(
        refused(bounded(item([("pk", s("b")), ("sk", n(5))]))).await,
        "The query can return at most one row and cannot be restarted"
    );
    assert_eq!(backend.query(bounded(item([("pk", s("a")), ("sk", n(3))]))).await.unwrap().count, 0);
    assert_eq!(
        refused(on_index(item([("pk", s("a")), ("sk", n(1)), ("status", s("closed"))]))).await,
        "The provided starting key is outside query boundaries based on provided conditions"
    );
    let hash_only = CreateTableInput {
        attribute_definitions: vec![definition("pk", ScalarType::S)],
        key_schema: keys("pk", None),
        local_secondary_indexes: vec![],
        global_secondary_indexes: vec![],
        ..table_input("hash")
    };
    backend.create_table(hash_only).await.unwrap();
    let error = backend
        .query(QueryInput {
            table_name: "hash".into(),
            exclusive_start_key: Some(item([("pk", s("b"))])),
            ..key_query("pk = :p", &[(":p", s("a"))])
        })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "The provided exclusive start key is invalid");
    // A scan of an index names the index when an index key has another type.
    let error = backend
        .scan(ScanInput {
            table_name: "items".into(),
            index_name: Some("by_status".into()),
            exclusive_start_key: Some(item([("pk", s("a")), ("sk", n(1)), ("status", n(5))])),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Type mismatch for Index Key status Expected: S Actual: N IndexName: by_status"
    );
}

#[tokio::test]
async fn secondary_index_keys_have_the_size_limits_of_table_keys() {
    let backend = backend_with_table().await;
    let write = |extra: (&str, AttributeValue)| PutItemInput {
        table_name: "items".into(),
        item: item([("pk", s("a")), ("sk", n(1)), extra]),
        ..Default::default()
    };
    let error = backend.put_item(write(("status", s(&"x".repeat(3000))))).await.unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Size of hashkey has exceeded the maximum size limit of2048 bytes"
    );
    backend.put_item(write(("status", s(&"x".repeat(2048))))).await.unwrap();
    let sorted = CreateTableInput {
        attribute_definitions: vec![
            definition("pk", ScalarType::S),
            definition("sk", ScalarType::N),
            definition("tag", ScalarType::S),
        ],
        local_secondary_indexes: vec![LocalSecondaryIndex {
            index_name: "by_tag".into(),
            key_schema: keys("pk", Some("tag")),
            projection: Projection { projection_type: ProjectionType::KeysOnly, non_key_attributes: vec![] },
        }],
        global_secondary_indexes: vec![],
        ..table_input("tagged")
    };
    backend.create_table(sorted).await.unwrap();
    let error = backend
        .put_item(PutItemInput { table_name: "tagged".into(), ..write(("tag", s(&"x".repeat(1025)))) })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values were invalid: Aggregated size of all range keys has exceeded the size limit of 1024 bytes"
    );
    let update = UpdateItemInput {
        table_name: "items".into(),
        key: item([("pk", s("a")), ("sk", n(1))]),
        update_expression: Some("SET #s = :s".into()),
        expression_attribute_names: names(&[("#s", "status")]),
        expression_attribute_values: values(&[(":s", s(&"y".repeat(2049)))]),
        ..Default::default()
    };
    let error = backend.update_item(update).await.unwrap_err();
    assert!(message(&error).contains("Size of hashkey has exceeded"), "{}", message(&error));
}

#[tokio::test]
async fn updates_count_toward_the_size_of_a_transaction() {
    let backend = backend_with_table().await;
    let blob = "x".repeat(390 * 1024);
    let updates = (0..11)
        .map(|sk| TransactWriteItem::Update {
            target: TransactTarget {
                table_name: "items".into(),
                expression_attribute_names: names(&[("#b", "blob")]),
                expression_attribute_values: values(&[(":b", s(&blob))]),
                ..Default::default()
            },
            key: item([("pk", s("a")), ("sk", n(sk))]),
            update_expression: "SET #b = :b".into(),
        })
        .collect();
    let error = backend
        .transact_write_items(TransactWriteItemsInput { transact_items: updates, ..Default::default() })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Transaction size has exceeded the maximum allowed size");
    let left = backend.scan(ScanInput { table_name: "items".into(), ..Default::default() }).await.unwrap();
    assert_eq!(left.count, 0, "nothing is written");
}

fn statement(text: &str) -> ParameterizedStatement {
    ParameterizedStatement { statement: text.into(), ..Default::default() }
}

fn run(text: &str) -> ExecuteStatementInput {
    ExecuteStatementInput { statement: text.into(), ..Default::default() }
}

#[tokio::test]
async fn partiql_batches_refuse_mixed_and_repeated_items_and_return_old_items() {
    // Captured against DynamoDB by dynoxide-rs (src/actions/batch_execute_statement.rs,
    // tests/partiql_condition_failure_item.rs).
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("v", n(1))])).await;
    let batch =
        |statements: Vec<ParameterizedStatement>| BatchExecuteStatementInput { statements, ..Default::default() };
    let error = backend
        .batch_execute_statement(batch(vec![
            statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
            statement("INSERT INTO items VALUE {'pk': 'b', 'sk': 1}"),
        ]))
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Read and write requests together in the same batch is not supported.");
    let error = backend
        .batch_execute_statement(batch(vec![
            statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
            statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
        ]))
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Provided list of item keys contains duplicates");
    let failing = ParameterizedStatement {
        return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
        ..statement("UPDATE items SET v = 2 WHERE pk = 'a' AND sk = 1 AND v = 5")
    };
    let output = backend.batch_execute_statement(batch(vec![failing.clone()])).await.unwrap();
    let error = output.responses[0].error.as_ref().unwrap();
    assert_eq!(
        (error.code.as_str(), error.message.as_deref()),
        ("ConditionalCheckFailed", Some("The conditional request failed"))
    );
    assert_eq!(error.item.as_ref().unwrap()["v"], n(1), "the stored item, as ALL_OLD asks");
    let output = backend
        .batch_execute_statement(batch(vec![statement("UPDATE items SET v = 2 WHERE pk = 'a' AND sk = 1 AND v = 5")]))
        .await
        .unwrap();
    assert!(output.responses[0].error.as_ref().unwrap().item.is_none());
    // Reads of a missing table, and reads without the full key.
    let output = backend
        .batch_execute_statement(BatchExecuteStatementInput {
            statements: vec![
                statement("SELECT * FROM missing WHERE pk = 'a'"),
                statement("SELECT * FROM items WHERE pk = 'a'"),
            ],
            return_consumed_capacity: ReturnConsumedCapacity::Total,
        })
        .await
        .unwrap();
    let error = output.responses[0].error.as_ref().unwrap();
    assert_eq!(
        (error.code.as_str(), error.message.as_deref()),
        ("ResourceNotFound", Some("Requested resource not found"))
    );
    let error = output.responses[1].error.as_ref().unwrap();
    assert_eq!(
        error.message.as_deref(),
        Some("Select statements within BatchExecuteStatement must specify the primary key in the where clause.")
    );
    assert_eq!(output.responses[1].table_name, None);
    // Each keyed read is charged on its table.
    let output = backend
        .batch_execute_statement(BatchExecuteStatementInput {
            statements: vec![
                statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
                statement("SELECT * FROM items WHERE pk = 'z' AND sk = 1"),
            ],
            return_consumed_capacity: ReturnConsumedCapacity::Total,
        })
        .await
        .unwrap();
    assert_eq!(output.consumed_capacity.iter().map(|report| report.capacity_units).collect::<Vec<_>>(), [1.0]);
}

#[tokio::test]
async fn partiql_transactions_follow_dynamodb_rules() {
    // Captured against DynamoDB by dynoxide-rs (src/actions/execute_transaction.rs).
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("v", n(1))])).await;
    let transaction = |statements: Vec<ParameterizedStatement>| ExecuteTransactionInput {
        transact_statements: statements,
        ..Default::default()
    };
    let refused =
        async |input: ExecuteTransactionInput| message(&backend.execute_transaction(input).await.unwrap_err());
    assert_eq!(
        refused(transaction(vec![statement("UPDATE items SET v = 2 WHERE pk = 'a' AND sk = 1 RETURNING ALL NEW *")]))
            .await,
        "Validation failed in TransactStatements[0]: RETURNING clause is not supported in ExecuteTransaction."
    );
    assert_eq!(
        refused(transaction(vec![
            statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
            statement("INSERT INTO items VALUE {'pk': 'b', 'sk': 1}"),
        ]))
        .await,
        "ExecuteTransaction API does not support both read and write operations in the same request."
    );
    assert_eq!(
        refused(transaction(vec![
            statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1"),
            statement("SELECT * FROM items")
        ]))
        .await,
        "Validation failed in TransactStatements[1]: Select statements within ExecuteTransaction must specify the primary key in the where clause."
    );
    let token = "t".repeat(80);
    assert_eq!(
        refused(ExecuteTransactionInput {
            client_request_token: Some(token.clone()),
            ..transaction(vec![statement("INSERT INTO items VALUE {'pk': 'b', 'sk': 1}")])
        })
        .await,
        format!(
            "1 validation error detected: Value '{token}' at 'clientRequestToken' failed to satisfy constraint: Member must have length less than or equal to 36"
        )
    );
    // A failed condition returns the stored item, as ALL_OLD asks.
    let failing = ParameterizedStatement {
        return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
        ..statement("UPDATE items SET v = 2 WHERE pk = 'a' AND sk = 1 AND v = 5")
    };
    let error = backend.execute_transaction(transaction(vec![failing])).await.unwrap_err();
    let reasons = &error.as_service().unwrap().cancellation_reasons;
    assert_eq!(reasons[0].item.as_ref().unwrap()["v"], n(1));
    // A read transaction costs 2 units per 4 KB item, on the table.
    let output = backend
        .execute_transaction(ExecuteTransactionInput {
            return_consumed_capacity: ReturnConsumedCapacity::Total,
            ..transaction(vec![statement("SELECT * FROM items WHERE pk = 'a' AND sk = 1")])
        })
        .await
        .unwrap();
    assert_eq!(output.consumed_capacity[0].capacity_units, 2.0);
}

#[tokio::test]
async fn partiql_statements_are_refused_as_dynamodb_refuses_them() {
    // Captured against DynamoDB by dynoxide-rs (tests/partiql_*.rs), except the empty UPDATE.
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("tags", AttributeValue::Ss(vec!["x".into()])), ("n", n(1))]))
        .await;
    let refused = async |text: &str| message(&backend.execute_statement(run(text)).await.unwrap_err());
    assert_eq!(
        refused("UPDATE items SET v = 1 WHERE pk = 'a' AND pk = 'b' AND sk = 1").await,
        "Multiple conditions on same key pk. Only single item Update/Insert/Delete are supported"
    );
    backend.execute_statement(run("UPDATE items SET v = 1 WHERE pk = 'a' AND pk = 'a' AND sk = 1")).await.unwrap();
    assert_eq!(
        refused("DELETE FROM items WHERE pk = 'a' AND sk = 1 RETURNING ALL NEW *").await,
        "Invalid returning clause: RETURNING ALL NEW *. Only RETURNING ALL OLD * is allowed in DELETE statements."
    );
    assert_eq!(
        refused("INSERT INTO items.by_status VALUE {'pk': 'b', 'sk': 1}").await,
        "FROM clause may only contain a single table name in data manipulation statements"
    );
    assert_eq!(
        refused("UPDATE items.by_status SET v = 1 WHERE pk = 'a' AND sk = 1").await,
        "This operation is not supported on an index"
    );
    assert_eq!(
        refused("DELETE FROM items.by_status WHERE pk = 'a' AND sk = 1").await,
        "This operation is not supported on an index"
    );
    assert_eq!(
        refused("UPDATE items WHERE pk = 'a' AND sk = 1").await,
        "Statement wasn't well formed, can't be processed: UPDATE requires at least one SET or REMOVE clause"
    );
    assert_eq!(
        refused("SELECT pk FROM \"items\" WHERE pk = 'a' AND EXISTS(tags)").await,
        "Unrecognized function: exists at 1:43:6"
    );
    let found =
        backend.execute_statement(run("SELECT * FROM items WHERE pk = 'a' AND attribute_exists(tags)")).await.unwrap();
    assert_eq!(found.items.len(), 1);
    let error = backend
        .execute_statement(ExecuteStatementInput { limit: Some(0), ..run("SELECT * FROM items") })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "1 validation error detected: Value '0' at 'limit' failed to satisfy constraint: Member must have value greater than or equal to 1"
    );
    // SET_ADD and SET_DELETE take the assigned attribute and a set.
    assert_eq!(
        refused("UPDATE \"items\" SET t = set_add(tags, <<'y'>>) WHERE pk = 'a' AND sk = 1").await,
        "The first argument to SET_ADD must equal the assignment value at 1:24:7"
    );
    assert_eq!(
        refused("UPDATE \"items\" SET n = set_add(n, 5) WHERE pk = 'a' AND sk = 1").await,
        "The second argument to SET_ADD must be a value with type SET at 1:32:1"
    );
    assert_eq!(
        refused("UPDATE \"items\" SET tags = set_delete(tags) WHERE pk = 'a' AND sk = 1").await,
        "SET_DELETE must have exactly two arguments at 1:27:10"
    );
    backend
        .execute_statement(run("UPDATE items SET tags = set_add(tags, <<'y'>>) WHERE pk = 'a' AND sk = 1"))
        .await
        .unwrap();
    // Negative numbers after keywords.
    for text in [
        "SELECT * FROM items WHERE pk = 'a' AND n BETWEEN -5 AND 5",
        "SELECT * FROM items WHERE pk = 'a' AND (n = 7 OR -3 < n)",
    ] {
        assert_eq!(backend.execute_statement(run(text)).await.unwrap().items.len(), 1, "{text}");
    }
}

#[tokio::test]
async fn partiql_capacity_follows_dynamodb() {
    // Captured against DynamoDB by dynoxide-rs (tests/partiql_index_consumed_capacity.rs).
    let backend = backend_with_table().await;
    put(&backend, item([("pk", s("a")), ("sk", n(1)), ("status", s("open"))])).await;
    let with = |text: &str, mode| ExecuteStatementInput { return_consumed_capacity: mode, ..run(text) };
    let output = backend
        .execute_statement(with("SELECT * FROM items.by_status WHERE status = 'open'", ReturnConsumedCapacity::Indexes))
        .await
        .unwrap();
    let capacity = output.consumed_capacity.unwrap();
    assert_eq!(capacity.table.unwrap().capacity_units, 0.0);
    assert_eq!(capacity.global_secondary_indexes["by_status"].capacity_units, 0.5);
    let output = backend
        .execute_statement(with("DELETE FROM items WHERE pk = 'missing' AND sk = 1", ReturnConsumedCapacity::Total))
        .await
        .unwrap();
    assert_eq!(output.consumed_capacity.unwrap().capacity_units, 1.0, "a delete of a missing item costs a write");
    // A transaction doubles the table's write units, not the indexes'.
    let output = backend
        .execute_transaction(ExecuteTransactionInput {
            transact_statements: vec![statement("INSERT INTO items VALUE {'pk': 'b', 'sk': 1, 'status': 'open'}")],
            return_consumed_capacity: ReturnConsumedCapacity::Indexes,
            ..Default::default()
        })
        .await
        .unwrap();
    let capacity = &output.consumed_capacity[0];
    assert_eq!(capacity.table.unwrap().capacity_units, 2.0);
    assert_eq!(capacity.global_secondary_indexes["by_status"].capacity_units, 1.0);
    assert_eq!(capacity.capacity_units, 3.0);
}

#[tokio::test]
async fn names_values_and_messages_follow_dynamodb() {
    let backend = backend_with_table().await;
    // Empty attribute names (captured against DynamoDB by dynoxide-rs, src/empty_names.rs).
    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1)), ("", n(1))]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Empty attribute name");
    let nested = AttributeValue::M(item([("", n(1))]));
    let error = backend
        .put_item(PutItemInput {
            table_name: "items".into(),
            item: item([("pk", s("a")), ("sk", n(1)), ("m", nested)]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "Empty attribute name");
    let error = backend
        .get_item(GetItemInput {
            table_name: "items".into(),
            key: item([("pk", s("a")), ("sk", n(1))]),
            projection_expression: Some("#e".into()),
            expression_attribute_names: names(&[("#e", "")]),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(message(&error), "ExpressionAttributeNames contains invalid value: Empty attribute name for key #e");
    // contains() of a path in itself.
    let error = backend
        .scan(ScanInput {
            table_name: "items".into(),
            filter_expression: Some("contains(a, a)".into()),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(
        message(&error),
        "Invalid FilterExpression: The first operand must be distinct from the remaining operands for this operator or function; operator: contains, first operand: [a]"
    );
    // An update that makes an item too large, and an update to an empty index key.
    put(&backend, item([("pk", s("a")), ("sk", n(1))])).await;
    let update = |expression: &str, value: AttributeValue| UpdateItemInput {
        table_name: "items".into(),
        key: item([("pk", s("a")), ("sk", n(1))]),
        update_expression: Some(expression.into()),
        expression_attribute_names: names(if expression.contains("#b") {
            &[("#a", "status"), ("#b", "blob")]
        } else {
            &[("#a", "status")]
        }),
        expression_attribute_values: values(&[(":v", value)]),
        ..Default::default()
    };
    let error = backend.update_item(update("SET #b = :v REMOVE #a", s(&"x".repeat(400 * 1024)))).await.unwrap_err();
    assert_eq!(message(&error), "Item size to update has exceeded the maximum allowed size");
    let error = backend.update_item(update("SET #a = :v", s(""))).await.unwrap_err();
    assert_eq!(
        message(&error),
        "One or more parameter values are not valid. The update expression attempted to update a secondary index key to a value that is not supported. The AttributeValue for a key attribute cannot contain an empty string value."
    );
}
