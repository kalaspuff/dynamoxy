//! End-to-end scenarios run against the in-memory backend and, when
//! `DYNAMOXY_TEST_ENDPOINT_URL` is set, against a DynamoDB emulator (MiniStack) through the AWS
//! adapter. Both runs make the same assertions: this is where backend parity is checked.
//!
//! ```bash
//! uv run ministack &   # or any DynamoDB-compatible endpoint
//! DYNAMOXY_TEST_ENDPOINT_URL=http://localhost:4566 cargo test -p dynamoxy-core --test emulator
//! ```

use std::collections::BTreeMap;
use std::time::Duration;

use dynamoxy_core::types::*;
use dynamoxy_core::value::item;
use dynamoxy_core::{AttributeValue, Client, ClientConfig, ErrorKind, Item};

fn endpoint() -> Option<String> {
    std::env::var("DYNAMOXY_TEST_ENDPOINT_URL").ok().filter(|value| !value.is_empty())
}

fn unique(prefix: &str) -> String {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos();
    format!("{prefix}-{}-{nanos:x}", std::process::id())
}

async fn clients() -> Vec<(&'static str, Client)> {
    let mut clients = vec![("memory", Client::memory(ClientConfig::default(), None).unwrap())];
    match endpoint() {
        Some(endpoint) => {
            let config = ClientConfig {
                endpoint_url: Some(endpoint),
                region: Some("us-east-1".into()),
                access_key_id: Some("test".into()),
                secret_access_key: Some("test".into()),
                ..ClientConfig::default()
            };
            clients.push(("emulator", Client::connect(config).await.unwrap()));
        }
        None => eprintln!("skipping the emulator run: DYNAMOXY_TEST_ENDPOINT_URL is not set"),
    }
    clients
}

fn s(value: &str) -> AttributeValue {
    AttributeValue::s(value)
}

fn n(value: i64) -> AttributeValue {
    AttributeValue::n(value)
}

fn values(pairs: &[(&str, AttributeValue)]) -> BTreeMap<String, AttributeValue> {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), value.clone())).collect()
}

async fn create_table(client: &Client, name: &str) {
    let input = CreateTableInput {
        table_name: name.into(),
        attribute_definitions: vec![
            AttributeDefinition { name: "pk".into(), attribute_type: ScalarType::S },
            AttributeDefinition { name: "sk".into(), attribute_type: ScalarType::S },
            AttributeDefinition { name: "gsi_pk".into(), attribute_type: ScalarType::S },
        ],
        key_schema: KeySchema { partition_key: "pk".into(), sort_key: Some("sk".into()) },
        local_secondary_indexes: vec![],
        global_secondary_indexes: vec![GlobalSecondaryIndex {
            index_name: "gsi1".into(),
            key_schema: KeySchema { partition_key: "gsi_pk".into(), sort_key: Some("sk".into()) },
            projection: Projection::all(),
            provisioned_throughput: None,
            on_demand_throughput: None,
        }],
        billing_mode: BillingMode::PayPerRequest,
        provisioned_throughput: None,
        on_demand_throughput: None,
        stream_specification: None,
        sse_specification: None,
        tags: BTreeMap::from([("suite".into(), "dynamoxy".into())]),
        table_class: None,
        deletion_protection_enabled: false,
    };
    client.create_table(input).await.result.unwrap();
    client.wait_for_table(name, true, Duration::from_secs(30)).await.unwrap();
}

fn key(pk: &str, sk: &str) -> Item {
    item([("pk", s(pk)), ("sk", s(sk))])
}

