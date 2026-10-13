//! `Query` and `Scan`: one page at a time, with DynamoDB's limits (`Limit` counts evaluated
//! items, a page reads at most 1 MB before filtering) and `LastEvaluatedKey` semantics (it is
//! returned whenever the page stopped early, even if no item follows).

use super::State;
use super::table::{CapacityMeter, Index, KeyInfo, StoredKey, Table, read_units};
use crate::error::{Error, Result};
use crate::expr::{
    Condition, ExpressionInput, Path, condition_paths, evaluate, key_condition, parse_request, project,
    sort_key_matches,
};
use crate::types::{ProjectionType, QueryInput, QueryOutput, ReturnConsumedCapacity, ScanInput, Select};
use crate::value::{Item, item_size};

/// Data a page may read before it stops (1 MB).
const PAGE_SIZE_LIMIT: usize = 1024 * 1024;

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

/// What a read targets: the table itself or one of its indexes.
struct Target<'a> {
    table: &'a Table,
    index: Option<&'a Index>,
}

impl<'a> Target<'a> {
    fn new(table: &'a Table, index_name: Option<&str>, consistent_read: bool) -> Result<Self> {
        let index = match index_name {
            Some(name) => Some(table.index(name)?),
            None => None,
        };
        if consistent_read && index.is_some_and(|index| index.global) {
            return Err(invalid("Consistent reads are not supported on global secondary indexes"));
        }
        Ok(Self { table, index })
    }

    fn key(&self) -> &KeyInfo {
        self.index.map_or(&self.table.key, |index| &index.key)
    }

    /// The attributes visible through the target (index projection).
    fn visible(&self, item: &Item) -> Item {
        match self.index {
            Some(index) => index.project(item, &self.table.key),
            None => item.clone(),
        }
    }

    /// `LastEvaluatedKey` for an item: table key plus index key attributes.
    fn last_key(&self, item: &Item) -> Item {
        let mut key = self.table.key.key_of(item);
        if let Some(index) = self.index {
            key.extend(index.key.key_of(item));
        }
        key
    }

    /// The position of an `ExclusiveStartKey` in iteration order.
    fn start_position(&self, start: &Item) -> Result<(StoredKey, Option<StoredKey>)> {
        let bad =
            || invalid("The provided starting key is invalid: The provided key element does not match the schema");
        let table_key = self.table.key.exact_key(&self.table.key.key_of(start)).map_err(|_| bad())?;
        match self.index {
            None => {
                if start.len() != self.table.key.names().len() {
                    return Err(bad());
                }
                Ok((table_key, None))
            }
            Some(index) => {
                let index_key = index.entry_key(start).ok_or_else(bad)?;
                Ok((index_key, Some(table_key)))
            }
        }
    }

    fn capacity_target(&self) -> Option<(&str, bool)> {
        self.index.map(|index| (index.name.as_str(), index.global))
    }
}

/// Resolves `Select` against the projection expression and the target, like DynamoDB.
fn effective_select(select: Option<Select>, projection: Option<&Vec<Path>>, target: &Target<'_>) -> Result<Select> {
    let select = match (select, projection) {
        (None, Some(_)) => Select::SpecificAttributes,
        (None, None) => {
            if target.index.is_some() {
                Select::AllProjectedAttributes
            } else {
                Select::AllAttributes
            }
        }
        (Some(Select::SpecificAttributes), None) => {
            return Err(invalid(
                "1 validation error detected: SPECIFIC_ATTRIBUTES is only valid with a ProjectionExpression or AttributesToGet",
            ));
        }
        (Some(select @ (Select::AllAttributes | Select::AllProjectedAttributes | Select::Count)), Some(_)) => {
            return Err(invalid(format!(
                "Cannot specify the ProjectionExpression when choosing to get {}",
                select.as_str()
            )));
        }
        (Some(select), _) => select,
    };
    if select == Select::AllProjectedAttributes && target.index.is_none() {
        return Err(invalid("ALL_PROJECTED_ATTRIBUTES can be used only when Querying using an IndexName"));
    }
    if select == Select::AllAttributes
        && let Some(index) = target.index
        && index.global
        && index.projection.projection_type != ProjectionType::All
    {
        return Err(invalid(format!(
            "One or more parameter values were invalid: Select type ALL_ATTRIBUTES is not supported for global secondary index {} because its projection type is not ALL",
            index.name
        )));
    }
    Ok(select)
}

