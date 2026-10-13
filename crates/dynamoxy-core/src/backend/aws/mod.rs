//! The AWS adapter: DynamoDB (or a DynamoDB-compatible endpoint) through the AWS SDK for Rust.
//!
//! Credentials and region come from the standard AWS chain (environment, profiles, SSO, web
//! identity, ECS and EC2 metadata) unless the configuration sets them. The SDK's own retries are
//! disabled: [`crate::Client`] retries with its policy and reports every attempt.

mod convert;
mod errors;

use std::collections::BTreeMap;
use std::time::Duration;

use async_trait::async_trait;
use aws_config::BehaviorVersion;
use aws_config::retry::RetryConfig;
use aws_config::timeout::TimeoutConfig;
use aws_sdk_dynamodb::operation::{
    delete_item::DeleteItemError, execute_statement::ExecuteStatementError,
    execute_transaction::ExecuteTransactionError, put_item::PutItemError, transact_get_items::TransactGetItemsError,
    transact_write_items::TransactWriteItemsError, update_item::UpdateItemError,
};
use aws_sdk_dynamodb::primitives::DateTime;
use aws_sdk_dynamodb::types as sdk;

use self::convert::{build_error, protocol_error};
use self::errors::{Extra, cancellation_reasons, condition_item, map_error};
use super::Backend;
use crate::config::ClientConfig;
use crate::error::{Error, Result};
use crate::types::{
    BackupDescription, BackupDetails, BatchExecuteStatementInput, BatchExecuteStatementOutput, BatchGetItemInput,
    BatchGetItemOutput, BatchStatementError, BatchStatementResponse, BatchWriteItemInput, BatchWriteItemOutput,
    ContinuousBackupsDescription, CreateTableInput, DeleteItemInput, ExecuteStatementInput, ExecuteStatementOutput,
    ExecuteTransactionInput, ExecuteTransactionOutput, GetItemInput, GetItemOutput, GlobalSecondaryIndexUpdate,
    KeysAndAttributes, ListBackupsInput, ListBackupsOutput, ListTablesInput, ListTablesOutput, ListTagsOutput,
    ParameterizedStatement, PutItemInput, QueryInput, QueryOutput, RestoreOverrides, RestoreTableFromBackupInput,
    RestoreTableToPointInTimeInput, ScanInput, TableDescription, TimeToLiveDescription, TimeToLiveStatus,
    TransactGetItemsInput, TransactGetItemsOutput, TransactWriteItem, TransactWriteItemsInput,
    TransactWriteItemsOutput, UpdateItemInput, UpdateTableInput, UpdateTimeToLiveInput, WriteItemOutput, WriteRequest,
};

/// DynamoDB through the AWS SDK. Cheap to clone.
#[derive(Clone, Debug)]
pub struct AwsBackend {
    client: aws_sdk_dynamodb::Client,
}

fn no_extra<E>(_: &E) -> Extra {
    Extra::None
}

impl AwsBackend {
    /// Loads the AWS configuration (region, credentials) and creates the SDK client.
    ///
    /// Credentials are resolved lazily, on the first request. With an `endpoint_url` and no
    /// region anywhere, `us-east-1` is used (local emulators ignore it).
    pub async fn connect(config: &ClientConfig) -> Result<Self> {
        let mut loader = aws_config::defaults(BehaviorVersion::latest());
        if let Some(region) = &config.region {
            loader = loader.region(aws_config::Region::new(region.clone()));
        }
        if let Some(profile) = &config.profile {
            loader = loader.profile_name(profile);
        }
        if let Some(endpoint) = &config.endpoint_url {
            loader = loader.endpoint_url(endpoint);
        }
        if let (Some(key), Some(secret)) = (&config.access_key_id, &config.secret_access_key) {
            loader = loader.credentials_provider(aws_credential_types::Credentials::new(
                key,
                secret,
                config.session_token.clone(),
                None,
                "dynamoxy",
            ));
        }
        let shared = loader.load().await;
        let mut builder =
            aws_sdk_dynamodb::config::Builder::from(&shared).retry_config(RetryConfig::disabled()).timeout_config(
                TimeoutConfig::builder().connect_timeout(Duration::from_secs_f64(config.connect_timeout)).build(),
            );
        if shared.region().is_none() {
            if config.endpoint_url.is_some() {
                builder = builder.region(aws_config::Region::new("us-east-1"));
            } else {
                return Err(Error::Config(
                    "no AWS region configured: pass region=... or set AWS_REGION (or a region in your AWS profile)"
                        .into(),
                ));
            }
        }
        Ok(Self { client: aws_sdk_dynamodb::Client::from_conf(builder.build()) })
    }
}

fn keys_and_attributes_to_sdk(request: KeysAndAttributes) -> Result<sdk::KeysAndAttributes> {
    sdk::KeysAndAttributes::builder()
        .set_keys(Some(request.keys.into_iter().map(convert::item_to_sdk).collect()))
        .consistent_read(request.consistent_read)
        .set_projection_expression(request.projection_expression)
        .set_expression_attribute_names(convert::names(request.expression_attribute_names))
        .build()
        .map_err(build_error)
}

