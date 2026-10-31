//! `Query` and `Scan`: one page at a time, with DynamoDB's limits (`Limit` counts evaluated
//! items, a page reads at most 1 MB before filtering) and `LastEvaluatedKey` semantics (it is
//! returned whenever the page stopped early, even if no item follows).

use std::borrow::Cow;
use std::ops::Bound;

use super::State;
use super::table::{CapacityMeter, Index, KeyInfo, PageRead, StoredKey, Table};
use crate::error::{Error, Result};
use crate::expr::{
    Comparator, Condition, ExpressionInput, Path, Projector, SortKeyCondition, condition_paths, evaluate,
    key_condition, parse_request, sort_key_matches,
};
use crate::types::{ProjectionType, QueryInput, QueryOutput, ReturnConsumedCapacity, ScalarType, ScanInput, Select};
use crate::value::{AttributeValue, Item};

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
    fn visible<'i>(&self, item: &'i Item) -> Cow<'i, Item> {
        match self.index {
            Some(index) => index.projected(item, &self.table.key),
            None => Cow::Borrowed(item),
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
    ///
    /// The start key holds the table's key attributes and, for an index, the index's, and
    /// nothing else, each of its type; DynamoDB answers anything else with "The provided starting
    /// key is invalid" (an index key of another type in a `Scan` names the index). Table key
    /// values may not be empty. Captured against DynamoDB by dynoxide-rs
    /// (`tests/query_start_key_and_key_conditions.rs`).
    fn start_position(&self, start: &Item, scan: bool) -> Result<(StoredKey, Option<StoredKey>)> {
        let bad = || invalid("The provided starting key is invalid");
        let mut names = self.table.key.names();
        if let Some(index) = self.index {
            for name in index.key.names() {
                if !names.contains(&name) {
                    names.push(name);
                }
            }
        }
        if start.len() != names.len() || names.iter().any(|name| !start.contains_key(*name)) {
            return Err(bad());
        }
        if let Some(index) = self.index {
            for attribute in index.key.attributes() {
                let value = &start[&attribute.name];
                if value.value_type() != attribute.kind.value_type() {
                    if scan {
                        return Err(invalid(format!(
                            "One or more parameter values were invalid: Type mismatch for Index Key {} Expected: {} \
                             Actual: {} IndexName: {}",
                            attribute.name,
                            attribute.kind.as_str(),
                            value.value_type().as_str(),
                            index.name
                        )));
                    }
                    return Err(bad());
                }
            }
        }
        for attribute in self.table.key.attributes() {
            let kind = match &start[&attribute.name] {
                AttributeValue::S(text) if text.is_empty() && attribute.kind == ScalarType::S => "string",
                AttributeValue::B(bytes) if bytes.is_empty() && attribute.kind == ScalarType::B => "binary",
                _ => continue,
            };
            return Err(invalid(format!(
                "One or more parameter values are not valid. The AttributeValue for a key attribute cannot contain \
                 an empty {kind} value. Key: {}",
                attribute.name
            )));
        }
        let table_key = self.table.key.exact_key(&self.table.key.key_of(start)).map_err(|_| bad())?;
        match self.index {
            None => Ok((table_key, None)),
            Some(index) => {
                let index_key = index.entry_key(start).ok_or_else(bad)?;
                Ok((index_key, Some(table_key)))
            }
        }
    }

    fn capacity_target(&self) -> Option<(&str, bool)> {
        self.index.map(|index| (index.name.as_str(), index.global))
    }

    /// Whether the read evaluates the items of the table rather than the index entries: a local
    /// index fetches each item when the read returns an attribute the index does not hold
    /// (`ALL_ATTRIBUTES`, or a projection of one).
    fn fetches(&self, select: Select, projection: Option<&[Path]>) -> bool {
        let Some(index) = self.index.filter(|index| !index.global) else { return false };
        match (select, projection) {
            (Select::AllAttributes, _) => index.projection.projection_type != ProjectionType::All,
            (Select::SpecificAttributes, Some(paths)) => index.fetches(paths, &self.table.key),
            _ => false,
        }
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

/// Refuses a query filter that reads attributes the index does not hold, like DynamoDB (measured
/// in eu-north-1 on 2026-10-31, docs/parity): for local indexes too, and whatever the query
/// returns (`ALL_ATTRIBUTES`, `COUNT` or a projection it fetches from the table). The message
/// names each place the filter reads one, in order. A scan's filter on them matches nothing.
fn check_index_filter(index: &Index, table_key: &KeyInfo, filter: &Condition) -> Result<()> {
    let missing: Vec<&str> =
        condition_paths(filter).into_iter().map(Path::root).filter(|name| !index.holds(name, table_key)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    // "Secondary index", without Global or Local, on both kinds.
    Err(invalid(format!(
        "One or more parameter values were invalid: Secondary index {} does not project one or more filter attributes: [{}]",
        index.name,
        missing.join(", ")
    )))
}

/// Refuses a projection of a global index that names attributes the index does not hold, in
/// queries and scans, like DynamoDB (measured in eu-north-1 on 2026-10-31, docs/parity); a local
/// index fetches them from the table.
fn check_index_projection(index: &Index, table_key: &KeyInfo, projection: Option<&[Path]>) -> Result<()> {
    if !index.global {
        return Ok(());
    }
    let missing: Vec<&str> =
        projection.unwrap_or_default().iter().map(Path::root).filter(|name| !index.holds(name, table_key)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(invalid(format!(
        "One or more parameter values were invalid: Global secondary index {} does not project [{}]",
        index.name,
        missing.join(", ")
    )))
}

struct PageRequest<'a> {
    filter: Option<&'a Condition>,
    projection: Option<Projector>,
    select: Select,
    /// Whether the read fetches each item from the table ([`Target::fetches`]).
    fetch: bool,
    limit: Option<u32>,
    consistent: bool,
    return_consumed_capacity: ReturnConsumedCapacity,
}

/// Evaluates candidates (already positioned after the start key, in order) into one page.
fn page<'a>(target: &Target<'_>, candidates: impl Iterator<Item = &'a Item>, request: &PageRequest<'_>) -> QueryOutput {
    let mut output = QueryOutput::default();
    let mut read = PageRead::new(request.consistent);
    let mut last: Option<&Item> = None;
    let mut stopped_early = false;
    let mut candidates = candidates.peekable();
    while let Some(item) = candidates.next() {
        let visible = target.visible(item);
        read.entry(&visible);
        output.scanned_count += 1;
        last = Some(item);
        // The filter sees the index entry: a scan's filter on attributes the index does not hold
        // matches nothing (measured, docs/parity). That it does so also when the scan fetches the
        // items from the table is assumed from DynamoDB's PartiQL SELECT, which does (round 2 of
        // scripts/probe_dynamodb.sh asks DynamoDB).
        let matches = request.filter.is_none_or(|filter| evaluate(filter, &visible));
        let evaluated = if request.fetch {
            read.fetch(item);
            Cow::Borrowed(item)
        } else {
            visible
        };
        if matches {
            output.count += 1;
            if request.select != Select::Count {
                output.items.push(match &request.projection {
                    Some(projector) => projector.apply(&evaluated),
                    None => evaluated.into_owned(),
                });
            }
        }
        let limit_reached = request.limit.is_some_and(|limit| output.scanned_count >= limit);
        if limit_reached || read.is_full() {
            stopped_early = limit_reached || candidates.peek().is_some();
            break;
        }
    }
    if stopped_early && let Some(item) = last {
        output.last_evaluated_key = Some(target.last_key(item));
    }
    let mut meter = CapacityMeter::default();
    read.charge(&mut meter, &target.table.name, target.capacity_target());
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
    let start = match &input.exclusive_start_key {
        Some(start) => Some((start, target.start_position(start, false)?)),
        None => None,
    };
    let condition = key_condition(
        parsed.key_condition.as_ref().ok_or_else(|| invalid("missing key condition"))?,
        (&key.partition.name, key.partition.kind.value_type()),
        key.sort.as_ref().map(|sort| (sort.name.as_str(), sort.kind.value_type())),
    )?;
    check_key_condition_values(&condition)?;
    if let Some((start, _)) = start {
        check_start_within_condition(start, &condition, target.index.is_none() && key.sort.is_none())?;
    }
    let start = start.map(|(_, position)| position);
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
    if let (Some(index), Some(filter)) = (target.index, &parsed.filter) {
        check_index_filter(index, &table.key, filter)?;
    }
    if let Some(index) = target.index {
        check_index_projection(index, &table.key, parsed.projection.as_deref())?;
    }
    let select = effective_select(input.select, parsed.projection.as_ref(), &target)?;
    let partition = StoredKey::new(condition.partition_value.clone(), None);
    let sort_condition = match (&condition.sort, &key.sort) {
        (Some((_, sort_condition)), Some(sort)) => Some((sort.name.as_str(), sort_condition)),
        _ => None,
    };
    let sort_matches = |item: &&Item| {
        sort_condition.is_none_or(|(name, sort_condition)| {
            item.get(name).is_some_and(|value| sort_key_matches(value, sort_condition))
        })
    };
    let request = PageRequest {
        filter: parsed.filter.as_ref(),
        projection: parsed.projection.as_deref().map(Projector::new),
        select,
        fetch: target.fetches(select, parsed.projection.as_deref()),
        limit: input.limit,
        consistent: input.consistent_read,
        return_consumed_capacity: input.return_consumed_capacity,
    };
    // The keys the query can return: the partition, narrowed by the sort key condition and by
    // the start key (in iteration order: after it, or before it for descending queries).
    let mut bounds = Bounds::new(partition.clone(), partition.partition_end());
    if let Some((_, sort_condition)) = sort_condition {
        bounds.narrow_to_sort_condition(&partition, sort_condition);
    }
    let forward = input.scan_index_forward;
    Ok(match target.index {
        None => {
            if let Some((start, _)) = &start {
                bounds.narrow_to_start(start.clone(), forward);
            }
            let range = bounds.range(&table.items);
            let candidates = range.into_iter().flatten().map(|(_, item)| item).filter(sort_matches);
            if forward { page(&target, candidates, &request) } else { page(&target, candidates.rev(), &request) }
        }
        Some(index) => {
            let mut bounds = bounds.with_table_keys();
            if let Some((start, Some(start_table_key))) = &start {
                bounds.narrow_to_start((start.clone(), start_table_key.clone()), forward);
            }
            let range = bounds.range_set(&index.entries);
            let candidates = range
                .into_iter()
                .flatten()
                .filter_map(|(_, table_key)| table.items.get(table_key))
                .filter(sort_matches);
            if forward { page(&target, candidates, &request) } else { page(&target, candidates.rev(), &request) }
        }
    })
}

/// Refuses empty strings and binary values in a key condition: no key attribute can hold one.
fn check_key_condition_values(condition: &crate::expr::KeyCondition) -> Result<()> {
    let mut values = vec![(condition.partition_name.as_str(), &condition.partition_value)];
    if let Some((name, sort)) = &condition.sort {
        match sort {
            SortKeyCondition::Compare(_, value) | SortKeyCondition::BeginsWith(value) => values.push((name, value)),
            SortKeyCondition::Between(low, high) => values.extend([(name.as_str(), low), (name.as_str(), high)]),
        }
    }
    for (name, value) in values {
        let kind = match value {
            AttributeValue::S(text) if text.is_empty() => "string",
            AttributeValue::B(bytes) if bytes.is_empty() => "binary",
            _ => continue,
        };
        return Err(invalid(format!(
            "One or more parameter values are not valid. The AttributeValue for a key attribute cannot contain an \
             empty {kind} value. Key: {name}"
        )));
    }
    Ok(())
}

/// Refuses a start key that the key condition could not have returned, as DynamoDB does: one
/// outside the sort key condition, then one of another partition ("at most one row" with a sort
/// key condition; on a table without a sort key the start key is "invalid"). Captured against
/// DynamoDB by dynoxide-rs (`check_start_key_within_condition` in `src/actions/query.rs`).
fn check_start_within_condition(
    start: &Item,
    condition: &crate::expr::KeyCondition,
    table_without_sort_key: bool,
) -> Result<()> {
    if let Some((name, sort)) = &condition.sort
        && start.get(name).is_some_and(|value| !sort_key_matches(value, sort))
    {
        return Err(invalid("The provided starting key does not match the range key predicate"));
    }
    if start.get(&condition.partition_name) != Some(&condition.partition_value) {
        return Err(invalid(if condition.sort.is_some() {
            "The query can return at most one row and cannot be restarted"
        } else if table_without_sort_key {
            "The provided exclusive start key is invalid"
        } else {
            "The provided starting key is outside query boundaries based on provided conditions"
        }));
    }
    Ok(())
}

/// The bounds of a range of keys, narrowed step by step (an empty range has no bounds).
struct Bounds<K> {
    lower: Bound<K>,
    upper: Bound<K>,
}

impl<K: Ord + Clone> Bounds<K> {
    fn new(lower: K, upper: K) -> Self {
        Self { lower: Bound::Included(lower), upper: Bound::Included(upper) }
    }

    fn raise(&mut self, bound: Bound<K>) {
        let tighter = match (&bound, &self.lower) {
            (Bound::Included(new) | Bound::Excluded(new), Bound::Included(old)) => {
                new > old || (new == old && matches!(bound, Bound::Excluded(_)))
            }
            (Bound::Included(new) | Bound::Excluded(new), Bound::Excluded(old)) => new > old,
            (Bound::Unbounded, _) => false,
            (_, Bound::Unbounded) => true,
        };
        if tighter {
            self.lower = bound;
        }
    }

    fn lower(&mut self, bound: Bound<K>) {
        let tighter = match (&bound, &self.upper) {
            (Bound::Included(new) | Bound::Excluded(new), Bound::Included(old)) => {
                new < old || (new == old && matches!(bound, Bound::Excluded(_)))
            }
            (Bound::Included(new) | Bound::Excluded(new), Bound::Excluded(old)) => new < old,
            (Bound::Unbounded, _) => false,
            (_, Bound::Unbounded) => true,
        };
        if tighter {
            self.upper = bound;
        }
    }

    /// Keeps the keys after `start` in iteration order (before it when iterating backwards).
    fn narrow_to_start(&mut self, start: K, forward: bool) {
        if forward { self.raise(Bound::Excluded(start)) } else { self.lower(Bound::Excluded(start)) }
    }

    /// Whether no key fits (`BTreeMap::range` panics on such bounds).
    fn is_empty(&self) -> bool {
        match (&self.lower, &self.upper) {
            (Bound::Included(lower), Bound::Included(upper)) => lower > upper,
            (Bound::Included(lower) | Bound::Excluded(lower), Bound::Included(upper) | Bound::Excluded(upper)) => {
                lower >= upper
            }
            _ => false,
        }
    }

    fn as_ref(&self) -> (Bound<&K>, Bound<&K>) {
        (self.lower.as_ref(), self.upper.as_ref())
    }

    fn range<'m, V>(
        &self,
        map: &'m std::collections::BTreeMap<K, V>,
    ) -> Option<std::collections::btree_map::Range<'m, K, V>> {
        (!self.is_empty()).then(|| map.range::<K, _>(self.as_ref()))
    }

    fn range_set<'m>(
        &self,
        set: &'m std::collections::BTreeSet<K>,
    ) -> Option<std::collections::btree_set::Range<'m, K>> {
        (!self.is_empty()).then(|| set.range::<K, _>(self.as_ref()))
    }
}