struct PageRequest<'a> {
    filter: Option<&'a Condition>,
    projection: Option<&'a Vec<Path>>,
    select: Select,
    limit: Option<u32>,
    consistent: bool,
    return_consumed_capacity: ReturnConsumedCapacity,
}

/// Evaluates candidates (already positioned after the start key, in order) into one page.
fn page<'a>(target: &Target<'_>, candidates: impl Iterator<Item = &'a Item>, request: &PageRequest<'_>) -> QueryOutput {
    let mut output = QueryOutput::default();
    let mut read_bytes = 0usize;
    let mut last: Option<&Item> = None;
    let mut stopped_early = false;
    let mut candidates = candidates.peekable();
    while let Some(item) = candidates.next() {
        let visible = target.visible(item);
        read_bytes += item_size(&visible);
        output.scanned_count += 1;
        last = Some(item);
        // Local indexes fetch non-projected attributes from the table.
        let evaluated = match (request.select, target.index) {
            (Select::AllAttributes, Some(index)) if !index.global => item.clone(),
            _ => visible,
        };
        if request.filter.is_none_or(|filter| evaluate(filter, &evaluated)) {
            output.count += 1;
            if request.select != Select::Count {
                output.items.push(match request.projection {
                    Some(paths) => project(&evaluated, paths),
                    None => evaluated,
                });
            }
        }
        let limit_reached = request.limit.is_some_and(|limit| output.scanned_count >= limit);
        if limit_reached || read_bytes >= PAGE_SIZE_LIMIT {
            stopped_early = limit_reached || candidates.peek().is_some();
            break;
        }
    }
    if stopped_early && let Some(item) = last {
        output.last_evaluated_key = Some(target.last_key(item));
    }
    let mut meter = CapacityMeter::default();
    meter.read(&target.table.name, target.capacity_target(), read_units(read_bytes, request.consistent));
    output.consumed_capacity = meter.table_report(&target.table.name, request.return_consumed_capacity);
    output
}

fn check_limit(limit: Option<u32>) -> Result<()> {
    if limit == Some(0) {
        return Err(invalid(
            "1 validation error detected: Value '0' at 'limit' failed to satisfy constraint: Member must have value greater than or equal to 1",
        ));
    }
    Ok(())
}

/// `Query`.
pub fn query(state: &State, input: &QueryInput) -> Result<QueryOutput> {
    check_limit(input.limit)?;
    let Some(key_text) = input.key_condition_expression.as_deref() else {
        return Err(invalid(
            "Either the KeyConditions or KeyConditionExpression parameter must be specified in the request.",
        ));
    };
    let parsed = parse_request(
        ExpressionInput {
            key_condition: Some(key_text),
            filter: input.filter_expression.as_deref(),
            projection: input.projection_expression.as_deref(),
            ..Default::default()
        },
        &input.expression_attribute_names,
        &input.expression_attribute_values,
    )?;
    let table = state.table(&input.table_name)?;
    let target = Target::new(table, input.index_name.as_deref(), input.consistent_read)?;
    let key = target.key();
    let condition = key_condition(
        parsed.key_condition.as_ref().ok_or_else(|| invalid("missing key condition"))?,
        (&key.partition.name, key.partition.kind.value_type()),
        key.sort.as_ref().map(|sort| (sort.name.as_str(), sort.kind.value_type())),
    )?;
    if let Some(filter) = &parsed.filter {
        for path in condition_paths(filter) {
            if key.names().contains(&path.root()) {
                return Err(invalid(format!(
                    "Filter Expression can only contain non-primary key attributes: Primary key attribute: {}",
                    path.root()
                )));
            }
        }
    }
    let select = effective_select(input.select, parsed.projection.as_ref(), &target)?;
    let partition = StoredKey::new(condition.partition_value.clone(), None);
    let in_partition = |stored: &StoredKey| stored.hash == partition.hash && stored.partition == partition.partition;
    let sort_matches = |item: &Item| match (&condition.sort, &key.sort) {
        (Some((_, sort_condition)), Some(sort)) => {
            item.get(&sort.name).is_some_and(|value| sort_key_matches(value, sort_condition))
        }
        _ => true,
    };
    let start = match &input.exclusive_start_key {
        Some(start) => Some(target.start_position(start)?),
        None => None,
    };
    // Candidates in sort key order (then reversed for descending queries).
    let mut candidates: Vec<(StoredKey, Option<StoredKey>, &Item)> = match target.index {
        None => table
            .items
            .range(partition.clone()..)
            .take_while(|(stored, _)| in_partition(stored))
            .map(|(stored, item)| (stored.clone(), None, item))
            .collect(),
        Some(index) => index
            .entries
            .range((partition.clone(), StoredKey::min())..)
            .take_while(|(index_key, _)| in_partition(index_key))
            .filter_map(|(index_key, table_key)| {
                table.items.get(table_key).map(|item| (index_key.clone(), Some(table_key.clone()), item))
            })
            .collect(),
    };
    candidates.retain(|(_, _, item)| sort_matches(item));
    if !input.scan_index_forward {
        candidates.reverse();
    }
    if let Some((start_key, start_table_key)) = &start {
        let start = (start_key, start_table_key.as_ref());
        let position = candidates.iter().position(|(stored, table_key, _)| {
            let current = (stored, table_key.as_ref());
            if input.scan_index_forward { current > start } else { current < start }
        });
        candidates.drain(..position.unwrap_or(candidates.len()));
    }
    let request = PageRequest {
        filter: parsed.filter.as_ref(),
        projection: parsed.projection.as_ref(),
        select,
        limit: input.limit,
        consistent: input.consistent_read,
        return_consumed_capacity: input.return_consumed_capacity,
    };
    Ok(page(&target, candidates.into_iter().map(|(_, _, item)| item), &request))
}