fn keys_and_attributes_from_sdk(request: sdk::KeysAndAttributes) -> Result<KeysAndAttributes> {
    Ok(KeysAndAttributes {
        keys: request.keys.into_iter().map(convert::item_from_sdk).collect::<Result<_>>()?,
        consistent_read: request.consistent_read.unwrap_or(false),
        projection_expression: request.projection_expression,
        expression_attribute_names: request.expression_attribute_names.unwrap_or_default().into_iter().collect(),
    })
}

fn write_request_to_sdk(request: WriteRequest) -> Result<sdk::WriteRequest> {
    Ok(match request {
        WriteRequest::Put(item) => sdk::WriteRequest::builder()
            .put_request(
                sdk::PutRequest::builder().set_item(Some(convert::item_to_sdk(item))).build().map_err(build_error)?,
            )
            .build(),
        WriteRequest::Delete(key) => sdk::WriteRequest::builder()
            .delete_request(
                sdk::DeleteRequest::builder().set_key(Some(convert::item_to_sdk(key))).build().map_err(build_error)?,
            )
            .build(),
    })
}

fn write_request_from_sdk(request: sdk::WriteRequest) -> Result<WriteRequest> {
    if let Some(put) = request.put_request {
        return Ok(WriteRequest::Put(convert::item_from_sdk(put.item)?));
    }
    if let Some(delete) = request.delete_request {
        return Ok(WriteRequest::Delete(convert::item_from_sdk(delete.key)?));
    }
    Err(protocol_error("write request without put or delete"))
}

fn transact_write_to_sdk(item: TransactWriteItem) -> Result<sdk::TransactWriteItem> {
    let builder = sdk::TransactWriteItem::builder();
    Ok(match item {
        TransactWriteItem::ConditionCheck { target, key } => builder
            .condition_check(
                sdk::ConditionCheck::builder()
                    .table_name(target.table_name)
                    .set_key(Some(convert::item_to_sdk(key)))
                    .set_condition_expression(target.condition_expression)
                    .set_expression_attribute_names(convert::names(target.expression_attribute_names))
                    .set_expression_attribute_values(convert::values(target.expression_attribute_values))
                    .set_return_values_on_condition_check_failure(convert::return_on_failure(
                        target.return_values_on_condition_check_failure,
                    ))
                    .build()
                    .map_err(build_error)?,
            )
            .build(),
        TransactWriteItem::Put { target, item } => builder
            .put(
                sdk::Put::builder()
                    .table_name(target.table_name)
                    .set_item(Some(convert::item_to_sdk(item)))
                    .set_condition_expression(target.condition_expression)
                    .set_expression_attribute_names(convert::names(target.expression_attribute_names))
                    .set_expression_attribute_values(convert::values(target.expression_attribute_values))
                    .set_return_values_on_condition_check_failure(convert::return_on_failure(
                        target.return_values_on_condition_check_failure,
                    ))
                    .build()
                    .map_err(build_error)?,
            )
            .build(),
        TransactWriteItem::Delete { target, key } => builder
            .delete(
                sdk::Delete::builder()
                    .table_name(target.table_name)
                    .set_key(Some(convert::item_to_sdk(key)))
                    .set_condition_expression(target.condition_expression)
                    .set_expression_attribute_names(convert::names(target.expression_attribute_names))
                    .set_expression_attribute_values(convert::values(target.expression_attribute_values))
                    .set_return_values_on_condition_check_failure(convert::return_on_failure(
                        target.return_values_on_condition_check_failure,
                    ))
                    .build()
                    .map_err(build_error)?,
            )
            .build(),
        TransactWriteItem::Update { target, key, update_expression } => builder
            .update(
                sdk::Update::builder()
                    .table_name(target.table_name)
                    .set_key(Some(convert::item_to_sdk(key)))
                    .update_expression(update_expression)
                    .set_condition_expression(target.condition_expression)
                    .set_expression_attribute_names(convert::names(target.expression_attribute_names))
                    .set_expression_attribute_values(convert::values(target.expression_attribute_values))
                    .set_return_values_on_condition_check_failure(convert::return_on_failure(
                        target.return_values_on_condition_check_failure,
                    ))
                    .build()
                    .map_err(build_error)?,
            )
            .build(),
    })
}

fn statement_to_sdk(statement: ParameterizedStatement) -> Result<sdk::ParameterizedStatement> {
    sdk::ParameterizedStatement::builder()
        .statement(statement.statement)
        .set_parameters(
            (!statement.parameters.is_empty())
                .then(|| statement.parameters.into_iter().map(convert::value_to_sdk).collect()),
        )
        .set_return_values_on_condition_check_failure(convert::return_on_failure(
            statement.return_values_on_condition_check_failure,
        ))
        .build()
        .map_err(build_error)
}

fn restore_settings(
    overrides: &RestoreOverrides,
) -> Result<(Option<sdk::ProvisionedThroughput>, Option<sdk::SseSpecification>)> {
    Ok((
        overrides.provisioned_throughput.as_ref().map(convert::throughput_to_sdk).transpose()?,
        overrides.sse_specification.as_ref().map(convert::sse_to_sdk),
    ))
}

