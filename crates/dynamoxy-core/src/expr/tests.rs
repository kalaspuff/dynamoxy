use super::parser::MAX_NESTING;
use super::*;
use crate::value::{Item, ValueType, item};

fn names(pairs: &[(&str, &str)]) -> Names {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), (*value).to_owned())).collect()
}

fn values(pairs: &[(&str, AttributeValue)]) -> Values {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), value.clone())).collect()
}

fn condition(text: &str, names: &Names, values: &Values) -> Result<Condition> {
    let parsed = parse_request(ExpressionInput { condition: Some(text), ..Default::default() }, names, values)?;
    Ok(parsed.condition.expect("parsed"))
}

fn holds(text: &str, values: &Values, item: &Item) -> bool {
    let names = names(&[("#s", "status")]);
    let mut placeholders = Placeholders::new(&names, values);
    let condition = parse_condition(ExpressionKind::Condition, text, &mut placeholders).unwrap();
    evaluate(&condition, item)
}

#[test]
fn and_binds_tighter_than_or_and_not_binds_tightest() {
    let values = values(&[(":one", AttributeValue::n(1))]);
    let parsed = condition("a = :one OR b = :one AND NOT c = :one", &Names::new(), &values).unwrap();
    let Condition::Or(_, right) = parsed else { panic!("OR must be the root: {parsed:?}") };
    let Condition::And(_, not) = *right else { panic!("AND under OR") };
    assert!(matches!(*not, Condition::Not(_)));
}

#[test]
fn keywords_are_case_insensitive_and_functions_parse() {
    let values = values(&[(":a", AttributeValue::n(1)), (":b", AttributeValue::n(5)), (":p", AttributeValue::s("x"))]);
    let parsed = condition(
        "n between :a and :b and (begins_with(s, :p) or attribute_not_exists(m.x[2]) or size(l) > :a)",
        &Names::new(),
        &values,
    )
    .unwrap();
    assert!(matches!(parsed, Condition::And(..)));
}

#[test]
fn reserved_words_need_placeholders() {
    let values = values(&[(":v", AttributeValue::s("x"))]);
    let error = condition("status = :v", &Names::new(), &values).unwrap_err();
    assert_eq!(
        error.to_string(),
        "ValidationException: Invalid ConditionExpression: Attribute name is a reserved keyword; reserved keyword: status"
    );
    assert!(condition("#s = :v", &names(&[("#s", "status")]), &values).is_ok());
}

#[test]
fn placeholders_must_be_defined_and_used() {
    let error = condition("a = :missing", &Names::new(), &Values::new()).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("An expression attribute value used in expression is not defined; attribute value: :missing")
    );
    let error = condition("#a = :v", &Names::new(), &values(&[(":v", AttributeValue::n(1))])).unwrap_err();
    assert!(error.to_string().contains("attribute name: #a"));
    let error = condition("a = :v", &names(&[("#x", "x")]), &values(&[(":v", AttributeValue::n(1))])).unwrap_err();
    assert!(error.to_string().contains("Value provided in ExpressionAttributeNames unused in expressions: keys: {#x}"));
    let error =
        condition("a = :v", &Names::new(), &values(&[(":v", AttributeValue::n(1)), (":w", AttributeValue::n(2))]))
            .unwrap_err();
    assert!(
        error.to_string().contains("Value provided in ExpressionAttributeValues unused in expressions: keys: {:w}")
    );
}

#[test]
fn placeholder_maps_are_validated() {
    let error = parse_request(ExpressionInput::default(), &names(&[("#a", "a")]), &Values::new()).unwrap_err();
    assert!(error.to_string().contains("can only be specified when using expressions"));
    let error = condition("a = :v", &names(&[("a", "a")]), &values(&[(":v", AttributeValue::n(1))])).unwrap_err();
    assert!(error.to_string().contains("ExpressionAttributeNames contains invalid key"));
    let error = condition("a = :v", &Names::new(), &values(&[(":v", AttributeValue::Ss(vec![]))])).unwrap_err();
    assert!(error.to_string().contains("ExpressionAttributeValues contains invalid value"));
}

#[test]
fn syntax_errors_name_the_token() {
    for text in ["a = ", "a == :v", "(a = :v", "a = :v)", "a $ :v", "foo(a)", "a BETWEEN :v"] {
        let error = condition(text, &Names::new(), &values(&[(":v", AttributeValue::n(1))])).unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("Syntax error") || message.contains("Invalid function name") || message.contains("unused"),
            "{text}: {message}"
        );
    }
    let error = condition("", &Names::new(), &Values::new()).unwrap_err();
    assert!(error.to_string().contains("The expression can not be empty"));
}