#[tokio::test(flavor = "multi_thread")]
async fn item_lifecycle_conditions_and_updates() {
    for (backend, client) in clients().await {
        let table = unique("items");
        create_table(&client, &table).await;
        let put = PutItemInput {
            table_name: table.clone(),
            item: item([("pk", s("u1")), ("sk", s("PROFILE")), ("name", s("Ada")), ("visits", n(1))]),
            condition_expression: Some("attribute_not_exists(pk)".into()),
            ..Default::default()
        };
        client.put_item(put.clone()).await.result.unwrap();
        let error = client
            .put_item(PutItemInput {
                return_values_on_condition_check_failure: ReturnValuesOnConditionCheckFailure::AllOld,
                ..put.clone()
            })
            .await
            .result
            .unwrap_err();
        let service = error.as_service().unwrap_or_else(|| panic!("{backend}: {error}"));
        assert_eq!(service.kind, ErrorKind::ConditionalCheckFailed, "{backend}");
        assert_eq!(service.item.as_ref().map(|old| old["name"].clone()), Some(s("Ada")), "{backend}");

        let updated = client
            .update_item(UpdateItemInput {
                table_name: table.clone(),
                key: key("u1", "PROFILE"),
                update_expression: Some("SET #n = :name ADD visits :one".into()),
                expression_attribute_names: BTreeMap::from([("#n".into(), "name".into())]),
                expression_attribute_values: values(&[(":name", s("Grace")), (":one", n(1))]),
                return_values: ReturnValues::AllNew,
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        let attributes = updated.attributes.unwrap();
        assert_eq!((attributes["name"].clone(), attributes["visits"].clone()), (s("Grace"), n(2)), "{backend}");

        let error = client
            .update_item(UpdateItemInput {
                table_name: table.clone(),
                key: key("u1", "PROFILE"),
                update_expression: Some("SET address.city = :city".into()),
                expression_attribute_values: values(&[(":city", s("Oslo"))]),
                ..Default::default()
            })
            .await
            .result
            .unwrap_err();
        assert_eq!(error.kind(), Some(ErrorKind::Validation), "{backend}: {error}");

        let fetched = client
            .get_item(GetItemInput {
                table_name: table.clone(),
                key: key("u1", "PROFILE"),
                consistent_read: true,
                projection_expression: Some("visits".into()),
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        assert_eq!(fetched.item, Some(item([("visits", n(2))])), "{backend}");

        let deleted = client
            .delete_item(DeleteItemInput {
                table_name: table.clone(),
                key: key("u1", "PROFILE"),
                return_values: ReturnValues::AllOld,
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        assert_eq!(deleted.attributes.unwrap()["name"], s("Grace"), "{backend}");
        client.delete_table(&table).await.result.unwrap();
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn queries_indexes_and_batches() {
    for (backend, client) in clients().await {
        let table = unique("query");
        create_table(&client, &table).await;
        let writes: Vec<(String, WriteRequest)> = (0..40)
            .map(|index| {
                let mut row = item([("pk", s("orders")), ("sk", s(&format!("ORDER#{index:03}"))), ("total", n(index))]);
                if index % 4 == 0 {
                    row.insert("gsi_pk".into(), s("flagged"));
                }
                (table.clone(), WriteRequest::Put(row))
            })
            .collect();
        let result = client.batch_write(writes, ReturnConsumedCapacity::None).await.result.unwrap();
        assert!(result.unprocessed.is_empty(), "{backend}");

        let mut input = QueryInput {
            table_name: table.clone(),
            key_condition_expression: Some("pk = :pk AND begins_with(sk, :prefix)".into()),
            filter_expression: Some("#t >= :min".into()),
            expression_attribute_names: BTreeMap::from([("#t".into(), "total".into())]),
            expression_attribute_values: values(&[(":pk", s("orders")), (":prefix", s("ORDER#")), (":min", n(30))]),
            scan_index_forward: false,
            limit: Some(15),
            ..Default::default()
        };
        let mut totals = Vec::new();
        loop {
            let page = client.query(input.clone()).await.result.unwrap();
            totals.extend(page.items.iter().map(|row| row["total"].clone()));
            match page.last_evaluated_key {
                Some(start) => input.exclusive_start_key = Some(start),
                None => break,
            }
        }
        assert_eq!(totals, (30..40).rev().map(n).collect::<Vec<_>>(), "{backend}");

        let flagged = client
            .query(QueryInput {
                table_name: table.clone(),
                index_name: Some("gsi1".into()),
                key_condition_expression: Some("gsi_pk = :flag".into()),
                expression_attribute_values: values(&[(":flag", s("flagged"))]),
                select: Some(Select::Count),
                scan_index_forward: true,
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        assert_eq!(flagged.count, 10, "{backend}");

        let keys: Vec<Item> = (0..40).map(|index| key("orders", &format!("ORDER#{index:03}"))).collect();
        let request = BTreeMap::from([(table.clone(), KeysAndAttributes { keys, ..Default::default() })]);
        let fetched = client.batch_get(request, ReturnConsumedCapacity::None).await.result.unwrap();
        assert_eq!(fetched.items[&table].len(), 40, "{backend}");

        let scanned = client
            .scan(ScanInput { table_name: table.clone(), select: Some(Select::Count), ..Default::default() })
            .await
            .result
            .unwrap();
        assert_eq!(scanned.count, 40, "{backend}");
        client.delete_table(&table).await.result.unwrap();
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn transactions_report_cancellation_reasons() {
    for (backend, client) in clients().await {
        let table = unique("txn");
        create_table(&client, &table).await;
        let target = |condition: Option<&str>| TransactTarget {
            table_name: table.clone(),
            condition_expression: condition.map(str::to_owned),
            ..Default::default()
        };
        let create = |sk: &str| TransactWriteItem::Put {
            target: target(Some("attribute_not_exists(pk)")),
            item: item([("pk", s("unique")), ("sk", s(sk))]),
        };
        let input = TransactWriteItemsInput { transact_items: vec![create("a"), create("b")], ..Default::default() };
        client.transact_write_items(input.clone()).await.result.unwrap();
        let error = client
            .transact_write_items(TransactWriteItemsInput {
                transact_items: vec![create("c"), create("b")],
                ..Default::default()
            })
            .await
            .result
            .unwrap_err();
        let service = error.as_service().unwrap_or_else(|| panic!("{backend}: {error}"));
        assert_eq!(service.kind, ErrorKind::TransactionCanceled, "{backend}");
        let codes: Vec<&str> = service.cancellation_reasons.iter().map(|reason| reason.code.as_str()).collect();
        assert_eq!(codes, ["None", "ConditionalCheckFailed"], "{backend}");

        let read = client
            .transact_get_items(TransactGetItemsInput {
                transact_items: vec![
                    TransactGet { table_name: table.clone(), key: key("unique", "a"), ..Default::default() },
                    TransactGet { table_name: table.clone(), key: key("unique", "c"), ..Default::default() },
                ],
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        assert!(read.responses[0].is_some() && read.responses[1].is_none(), "{backend}");
        client.delete_table(&table).await.result.unwrap();
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn table_settings_ttl_tags_and_partiql() {
    for (backend, client) in clients().await {
        let table = unique("settings");
        create_table(&client, &table).await;
        let ttl = client
            .update_time_to_live(UpdateTimeToLiveInput {
                table_name: table.clone(),
                enabled: true,
                attribute_name: "expires".into(),
            })
            .await
            .result
            .unwrap();
        assert_eq!(ttl.status, TimeToLiveStatus::Enabled, "{backend}");
        let described = client.describe_time_to_live(&table).await.result.unwrap();
        assert_eq!(described.attribute_name.as_deref(), Some("expires"), "{backend}");

        let arn = client.describe_table(&table).await.result.unwrap().table_arn.unwrap();
        client.tag_resource(&arn, BTreeMap::from([("owner".into(), "tests".into())])).await.result.unwrap();
        let tags = client.list_tags_of_resource(&arn).await.result.unwrap().tags;
        assert_eq!(tags.get("owner").map(String::as_str), Some("tests"), "{backend}");

        client
            .execute_statement(ExecuteStatementInput {
                statement: format!("INSERT INTO \"{table}\" VALUE {{'pk': ?, 'sk': ?, 'n': 1}}"),
                parameters: vec![s("p"), s("s")],
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        let selected = client
            .execute_statement(ExecuteStatementInput {
                statement: format!("SELECT n FROM \"{table}\" WHERE pk = ? AND sk = ?"),
                parameters: vec![s("p"), s("s")],
                ..Default::default()
            })
            .await
            .result
            .unwrap();
        assert_eq!(selected.items, [item([("n", n(1))])], "{backend}");
        client.delete_table(&table).await.result.unwrap();
        assert!(client.wait_for_table(&table, false, Duration::from_secs(30)).await.unwrap().is_none(), "{backend}");
    }
}