fn ttl_description(status: Option<&sdk::TimeToLiveStatus>, attribute: Option<&str>) -> TimeToLiveDescription {
    TimeToLiveDescription {
        status: status
            .and_then(|status| TimeToLiveStatus::parse(status.as_str()))
            .unwrap_or(TimeToLiveStatus::Disabled),
        attribute_name: attribute.map(str::to_owned),
    }
}

#[async_trait]
impl Backend for AwsBackend {
    fn name(&self) -> &'static str {
        "aws"
    }

    async fn get_item(&self, input: GetItemInput) -> Result<GetItemOutput> {
        let output = self
            .client
            .get_item()
            .table_name(input.table_name)
            .set_key(Some(convert::item_to_sdk(input.key)))
            .consistent_read(input.consistent_read)
            .set_projection_expression(input.projection_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| map_error("GetItem", error, no_extra))?;
        Ok(GetItemOutput {
            item: convert::optional_item(output.item)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
        })
    }

    async fn put_item(&self, input: PutItemInput) -> Result<WriteItemOutput> {
        let output = self
            .client
            .put_item()
            .table_name(input.table_name)
            .set_item(Some(convert::item_to_sdk(input.item)))
            .set_condition_expression(input.condition_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_expression_attribute_values(convert::values(input.expression_attribute_values))
            .set_return_values(convert::return_values(input.return_values))
            .set_return_values_on_condition_check_failure(convert::return_on_failure(
                input.return_values_on_condition_check_failure,
            ))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_item_collection_metrics(convert::return_item_collection_metrics(
                input.return_item_collection_metrics,
            ))
            .send()
            .await
            .map_err(|error| {
                map_error("PutItem", error, |error| match error {
                    PutItemError::ConditionalCheckFailedException(failure) => condition_item(failure),
                    _ => Extra::None,
                })
            })?;
        Ok(WriteItemOutput {
            attributes: convert::optional_item(output.attributes)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
            item_collection_metrics: output
                .item_collection_metrics
                .map(convert::item_collection_metrics)
                .transpose()?,
        })
    }

    async fn update_item(&self, input: UpdateItemInput) -> Result<WriteItemOutput> {
        let output = self
            .client
            .update_item()
            .table_name(input.table_name)
            .set_key(Some(convert::item_to_sdk(input.key)))
            .set_update_expression(input.update_expression)
            .set_condition_expression(input.condition_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_expression_attribute_values(convert::values(input.expression_attribute_values))
            .set_return_values(convert::return_values(input.return_values))
            .set_return_values_on_condition_check_failure(convert::return_on_failure(
                input.return_values_on_condition_check_failure,
            ))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_item_collection_metrics(convert::return_item_collection_metrics(
                input.return_item_collection_metrics,
            ))
            .send()
            .await
            .map_err(|error| {
                map_error("UpdateItem", error, |error| match error {
                    UpdateItemError::ConditionalCheckFailedException(failure) => condition_item(failure),
                    _ => Extra::None,
                })
            })?;
        Ok(WriteItemOutput {
            attributes: convert::optional_item(output.attributes)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
            item_collection_metrics: output
                .item_collection_metrics
                .map(convert::item_collection_metrics)
                .transpose()?,
        })
    }

    async fn delete_item(&self, input: DeleteItemInput) -> Result<WriteItemOutput> {
        let output = self
            .client
            .delete_item()
            .table_name(input.table_name)
            .set_key(Some(convert::item_to_sdk(input.key)))
            .set_condition_expression(input.condition_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_expression_attribute_values(convert::values(input.expression_attribute_values))
            .set_return_values(convert::return_values(input.return_values))
            .set_return_values_on_condition_check_failure(convert::return_on_failure(
                input.return_values_on_condition_check_failure,
            ))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_item_collection_metrics(convert::return_item_collection_metrics(
                input.return_item_collection_metrics,
            ))
            .send()
            .await
            .map_err(|error| {
                map_error("DeleteItem", error, |error| match error {
                    DeleteItemError::ConditionalCheckFailedException(failure) => condition_item(failure),
                    _ => Extra::None,
                })
            })?;
        Ok(WriteItemOutput {
            attributes: convert::optional_item(output.attributes)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
            item_collection_metrics: output
                .item_collection_metrics
                .map(convert::item_collection_metrics)
                .transpose()?,
        })
    }

    async fn query(&self, input: QueryInput) -> Result<QueryOutput> {
        let output = self
            .client
            .query()
            .table_name(input.table_name)
            .set_index_name(input.index_name)
            .set_key_condition_expression(input.key_condition_expression)
            .set_filter_expression(input.filter_expression)
            .set_projection_expression(input.projection_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_expression_attribute_values(convert::values(input.expression_attribute_values))
            .set_select(convert::select(input.select))
            .set_limit(input.limit.map(|limit| limit.min(i32::MAX as u32) as i32))
            .consistent_read(input.consistent_read)
            .scan_index_forward(input.scan_index_forward)
            .set_exclusive_start_key(input.exclusive_start_key.map(convert::item_to_sdk))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| map_error("Query", error, no_extra))?;
        Ok(QueryOutput {
            items: output.items.unwrap_or_default().into_iter().map(convert::item_from_sdk).collect::<Result<_>>()?,
            count: output.count.max(0) as u32,
            scanned_count: output.scanned_count.max(0) as u32,
            last_evaluated_key: convert::optional_item(output.last_evaluated_key)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
        })
    }

    async fn scan(&self, input: ScanInput) -> Result<QueryOutput> {
        let output = self
            .client
            .scan()
            .table_name(input.table_name)
            .set_index_name(input.index_name)
            .set_filter_expression(input.filter_expression)
            .set_projection_expression(input.projection_expression)
            .set_expression_attribute_names(convert::names(input.expression_attribute_names))
            .set_expression_attribute_values(convert::values(input.expression_attribute_values))
            .set_select(convert::select(input.select))
            .set_limit(input.limit.map(|limit| limit.min(i32::MAX as u32) as i32))
            .consistent_read(input.consistent_read)
            .set_exclusive_start_key(input.exclusive_start_key.map(convert::item_to_sdk))
            .set_segment(input.segment.map(|segment| segment as i32))
            .set_total_segments(input.total_segments.map(|total| total as i32))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| map_error("Scan", error, no_extra))?;
        Ok(QueryOutput {
            items: output.items.unwrap_or_default().into_iter().map(convert::item_from_sdk).collect::<Result<_>>()?,
            count: output.count.max(0) as u32,
            scanned_count: output.scanned_count.max(0) as u32,
            last_evaluated_key: convert::optional_item(output.last_evaluated_key)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
        })
    }

    async fn batch_get_item(&self, input: BatchGetItemInput) -> Result<BatchGetItemOutput> {
        let request_items = input
            .request_items
            .into_iter()
            .map(|(table, request)| Ok((table, keys_and_attributes_to_sdk(request)?)))
            .collect::<Result<_>>()?;
        let output = self
            .client
            .batch_get_item()
            .set_request_items(Some(request_items))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| map_error("BatchGetItem", error, no_extra))?;
        Ok(BatchGetItemOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            responses: output
                .responses
                .unwrap_or_default()
                .into_iter()
                .map(|(table, items)| {
                    Ok((table, items.into_iter().map(convert::item_from_sdk).collect::<Result<_>>()?))
                })
                .collect::<Result<_>>()?,
            unprocessed_keys: output
                .unprocessed_keys
                .unwrap_or_default()
                .into_iter()
                .map(|(table, request)| Ok((table, keys_and_attributes_from_sdk(request)?)))
                .collect::<Result<_>>()?,
        })
    }

    async fn batch_write_item(&self, input: BatchWriteItemInput) -> Result<BatchWriteItemOutput> {
        let request_items = input
            .request_items
            .into_iter()
            .map(|(table, requests)| {
                Ok((table, requests.into_iter().map(write_request_to_sdk).collect::<Result<_>>()?))
            })
            .collect::<Result<_>>()?;
        let output = self
            .client
            .batch_write_item()
            .set_request_items(Some(request_items))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_item_collection_metrics(convert::return_item_collection_metrics(
                input.return_item_collection_metrics,
            ))
            .send()
            .await
            .map_err(|error| map_error("BatchWriteItem", error, no_extra))?;
        Ok(BatchWriteItemOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            item_collection_metrics: convert::item_collection_metrics_map(output.item_collection_metrics)?,
            unprocessed_items: output
                .unprocessed_items
                .unwrap_or_default()
                .into_iter()
                .map(|(table, requests)| {
                    Ok((table, requests.into_iter().map(write_request_from_sdk).collect::<Result<_>>()?))
                })
                .collect::<Result<_>>()?,
        })
    }

    async fn transact_get_items(&self, input: TransactGetItemsInput) -> Result<TransactGetItemsOutput> {
        let items = input
            .transact_items
            .into_iter()
            .map(|get| {
                Ok(sdk::TransactGetItem::builder()
                    .get(
                        sdk::Get::builder()
                            .table_name(get.table_name)
                            .set_key(Some(convert::item_to_sdk(get.key)))
                            .set_projection_expression(get.projection_expression)
                            .set_expression_attribute_names(convert::names(get.expression_attribute_names))
                            .build()
                            .map_err(build_error)?,
                    )
                    .build())
            })
            .collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .transact_get_items()
            .set_transact_items(Some(items))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| {
                map_error("TransactGetItems", error, |error| match error {
                    TransactGetItemsError::TransactionCanceledException(canceled) => cancellation_reasons(canceled),
                    _ => Extra::None,
                })
            })?;
        Ok(TransactGetItemsOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            responses: output
                .responses
                .unwrap_or_default()
                .into_iter()
                .map(|response| convert::optional_item(response.item))
                .collect::<Result<_>>()?,
        })
    }

    async fn transact_write_items(&self, input: TransactWriteItemsInput) -> Result<TransactWriteItemsOutput> {
        let items = input.transact_items.into_iter().map(transact_write_to_sdk).collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .transact_write_items()
            .set_transact_items(Some(items))
            .set_client_request_token(input.client_request_token)
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_item_collection_metrics(convert::return_item_collection_metrics(
                input.return_item_collection_metrics,
            ))
            .send()
            .await
            .map_err(|error| {
                map_error("TransactWriteItems", error, |error| match error {
                    TransactWriteItemsError::TransactionCanceledException(canceled) => cancellation_reasons(canceled),
                    _ => Extra::None,
                })
            })?;
        Ok(TransactWriteItemsOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            item_collection_metrics: convert::item_collection_metrics_map(output.item_collection_metrics)?,
        })
    }

    async fn execute_statement(&self, input: ExecuteStatementInput) -> Result<ExecuteStatementOutput> {
        let output = self
            .client
            .execute_statement()
            .statement(input.statement)
            .set_parameters(
                (!input.parameters.is_empty())
                    .then(|| input.parameters.into_iter().map(convert::value_to_sdk).collect()),
            )
            .consistent_read(input.consistent_read)
            .set_next_token(input.next_token)
            .set_limit(input.limit.map(|limit| limit.min(i32::MAX as u32) as i32))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .set_return_values_on_condition_check_failure(convert::return_on_failure(
                input.return_values_on_condition_check_failure,
            ))
            .send()
            .await
            .map_err(|error| {
                map_error("ExecuteStatement", error, |error| match error {
                    ExecuteStatementError::ConditionalCheckFailedException(failure) => condition_item(failure),
                    _ => Extra::None,
                })
            })?;
        Ok(ExecuteStatementOutput {
            items: output.items.unwrap_or_default().into_iter().map(convert::item_from_sdk).collect::<Result<_>>()?,
            next_token: output.next_token,
            last_evaluated_key: convert::optional_item(output.last_evaluated_key)?,
            consumed_capacity: output.consumed_capacity.as_ref().map(convert::consumed_capacity),
        })
    }

    async fn batch_execute_statement(&self, input: BatchExecuteStatementInput) -> Result<BatchExecuteStatementOutput> {
        let statements = input
            .statements
            .into_iter()
            .map(|statement| {
                sdk::BatchStatementRequest::builder()
                    .statement(statement.statement)
                    .set_parameters(
                        (!statement.parameters.is_empty())
                            .then(|| statement.parameters.into_iter().map(convert::value_to_sdk).collect()),
                    )
                    .consistent_read(statement.consistent_read)
                    .set_return_values_on_condition_check_failure(convert::return_on_failure(
                        statement.return_values_on_condition_check_failure,
                    ))
                    .build()
                    .map_err(build_error)
            })
            .collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .batch_execute_statement()
            .set_statements(Some(statements))
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| map_error("BatchExecuteStatement", error, no_extra))?;
        Ok(BatchExecuteStatementOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            responses: output
                .responses
                .unwrap_or_default()
                .into_iter()
                .map(|response| {
                    Ok(BatchStatementResponse {
                        error: response
                            .error
                            .map(|error| {
                                Ok::<_, Error>(BatchStatementError {
                                    code: error.code.map(|code| code.as_str().to_owned()).unwrap_or_default(),
                                    message: error.message,
                                    item: convert::optional_item(error.item)?,
                                })
                            })
                            .transpose()?,
                        table_name: response.table_name,
                        item: convert::optional_item(response.item)?,
                    })
                })
                .collect::<Result<_>>()?,
        })
    }

    async fn execute_transaction(&self, input: ExecuteTransactionInput) -> Result<ExecuteTransactionOutput> {
        let statements = input.transact_statements.into_iter().map(statement_to_sdk).collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .execute_transaction()
            .set_transact_statements(Some(statements))
            .set_client_request_token(input.client_request_token)
            .set_return_consumed_capacity(convert::return_consumed_capacity(input.return_consumed_capacity))
            .send()
            .await
            .map_err(|error| {
                map_error("ExecuteTransaction", error, |error| match error {
                    ExecuteTransactionError::TransactionCanceledException(canceled) => cancellation_reasons(canceled),
                    _ => Extra::None,
                })
            })?;
        Ok(ExecuteTransactionOutput {
            consumed_capacity: convert::consumed_capacities(output.consumed_capacity.as_deref()),
            responses: output
                .responses
                .unwrap_or_default()
                .into_iter()
                .map(|response| convert::optional_item(response.item))
                .collect::<Result<_>>()?,
        })
    }

    async fn create_table(&self, input: CreateTableInput) -> Result<TableDescription> {
        let globals = input
            .global_secondary_indexes
            .iter()
            .map(|index| {
                sdk::GlobalSecondaryIndex::builder()
                    .index_name(&index.index_name)
                    .set_key_schema(Some(convert::key_schema_to_sdk(&index.key_schema)?))
                    .projection(convert::projection_to_sdk(&index.projection))
                    .set_provisioned_throughput(
                        index.provisioned_throughput.as_ref().map(convert::throughput_to_sdk).transpose()?,
                    )
                    .set_on_demand_throughput(index.on_demand_throughput.as_ref().map(convert::on_demand_to_sdk))
                    .build()
                    .map_err(build_error)
            })
            .collect::<Result<Vec<_>>>()?;
        let locals = input
            .local_secondary_indexes
            .iter()
            .map(|index| {
                sdk::LocalSecondaryIndex::builder()
                    .index_name(&index.index_name)
                    .set_key_schema(Some(convert::key_schema_to_sdk(&index.key_schema)?))
                    .projection(convert::projection_to_sdk(&index.projection))
                    .build()
                    .map_err(build_error)
            })
            .collect::<Result<Vec<_>>>()?;
        let tags = input
            .tags
            .iter()
            .map(|(key, value)| sdk::Tag::builder().key(key).value(value).build().map_err(build_error))
            .collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .create_table()
            .table_name(input.table_name)
            .set_attribute_definitions(Some(convert::attribute_definitions_to_sdk(&input.attribute_definitions)?))
            .set_key_schema(Some(convert::key_schema_to_sdk(&input.key_schema)?))
            .set_global_secondary_indexes((!globals.is_empty()).then_some(globals))
            .set_local_secondary_indexes((!locals.is_empty()).then_some(locals))
            .billing_mode(sdk::BillingMode::from(input.billing_mode.as_str()))
            .set_provisioned_throughput(
                input.provisioned_throughput.as_ref().map(convert::throughput_to_sdk).transpose()?,
            )
            .set_on_demand_throughput(input.on_demand_throughput.as_ref().map(convert::on_demand_to_sdk))
            .set_stream_specification(input.stream_specification.as_ref().map(convert::stream_to_sdk).transpose()?)
            .set_sse_specification(input.sse_specification.as_ref().map(convert::sse_to_sdk))
            .set_tags((!tags.is_empty()).then_some(tags))
            .set_table_class(input.table_class.map(|class| sdk::TableClass::from(class.as_str())))
            .deletion_protection_enabled(input.deletion_protection_enabled)
            .send()
            .await
            .map_err(|error| map_error("CreateTable", error, no_extra))?;
        output
            .table_description
            .map(convert::table_description)
            .ok_or_else(|| protocol_error("CreateTable without a description"))
    }

    async fn delete_table(&self, table_name: &str) -> Result<TableDescription> {
        let output = self
            .client
            .delete_table()
            .table_name(table_name)
            .send()
            .await
            .map_err(|error| map_error("DeleteTable", error, no_extra))?;
        output
            .table_description
            .map(convert::table_description)
            .ok_or_else(|| protocol_error("DeleteTable without a description"))
    }

    async fn describe_table(&self, table_name: &str) -> Result<TableDescription> {
        let output = self
            .client
            .describe_table()
            .table_name(table_name)
            .send()
            .await
            .map_err(|error| map_error("DescribeTable", error, no_extra))?;
        output.table.map(convert::table_description).ok_or_else(|| protocol_error("DescribeTable without a table"))
    }

    async fn update_table(&self, input: UpdateTableInput) -> Result<TableDescription> {
        let updates = input
            .global_secondary_index_updates
            .iter()
            .map(|update| {
                let builder = sdk::GlobalSecondaryIndexUpdate::builder();
                Ok(match update {
                    GlobalSecondaryIndexUpdate::Create(index) => builder
                        .create(
                            sdk::CreateGlobalSecondaryIndexAction::builder()
                                .index_name(&index.index_name)
                                .set_key_schema(Some(convert::key_schema_to_sdk(&index.key_schema)?))
                                .projection(convert::projection_to_sdk(&index.projection))
                                .set_provisioned_throughput(
                                    index
                                        .provisioned_throughput
                                        .as_ref()
                                        .map(convert::throughput_to_sdk)
                                        .transpose()?,
                                )
                                .set_on_demand_throughput(
                                    index.on_demand_throughput.as_ref().map(convert::on_demand_to_sdk),
                                )
                                .build()
                                .map_err(build_error)?,
                        )
                        .build(),
                    GlobalSecondaryIndexUpdate::Update { index_name, provisioned_throughput, on_demand_throughput } => {
                        builder
                            .update(
                                sdk::UpdateGlobalSecondaryIndexAction::builder()
                                    .index_name(index_name)
                                    .set_provisioned_throughput(
                                        provisioned_throughput.as_ref().map(convert::throughput_to_sdk).transpose()?,
                                    )
                                    .set_on_demand_throughput(
                                        on_demand_throughput.as_ref().map(convert::on_demand_to_sdk),
                                    )
                                    .build()
                                    .map_err(build_error)?,
                            )
                            .build()
                    }
                    GlobalSecondaryIndexUpdate::Delete { index_name } => builder
                        .delete(
                            sdk::DeleteGlobalSecondaryIndexAction::builder()
                                .index_name(index_name)
                                .build()
                                .map_err(build_error)?,
                        )
                        .build(),
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let output = self
            .client
            .update_table()
            .table_name(input.table_name)
            .set_attribute_definitions(
                (!input.attribute_definitions.is_empty())
                    .then(|| convert::attribute_definitions_to_sdk(&input.attribute_definitions))
                    .transpose()?,
            )
            .set_billing_mode(input.billing_mode.map(|mode| sdk::BillingMode::from(mode.as_str())))
            .set_provisioned_throughput(
                input.provisioned_throughput.as_ref().map(convert::throughput_to_sdk).transpose()?,
            )
            .set_on_demand_throughput(input.on_demand_throughput.as_ref().map(convert::on_demand_to_sdk))
            .set_global_secondary_index_updates((!updates.is_empty()).then_some(updates))
            .set_stream_specification(input.stream_specification.as_ref().map(convert::stream_to_sdk).transpose()?)
            .set_sse_specification(input.sse_specification.as_ref().map(convert::sse_to_sdk))
            .set_table_class(input.table_class.map(|class| sdk::TableClass::from(class.as_str())))
            .set_deletion_protection_enabled(input.deletion_protection_enabled)
            .send()
            .await
            .map_err(|error| map_error("UpdateTable", error, no_extra))?;
        output
            .table_description
            .map(convert::table_description)
            .ok_or_else(|| protocol_error("UpdateTable without a description"))
    }

    async fn list_tables(&self, input: ListTablesInput) -> Result<ListTablesOutput> {
        let output = self
            .client
            .list_tables()
            .set_exclusive_start_table_name(input.exclusive_start_table_name)
            .set_limit(input.limit.map(|limit| limit as i32))
            .send()
            .await
            .map_err(|error| map_error("ListTables", error, no_extra))?;
        Ok(ListTablesOutput {
            table_names: output.table_names.unwrap_or_default(),
            last_evaluated_table_name: output.last_evaluated_table_name,
        })
    }

    async fn update_time_to_live(&self, input: UpdateTimeToLiveInput) -> Result<TimeToLiveDescription> {
        let specification = sdk::TimeToLiveSpecification::builder()
            .enabled(input.enabled)
            .attribute_name(&input.attribute_name)
            .build()
            .map_err(build_error)?;
        let output = self
            .client
            .update_time_to_live()
            .table_name(input.table_name)
            .time_to_live_specification(specification)
            .send()
            .await
            .map_err(|error| map_error("UpdateTimeToLive", error, no_extra))?;
        let enabled =
            output.time_to_live_specification.as_ref().map_or(input.enabled, |specification| specification.enabled());
        Ok(TimeToLiveDescription {
            status: if enabled { TimeToLiveStatus::Enabled } else { TimeToLiveStatus::Disabled },
            attribute_name: Some(input.attribute_name),
        })
    }

    async fn describe_time_to_live(&self, table_name: &str) -> Result<TimeToLiveDescription> {
        let output = self
            .client
            .describe_time_to_live()
            .table_name(table_name)
            .send()
            .await
            .map_err(|error| map_error("DescribeTimeToLive", error, no_extra))?;
        let description = output.time_to_live_description;
        Ok(ttl_description(
            description.as_ref().and_then(|description| description.time_to_live_status()),
            description.as_ref().and_then(|description| description.attribute_name()),
        ))
    }

    async fn tag_resource(&self, resource_arn: &str, tags: BTreeMap<String, String>) -> Result<()> {
        let tags = tags
            .into_iter()
            .map(|(key, value)| sdk::Tag::builder().key(key).value(value).build().map_err(build_error))
            .collect::<Result<Vec<_>>>()?;
        self.client
            .tag_resource()
            .resource_arn(resource_arn)
            .set_tags(Some(tags))
            .send()
            .await
            .map_err(|error| map_error("TagResource", error, no_extra))?;
        Ok(())
    }

    async fn untag_resource(&self, resource_arn: &str, tag_keys: Vec<String>) -> Result<()> {
        self.client
            .untag_resource()
            .resource_arn(resource_arn)
            .set_tag_keys(Some(tag_keys))
            .send()
            .await
            .map_err(|error| map_error("UntagResource", error, no_extra))?;
        Ok(())
    }

    async fn list_tags_of_resource(&self, resource_arn: &str, next_token: Option<String>) -> Result<ListTagsOutput> {
        let output = self
            .client
            .list_tags_of_resource()
            .resource_arn(resource_arn)
            .set_next_token(next_token)
            .send()
            .await
            .map_err(|error| map_error("ListTagsOfResource", error, no_extra))?;
        Ok(ListTagsOutput {
            tags: output.tags.unwrap_or_default().into_iter().map(|tag| (tag.key, tag.value)).collect(),
            next_token: output.next_token,
        })
    }

    async fn describe_continuous_backups(&self, table_name: &str) -> Result<ContinuousBackupsDescription> {
        let output = self
            .client
            .describe_continuous_backups()
            .table_name(table_name)
            .send()
            .await
            .map_err(|error| map_error("DescribeContinuousBackups", error, no_extra))?;
        output
            .continuous_backups_description
            .as_ref()
            .map(convert::continuous_backups)
            .ok_or_else(|| protocol_error("DescribeContinuousBackups without a description"))
    }

    async fn update_continuous_backups(
        &self,
        table_name: &str,
        enabled: bool,
        recovery_period_in_days: Option<i64>,
    ) -> Result<ContinuousBackupsDescription> {
        let specification = sdk::PointInTimeRecoverySpecification::builder()
            .point_in_time_recovery_enabled(enabled)
            .set_recovery_period_in_days(recovery_period_in_days.map(|days| days as i32))
            .build()
            .map_err(build_error)?;
        let output = self
            .client
            .update_continuous_backups()
            .table_name(table_name)
            .point_in_time_recovery_specification(specification)
            .send()
            .await
            .map_err(|error| map_error("UpdateContinuousBackups", error, no_extra))?;
        output
            .continuous_backups_description
            .as_ref()
            .map(convert::continuous_backups)
            .ok_or_else(|| protocol_error("UpdateContinuousBackups without a description"))
    }

    async fn create_backup(&self, table_name: &str, backup_name: &str) -> Result<BackupDetails> {
        let output = self
            .client
            .create_backup()
            .table_name(table_name)
            .backup_name(backup_name)
            .send()
            .await
            .map_err(|error| map_error("CreateBackup", error, no_extra))?;
        output
            .backup_details
            .as_ref()
            .map(convert::backup_details)
            .ok_or_else(|| protocol_error("CreateBackup without details"))
    }

    async fn describe_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        let output = self
            .client
            .describe_backup()
            .backup_arn(backup_arn)
            .send()
            .await
            .map_err(|error| map_error("DescribeBackup", error, no_extra))?;
        convert::backup_description(
            output.backup_description.as_ref().ok_or_else(|| protocol_error("DescribeBackup without a description"))?,
        )
    }

    async fn delete_backup(&self, backup_arn: &str) -> Result<BackupDescription> {
        let output = self
            .client
            .delete_backup()
            .backup_arn(backup_arn)
            .send()
            .await
            .map_err(|error| map_error("DeleteBackup", error, no_extra))?;
        convert::backup_description(
            output.backup_description.as_ref().ok_or_else(|| protocol_error("DeleteBackup without a description"))?,
        )
    }

    async fn list_backups(&self, input: ListBackupsInput) -> Result<ListBackupsOutput> {
        let output = self
            .client
            .list_backups()
            .set_table_name(input.table_name)
            .set_limit(input.limit.map(|limit| limit as i32))
            .set_exclusive_start_backup_arn(input.exclusive_start_backup_arn)
            .set_backup_type(input.backup_type.map(|kind| sdk::BackupTypeFilter::from(kind.as_str())))
            .set_time_range_lower_bound(input.time_range_lower_bound.map(DateTime::from_secs_f64))
            .set_time_range_upper_bound(input.time_range_upper_bound.map(DateTime::from_secs_f64))
            .send()
            .await
            .map_err(|error| map_error("ListBackups", error, no_extra))?;
        Ok(ListBackupsOutput {
            backups: output.backup_summaries.unwrap_or_default().iter().map(convert::backup_summary).collect(),
            last_evaluated_backup_arn: output.last_evaluated_backup_arn,
        })
    }

    async fn restore_table_from_backup(&self, input: RestoreTableFromBackupInput) -> Result<TableDescription> {
        let (throughput, sse) = restore_settings(&input.overrides)?;
        let output = self
            .client
            .restore_table_from_backup()
            .target_table_name(input.target_table_name)
            .backup_arn(input.backup_arn)
            .set_billing_mode_override(input.overrides.billing_mode.map(|mode| sdk::BillingMode::from(mode.as_str())))
            .set_provisioned_throughput_override(throughput)
            .set_sse_specification_override(sse)
            .send()
            .await
            .map_err(|error| map_error("RestoreTableFromBackup", error, no_extra))?;
        output
            .table_description
            .map(convert::table_description)
            .ok_or_else(|| protocol_error("RestoreTableFromBackup without a description"))
    }

    async fn restore_table_to_point_in_time(&self, input: RestoreTableToPointInTimeInput) -> Result<TableDescription> {
        let (throughput, sse) = restore_settings(&input.overrides)?;
        let output = self
            .client
            .restore_table_to_point_in_time()
            .source_table_name(input.source_table_name)
            .target_table_name(input.target_table_name)
            .use_latest_restorable_time(input.use_latest_restorable_time)
            .set_restore_date_time(input.restore_date_time.map(DateTime::from_secs_f64))
            .set_billing_mode_override(input.overrides.billing_mode.map(|mode| sdk::BillingMode::from(mode.as_str())))
            .set_provisioned_throughput_override(throughput)
            .set_sse_specification_override(sse)
            .send()
            .await
            .map_err(|error| map_error("RestoreTableToPointInTime", error, no_extra))?;
        output
            .table_description
            .map(convert::table_description)
            .ok_or_else(|| protocol_error("RestoreTableToPointInTime without a description"))
    }
}