#[test]
fn literal_operand_types_are_checked() {
    let error = condition("a < :v", &Names::new(), &values(&[(":v", AttributeValue::Bool(true))])).unwrap_err();
    assert!(error.to_string().contains("operator or function: <, operand type: BOOL"));
    let error = condition("begins_with(a, :v)", &Names::new(), &values(&[(":v", AttributeValue::n(1))])).unwrap_err();
    assert!(error.to_string().contains("begins_with"));
    let error =
        condition("attribute_type(a, :v)", &Names::new(), &values(&[(":v", AttributeValue::s("X"))])).unwrap_err();
    assert!(error.to_string().contains("Invalid attribute type name found; type: X"));
    let error = condition(
        "a BETWEEN :hi AND :lo",
        &Names::new(),
        &values(&[(":lo", AttributeValue::n(1)), (":hi", AttributeValue::n(5))]),
    )
    .unwrap_err();
    assert!(error.to_string().contains("BETWEEN operator requires upper bound"));
}

#[test]
fn evaluation_follows_dynamodb_semantics() {
    let doc = item([
        ("status", AttributeValue::s("active")),
        ("n", AttributeValue::n(5)),
        ("tags", AttributeValue::Ss(vec!["a".into(), "b".into()])),
        ("l", AttributeValue::L(vec![AttributeValue::s("x"), AttributeValue::n(1)])),
        ("m", AttributeValue::M(item([("deep", AttributeValue::Bool(true))]))),
        ("bin", AttributeValue::b(vec![1, 2, 3])),
    ]);
    let v = values(&[
        (":active", AttributeValue::s("active")),
        (":act", AttributeValue::s("act")),
        (":five", AttributeValue::n(5)),
        (":one", AttributeValue::n(1)),
        (":ten", AttributeValue::n(10)),
        (":text", AttributeValue::s("5")),
        (":a", AttributeValue::s("a")),
        (":x", AttributeValue::s("x")),
        (":ss", AttributeValue::s("SS")),
        (":two", AttributeValue::n(2)),
        (":bytes", AttributeValue::b(vec![2, 3])),
    ]);
    assert!(holds("#s = :active", &v, &doc));
    assert!(holds("n BETWEEN :one AND :ten", &v, &doc));
    assert!(!holds("n = :text", &v, &doc), "different types are never equal");
    assert!(!holds("n < :text", &v, &doc), "ordering across types is false, not an error");
    assert!(!holds("absent = :one", &v, &doc));
    assert!(holds("absent <> :one", &v, &doc), "<> with a missing attribute is true");
    assert!(holds("n IN (:one, :five)", &v, &doc));
    assert!(holds("contains(tags, :a) AND contains(l, :x) AND contains(#s, :act)", &v, &doc));
    assert!(holds("contains(bin, :bytes)", &v, &doc));
    assert!(holds("begins_with(#s, :act)", &v, &doc));
    assert!(holds("attribute_type(tags, :ss)", &v, &doc));
    assert!(holds("size(tags) = :two AND size(#s) > :five", &v, &doc));
    assert!(holds("attribute_exists(m.deep) AND attribute_not_exists(m.shallow) AND attribute_exists(l[1])", &v, &doc));
    assert!(holds("NOT (n > :ten)", &v, &doc));
    assert!(holds("attribute_not_exists(anything)", &Values::new(), &Item::new()));
}