impl Bounds<StoredKey> {
    /// Narrows the partition's bounds to the sort keys a sort key condition accepts (the
    /// condition is still checked on each item: `begins_with` only sets the lower bound).
    fn narrow_to_sort_condition(&mut self, partition: &StoredKey, condition: &SortKeyCondition) {
        match condition {
            SortKeyCondition::Compare(comparator, value) => {
                let key = partition.with_sort(value.clone());
                match comparator {
                    Comparator::Eq => {
                        self.raise(Bound::Included(key.clone()));
                        self.lower(Bound::Included(key));
                    }
                    Comparator::Lt => self.lower(Bound::Excluded(key)),
                    Comparator::Le => self.lower(Bound::Included(key)),
                    Comparator::Gt => self.raise(Bound::Excluded(key)),
                    Comparator::Ge => self.raise(Bound::Included(key)),
                    Comparator::Ne => {}
                }
            }
            SortKeyCondition::Between(low, high) => {
                self.raise(Bound::Included(partition.with_sort(low.clone())));
                self.lower(Bound::Included(partition.with_sort(high.clone())));
            }
            SortKeyCondition::BeginsWith(prefix) => self.raise(Bound::Included(partition.with_sort(prefix.clone()))),
        }
    }

    /// The same bounds over index entries (`(index key, table key)`): an index key bound
    /// includes or excludes every entry with that index key.
    fn with_table_keys(self) -> Bounds<(StoredKey, StoredKey)> {
        let first = StoredKey::min;
        let last = StoredKey::max;
        Bounds {
            lower: match self.lower {
                Bound::Included(key) => Bound::Included((key, first())),
                Bound::Excluded(key) => Bound::Excluded((key, last())),
                Bound::Unbounded => Bound::Unbounded,
            },
            upper: match self.upper {
                Bound::Included(key) => Bound::Included((key, last())),
                Bound::Excluded(key) => Bound::Excluded((key, first())),
                Bound::Unbounded => Bound::Unbounded,
            },
        }
    }
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
    if let Some(index) = target.index {
        check_index_projection(index, &table.key, parsed.projection.as_deref())?;
    }
    let select = effective_select(input.select, parsed.projection.as_ref(), &target)?;
    let in_segment = |stored: &StoredKey| match (input.segment, input.total_segments) {
        (Some(segment), Some(total)) => stored.segment(total) == segment,
        _ => true,
    };
    let start = match &input.exclusive_start_key {
        Some(start) => Some(target.start_position(start, true)?),
        None => None,
    };
    let request = PageRequest {
        filter: parsed.filter.as_ref(),
        projection: parsed.projection.as_deref().map(Projector::new),
        select,
        fetch: target.fetches(select, parsed.projection.as_deref()),
        limit: input.limit,
        consistent: input.consistent_read,
        return_consumed_capacity: input.return_consumed_capacity,
    };
    // Items after the start key, in key order (partitions by hash).
    Ok(match target.index {
        None => {
            let range = match &start {
                Some((start, _)) => table.items.range::<StoredKey, _>((Bound::Excluded(start), Bound::Unbounded)),
                None => table.items.range::<StoredKey, _>(..),
            };
            let candidates = range.filter(|(stored, _)| in_segment(stored)).map(|(_, item)| item);
            page(&target, candidates, &request)
        }
        Some(index) => {
            let start = start.map(|(start, start_table)| (start, start_table.unwrap_or_else(StoredKey::min)));
            let range = match &start {
                Some(start) => {
                    index.entries.range::<(StoredKey, StoredKey), _>((Bound::Excluded(start), Bound::Unbounded))
                }
                None => index.entries.range::<(StoredKey, StoredKey), _>(..),
            };
            let candidates = range
                .filter(|(index_key, _)| in_segment(index_key))
                .filter_map(|(_, table_key)| table.items.get(table_key));
            page(&target, candidates, &request)
        }
    })
}