/// `Scan`.
pub fn scan(state: &State, input: &ScanInput) -> Result<QueryOutput> {
    check_limit(input.limit)?;
    match (input.segment, input.total_segments) {
        (Some(_), None) => {
            return Err(invalid(
                "The TotalSegments parameter is required but was not present in the request when Segment parameter is present",
            ));
        }
        (None, Some(_)) => {
            return Err(invalid(
                "The Segment parameter is required but was not present in the request when parameter TotalSegments is present",
            ));
        }
        (Some(segment), Some(total)) => {
            if total == 0 || total > 1_000_000 {
                return Err(invalid(format!(
                    "1 validation error detected: Value '{total}' at 'totalSegments' failed to satisfy constraint: Member must have value between 1 and 1000000"
                )));
            }
            if segment >= total {
                return Err(invalid(format!(
                    "The Segment parameter is zero-based and must be less than parameter TotalSegments: Segment: {segment} is not less than TotalSegments: {total}"
                )));
            }
        }
        (None, None) => {}
    }
    let parsed = parse_request(
        ExpressionInput {
            filter: input.filter_expression.as_deref(),
            projection: input.projection_expression.as_deref(),
            ..Default::default()
        },
        &input.expression_attribute_names,
        &input.expression_attribute_values,
    )?;
    let table = state.table(&input.table_name)?;
    let target = Target::new(table, input.index_name.as_deref(), input.consistent_read)?;
    let select = effective_select(input.select, parsed.projection.as_ref(), &target)?;
    let in_segment = |stored: &StoredKey| match (input.segment, input.total_segments) {
        (Some(segment), Some(total)) => stored.segment(total) == segment,
        _ => true,
    };
    let start = match &input.exclusive_start_key {
        Some(start) => Some(target.start_position(start)?),
        None => None,
    };
    let candidates: Vec<&Item> = match target.index {
        None => table
            .items
            .iter()
            .filter(|(stored, _)| in_segment(stored))
            .filter(|(stored, _)| start.as_ref().is_none_or(|(start, _)| *stored > start))
            .map(|(_, item)| item)
            .collect(),
        Some(index) => index
            .entries
            .iter()
            .filter(|(index_key, _)| in_segment(index_key))
            .filter(|(index_key, table_key)| {
                start
                    .as_ref()
                    .is_none_or(|(start, start_table)| (index_key, Some(table_key)) > (start, start_table.as_ref()))
            })
            .filter_map(|(_, table_key)| table.items.get(table_key))
            .collect(),
    };
    let request = PageRequest {
        filter: parsed.filter.as_ref(),
        projection: parsed.projection.as_ref(),
        select,
        limit: input.limit,
        consistent: input.consistent_read,
        return_consumed_capacity: input.return_consumed_capacity,
    };
    Ok(page(&target, candidates.into_iter(), &request))
}