#[test]
fn key_conditions_are_extracted_and_checked() {
    let all = values(&[
        (":pk", AttributeValue::s("u1")),
        (":lo", AttributeValue::n(1)),
        (":hi", AttributeValue::n(9)),
        (":p", AttributeValue::s("A")),
    ]);
    // Gives each case exactly the placeholders it uses (unused ones are an error of their own).
    let check = |text: &str, used: &[&str]| -> String {
        let subset: Values =
            all.iter().filter(|(key, _)| used.contains(&key.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect();
        let input = ExpressionInput { key_condition: Some(text), ..Default::default() };
        let result = parse_request(input, &Names::new(), &subset).and_then(|parsed| {
            key_condition(&parsed.key_condition.expect("parsed"), ("pk", ValueType::S), Some(("sk", ValueType::N)))
        });
        match result {
            Ok(condition) => format!("{condition:?}"),
            Err(error) => error.to_string(),
        }
    };
    assert!(check("pk = :pk AND sk BETWEEN :lo AND :hi", &[":pk", ":lo", ":hi"]).contains("Between"));
    assert!(check(":pk = pk", &[":pk"]).contains("partition_value"));
    assert!(check("sk > :lo", &[":lo"]).contains("Query condition missed key schema element: pk"));
    assert!(
        check("pk = :pk OR sk = :lo", &[":pk", ":lo"]).contains("Invalid operator used in KeyConditionExpression: OR")
    );
    assert!(check("pk = :pk AND pk = :pk", &[":pk"]).contains("one condition per key"));
    assert!(check("pk = :lo", &[":lo"]).contains("Condition parameter type does not match schema type"));
    assert!(check("pk = :pk AND begins_with(sk, :p)", &[":pk", ":p"]).contains("does not match schema type"));
    assert!(check("pk < :pk", &[":pk"]).contains("Query key condition not supported"));
}

#[test]
fn sort_key_matching() {
    let five = AttributeValue::n(5);
    assert!(sort_key_matches(&five, &SortKeyCondition::Compare(Comparator::Ge, AttributeValue::n(5))));
    assert!(sort_key_matches(&five, &SortKeyCondition::Between(AttributeValue::n(1), AttributeValue::n(5))));
    assert!(sort_key_matches(
        &AttributeValue::s("ORDER#1"),
        &SortKeyCondition::BeginsWith(AttributeValue::s("ORDER#"))
    ));
    assert!(!sort_key_matches(&AttributeValue::s("USER"), &SortKeyCondition::BeginsWith(AttributeValue::s("ORDER#"))));
}

#[test]
fn update_and_projection_parse_through_parse_request() {
    let parsed = parse_request(
        ExpressionInput {
            update: Some("SET a = :v REMOVE b ADD c :n DELETE d :s"),
            projection: Some("a, #b.c"),
            ..Default::default()
        },
        &names(&[("#b", "b")]),
        &values(&[
            (":v", AttributeValue::n(1)),
            (":n", AttributeValue::n(2)),
            (":s", AttributeValue::Ss(vec!["x".into()])),
        ]),
    )
    .unwrap();
    let update = parsed.update.unwrap();
    assert_eq!((update.set.len(), update.remove.len(), update.add.len(), update.delete.len()), (1, 1, 1, 1));
    assert_eq!(parsed.projection.unwrap().len(), 2);
}

/// Parses on a thread with the stack of the runtime's threads, where a deep recursion would
/// overflow and kill the process.
fn on_runtime_stack<T: Send + 'static>(parse: impl FnOnce() -> T + Send + 'static) -> T {
    std::thread::Builder::new().stack_size(2 * 1024 * 1024).spawn(parse).unwrap().join().unwrap()
}

#[test]
fn deeply_nested_expressions_are_refused_without_exhausting_the_stack() {
    let parse = |text: String| {
        on_runtime_stack(move || {
            let values = values(&[(":v", AttributeValue::n(1))]);
            condition(&text, &Names::new(), &values).map(|_| ()).map_err(|error| error.to_string())
        })
    };
    let parenthesized = |levels: usize| format!("{}a = :v{}", "(".repeat(levels), ")".repeat(levels));
    let negated = |levels: usize| format!("{}a = :v", "NOT ".repeat(levels));
    assert_eq!(parse(parenthesized(MAX_NESTING)), Ok(()));
    assert_eq!(parse(negated(MAX_NESTING)), Ok(()));
    // 2,000 parentheses fit in the 4 KB of an expression; they used to overflow the stack.
    for text in [parenthesized(MAX_NESTING + 1), negated(MAX_NESTING + 1), parenthesized(2000)] {
        let error = parse(text).unwrap_err();
        assert_eq!(
            error,
            "ValidationException: Invalid ConditionExpression: The expression nests parentheses, NOT and functions \
             more than 100 levels deep"
        );
    }

    let update = |levels: usize| {
        on_runtime_stack(move || {
            let (names, values) = (Names::new(), values(&[(":v", AttributeValue::L(vec![]))]));
            let mut placeholders = Placeholders::new(&names, &values);
            let text = format!("SET l = {}l{}", "list_append(".repeat(levels), ", :v)".repeat(levels));
            parse_update(&text, &mut placeholders).map(|_| ()).map_err(|error| error.to_string())
        })
    };
    assert_eq!(update(MAX_NESTING), Ok(()));
    assert!(update(MAX_NESTING + 1).unwrap_err().contains("more than 100 levels deep"));
}

#[test]
fn characters_outside_ascii_are_syntax_errors() {
    let values = values(&[(":v", AttributeValue::n(1))]);
    for (text, token, near) in [
        ("größe > :v", "ö", "größe"),
        ("a\u{a0}= :v", "\u{a0}", "a\u{a0}="),
        ("a = :v AND é = :v", "é", "AND é"),
        ("#\u{e9} = :v", "#", "#\u{e9}"),
    ] {
        let error = condition(text, &Names::new(), &values).unwrap_err();
        assert_eq!(
            error.to_string(),
            format!(
                "ValidationException: Invalid ConditionExpression: Syntax error; token: \"{token}\", near: \"{near}\""
            ),
        );
    }
    let no_names = Names::new();
    let mut placeholders = Placeholders::new(&no_names, &values);
    assert!(parse_update("SET a = :v, ü = :v", &mut placeholders).unwrap_err().to_string().contains("token: \"ü\""));
    let mut placeholders = Placeholders::new(&no_names, &values);
    assert!(parse_projection("a, ñ", &mut placeholders).unwrap_err().to_string().contains("token: \"ñ\""));
}
