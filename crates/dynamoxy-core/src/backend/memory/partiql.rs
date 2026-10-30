//! A PartiQL subset for the in-memory backend: the statements applications use with DynamoDB.
//!
//! ```text
//! SELECT * | path, ... FROM "table"["index"] [WHERE condition] [ORDER BY key [ASC|DESC], ...]
//! INSERT INTO "table" VALUE {'attr': value, ...}
//! UPDATE "table" SET path = value [, ...] [REMOVE path [, ...]] WHERE key = value [AND ...] [RETURNING ALL NEW *|ALL OLD *]
//! DELETE FROM "table" WHERE key = value [AND ...] [RETURNING ALL OLD *]
//! ```
//!
//! Values: `?` parameters, `'strings'`, numbers, `TRUE`, `FALSE`, `NULL`, `[lists]`, `{'maps': ...}`,
//! `<<sets>>`. Conditions: `= <> != < <= > >=`, `BETWEEN`, `IN [...]`, `IS [NOT] MISSING`,
//! `IS [NOT] NULL`, `AND`, `OR`, `NOT`, `begins_with`, `contains`, `attribute_type`, `size`.
//! `SET` values: `value`, `path + value`, `path - value`, `list_append(path, value)`,
//! `set_add(path, <<...>>)`, `set_delete(path, <<...>>)`.
//!
//! WHERE conditions become the same [`Condition`] AST as expressions, so evaluation matches.
//!
//! `ORDER BY` follows DynamoDB, which runs such a SELECT as one query per partition key value:
//! the WHERE clause must name the partition key values (`=` or `IN`, joined by `AND`, or `OR`s of
//! them), the keys are the partition and sort key of the table or index, and with more than one
//! partition key value the partition key must be one of them. Results are ordered by partition
//! key value, then by sort key, each in the direction of its first `ORDER BY` key (ascending by
//! default), and pages continue in that order.

use super::items::{Failure, Prepared, apply, prepare_delete, prepare_put, prepare_update};
use super::table::{CapacityMeter, KeyInfo, StoredKey, Table, read_units};
use super::{State, TokenRecord, now};
use crate::error::{CancellationReason, Error, Result, ServiceError};
use crate::expr::{
    Comparator, Condition, Operand, Path, PathElement, SetValue, UpdateExpression, UpdateOperand, evaluate,
    is_idempotent, project, validate_update,
};
use crate::types::{
    BatchExecuteStatementInput, BatchExecuteStatementOutput, BatchStatementError, BatchStatementResponse,
    ExecuteStatementInput, ExecuteStatementOutput, ExecuteTransactionInput, ExecuteTransactionOutput,
    ParameterizedStatement, ReturnValuesOnConditionCheckFailure,
};
use crate::value::{AttributeValue, Item, Number, item_size};

fn invalid(message: impl Into<String>) -> Error {
    Error::validation_exception(message)
}

fn syntax(near: &str) -> Error {
    invalid(format!("Statement wasn't well formed, can't be processed: unexpected token near '{near}'"))
}

#[derive(Clone, Debug, PartialEq)]
enum Token {
    Word(String),
    Quoted(String),
    Str(String),
    Number(String),
    Param,
    Symbol(&'static str),
}

fn lex(text: &str) -> Result<Vec<Token>> {
    let chars: Vec<char> = text.chars().collect();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        let c = chars[index];
        if c.is_whitespace() {
            index += 1;
            continue;
        }
        let two: String = chars[index..(index + 2).min(chars.len())].iter().collect();
        if let Some(symbol) = ["<<", ">>", "<>", "!=", "<=", ">="].into_iter().find(|symbol| *symbol == two) {
            tokens.push(Token::Symbol(symbol));
            index += 2;
            continue;
        }
        if let Some(symbol) = ["(", ")", "[", "]", "{", "}", ",", ".", ":", "*", "=", "<", ">", "+", "-"]
            .into_iter()
            .find(|symbol| symbol.starts_with(c))
        {
            // A sign directly followed by a digit after an operator is part of a number literal.
            let negative_number = c == '-'
                && chars.get(index + 1).is_some_and(char::is_ascii_digit)
                && !matches!(
                    tokens.last(),
                    Some(Token::Word(_) | Token::Quoted(_) | Token::Number(_) | Token::Str(_) | Token::Param)
                        | Some(Token::Symbol(")" | "]"))
                );
            if !negative_number {
                tokens.push(Token::Symbol(symbol));
                index += 1;
                continue;
            }
        }
        match c {
            '?' => {
                tokens.push(Token::Param);
                index += 1;
            }
            '\'' | '"' => {
                let mut value = String::new();
                index += 1;
                loop {
                    match chars.get(index) {
                        None => {
                            return Err(invalid(
                                "Statement wasn't well formed, can't be processed: unterminated literal",
                            ));
                        }
                        Some(&quote) if quote == c => {
                            if chars.get(index + 1) == Some(&c) {
                                value.push(c);
                                index += 2;
                            } else {
                                index += 1;
                                break;
                            }
                        }
                        Some(&other) => {
                            value.push(other);
                            index += 1;
                        }
                    }
                }
                tokens.push(if c == '\'' { Token::Str(value) } else { Token::Quoted(value) });
            }
            _ if c.is_ascii_digit() || c == '-' => {
                let start = index;
                index += 1;
                while index < chars.len()
                    && (chars[index].is_ascii_alphanumeric()
                        || chars[index] == '.'
                        || ((chars[index] == '-' || chars[index] == '+') && matches!(chars[index - 1], 'e' | 'E')))
                {
                    index += 1;
                }
                tokens.push(Token::Number(chars[start..index].iter().collect()));
            }
            _ if c.is_alphanumeric() || c == '_' => {
                let start = index;
                while index < chars.len() && (chars[index].is_alphanumeric() || chars[index] == '_') {
                    index += 1;
                }
                tokens.push(Token::Word(chars[start..index].iter().collect()));
            }
            other => return Err(syntax(&other.to_string())),
        }
    }
    Ok(tokens)
}

/// Which attributes an `UPDATE` / `DELETE` returns.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Returning {
    None,
    AllOld,
    AllNew,
}

/// One key of an `ORDER BY` clause.
#[derive(Debug)]
struct OrderKey {
    name: String,
    descending: bool,
}

#[derive(Debug)]
enum Statement {
    Select {
        table: String,
        index: Option<String>,
        projection: Option<Vec<Path>>,
        condition: Option<Condition>,
        order: Vec<OrderKey>,
    },
    Insert {
        table: String,
        item: Item,
    },
    Update {
        table: String,
        update: UpdateExpression,
        condition: Condition,
        returning: Returning,
    },
    Delete {
        table: String,
        condition: Condition,
        returning: Returning,
    },
}

impl Statement {
    fn table(&self) -> &str {
        match self {
            Self::Select { table, .. }
            | Self::Insert { table, .. }
            | Self::Update { table, .. }
            | Self::Delete { table, .. } => table,
        }
    }

    fn is_read(&self) -> bool {
        matches!(self, Self::Select { .. })
    }
}

/// How deep parentheses, `NOT` and the brackets of values may nest. Values hold at most 32
/// levels and conditions this deep are contrived; the parser refuses deeper statements before its
/// recursion could exhaust the stack of the thread it runs on (2 MiB for runtime threads, and
/// statements are parsed on every backend, see [`effect`]), which would kill the process.
const MAX_NESTING: usize = 100;

struct Parser<'a> {
    tokens: Vec<Token>,
    position: usize,
    parameters: &'a [AttributeValue],
    next_parameter: usize,
    /// How many parentheses, `NOT`s and value brackets enclose the current position.
    depth: usize,
}

impl Parser<'_> {
    /// Runs `parse` one nesting level deeper (see [`MAX_NESTING`]).
    fn nested<T>(&mut self, parse: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        if self.depth == MAX_NESTING {
            return Err(invalid(format!(
                "Statement wasn't well formed, can't be processed: it nests more than {MAX_NESTING} levels deep"
            )));
        }
        self.depth += 1;
        let parsed = parse(self);
        self.depth -= 1;
        parsed
    }

    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.position)
    }

    fn next(&mut self) -> Option<Token> {
        let token = self.tokens.get(self.position).cloned();
        self.position += usize::from(token.is_some());
        token
    }

    fn error(&self) -> Error {
        match self.peek() {
            Some(Token::Word(word) | Token::Quoted(word) | Token::Str(word) | Token::Number(word)) => syntax(word),
            Some(Token::Symbol(symbol)) => syntax(symbol),
            Some(Token::Param) => syntax("?"),
            None => syntax("<EOF>"),
        }
    }

    fn at_keyword(&self, keyword: &str) -> bool {
        matches!(self.peek(), Some(Token::Word(word)) if word.eq_ignore_ascii_case(keyword))
    }

    fn keyword(&mut self, keyword: &str) -> Result<()> {
        if self.at_keyword(keyword) {
            self.position += 1;
            Ok(())
        } else {
            Err(self.error())
        }
    }

    fn symbol(&mut self, symbol: &'static str) -> Result<()> {
        if self.peek() == Some(&Token::Symbol(symbol)) {
            self.position += 1;
            Ok(())
        } else {
            Err(self.error())
        }
    }

    fn at_symbol(&self, symbol: &str) -> bool {
        matches!(self.peek(), Some(Token::Symbol(found)) if *found == symbol)
    }

    fn identifier(&mut self) -> Result<String> {
        match self.next() {
            Some(Token::Word(word) | Token::Quoted(word)) => Ok(word),
            _ => {
                self.position = self.position.saturating_sub(1);
                Err(self.error())
            }
        }
    }

    fn table_ref(&mut self) -> Result<(String, Option<String>)> {
        let table = self.identifier()?;
        let index = if self.at_symbol(".") {
            self.position += 1;
            Some(self.identifier()?)
        } else {
            None
        };
        Ok((table, index))
    }

    fn path(&mut self) -> Result<Path> {
        let mut elements = vec![PathElement::Name(self.identifier()?)];
        loop {
            if self.at_symbol(".") {
                self.position += 1;
                elements.push(PathElement::Name(self.identifier()?));
            } else if self.at_symbol("[") {
                self.position += 1;
                match self.next() {
                    Some(Token::Number(digits)) => {
                        elements.push(PathElement::Index(digits.parse().map_err(|_| syntax(&digits))?));
                    }
                    Some(Token::Str(name)) => elements.push(PathElement::Name(name)),
                    _ => return Err(self.error()),
                }
                self.symbol("]")?;
            } else {
                return Ok(Path(elements));
            }
        }
    }

    fn parameter(&mut self) -> Result<AttributeValue> {
        let value = self
            .parameters
            .get(self.next_parameter)
            .cloned()
            .ok_or_else(|| invalid("Number of parameters in request and statement don't match."))?;
        self.next_parameter += 1;
        Ok(value)
    }

    fn literal(&mut self) -> Result<AttributeValue> {
        match self.next() {
            Some(Token::Param) => self.parameter(),
            Some(Token::Str(text)) => Ok(AttributeValue::S(text)),
            Some(Token::Number(text)) => {
                Number::parse(&text).map(AttributeValue::N).map_err(|error| invalid(error.to_string()))
            }
            Some(Token::Word(word)) if word.eq_ignore_ascii_case("true") => Ok(AttributeValue::Bool(true)),
            Some(Token::Word(word)) if word.eq_ignore_ascii_case("false") => Ok(AttributeValue::Bool(false)),
            Some(Token::Word(word)) if word.eq_ignore_ascii_case("null") => Ok(AttributeValue::Null),
            Some(Token::Symbol("[")) => self.nested(|parser| {
                let mut values = Vec::new();
                while !parser.at_symbol("]") {
                    values.push(parser.literal()?);
                    if !parser.at_symbol("]") {
                        parser.symbol(",")?;
                    }
                }
                parser.symbol("]")?;
                Ok(AttributeValue::L(values))
            }),
            Some(Token::Symbol("{")) => self.nested(|parser| {
                let mut map = Item::new();
                while !parser.at_symbol("}") {
                    let key = match parser.next() {
                        Some(Token::Str(key) | Token::Quoted(key)) => key,
                        _ => return Err(parser.error()),
                    };
                    parser.symbol(":")?;
                    map.insert(key, parser.literal()?);
                    if !parser.at_symbol("}") {
                        parser.symbol(",")?;
                    }
                }
                parser.symbol("}")?;
                Ok(AttributeValue::M(map))
            }),
            Some(Token::Symbol("<<")) => self.nested(|parser| {
                let mut values = Vec::new();
                while !parser.at_symbol(">>") {
                    values.push(parser.literal()?);
                    if !parser.at_symbol(">>") {
                        parser.symbol(",")?;
                    }
                }
                parser.symbol(">>")?;
                set_value(values)
            }),
            _ => {
                self.position = self.position.saturating_sub(1);
                Err(self.error())
            }
        }
    }

    fn is_value_start(&self) -> bool {
        match self.peek() {
            Some(Token::Param | Token::Str(_) | Token::Number(_)) => true,
            Some(Token::Symbol(symbol)) => matches!(*symbol, "[" | "{" | "<<"),
            Some(Token::Word(word)) => {
                ["true", "false", "null"].iter().any(|literal| word.eq_ignore_ascii_case(literal))
            }
            _ => false,
        }
    }

    fn operand(&mut self) -> Result<Operand> {
        if self.is_value_start() {
            return Ok(Operand::Value(self.literal()?));
        }
        if matches!(self.peek(), Some(Token::Word(word)) if word.eq_ignore_ascii_case("size"))
            && self.tokens.get(self.position + 1) == Some(&Token::Symbol("("))
        {
            self.position += 2;
            let path = self.path()?;
            self.symbol(")")?;
            return Ok(Operand::Size(path));
        }
        Ok(Operand::Path(self.path()?))
    }

    fn condition(&mut self) -> Result<Condition> {
        let mut left = self.and_condition()?;
        while self.at_keyword("OR") {
            self.position += 1;
            left = Condition::Or(Box::new(left), Box::new(self.and_condition()?));
        }
        Ok(left)
    }

    fn and_condition(&mut self) -> Result<Condition> {
        let mut left = self.not_condition()?;
        while self.at_keyword("AND") {
            self.position += 1;
            left = Condition::And(Box::new(left), Box::new(self.not_condition()?));
        }
        Ok(left)
    }

    fn not_condition(&mut self) -> Result<Condition> {
        if self.at_keyword("NOT") {
            self.position += 1;
            return self.nested(|parser| Ok(Condition::Not(Box::new(parser.not_condition()?))));
        }
        // Parentheses are handled here rather than in `primary`, whose large frame then stays off
        // the stack of nested conditions.
        if self.at_symbol("(") {
            self.position += 1;
            let inner = self.nested(Self::condition)?;
            self.symbol(")")?;
            return Ok(inner);
        }
        self.primary()
    }

    fn primary(&mut self) -> Result<Condition> {
        if let Some(Token::Word(name)) = self.peek().cloned()
            && self.tokens.get(self.position + 1) == Some(&Token::Symbol("("))
            && ["begins_with", "contains", "attribute_type", "exists"]
                .iter()
                .any(|function| name.eq_ignore_ascii_case(function))
        {
            self.position += 2;
            let path = self.path()?;
            let lower = name.to_ascii_lowercase();
            let condition = if lower == "exists" {
                Condition::AttributeExists(path)
            } else {
                self.symbol(",")?;
                let argument = Operand::Value(self.literal()?);
                match lower.as_str() {
                    "begins_with" => Condition::BeginsWith(path, argument),
                    "contains" => Condition::Contains(path, argument),
                    _ => Condition::AttributeType(path, argument),
                }
            };
            self.symbol(")")?;
            return Ok(condition);
        }
        let left = self.operand()?;
        if self.at_keyword("IS") {
            self.position += 1;
            let negated = self.at_keyword("NOT");
            if negated {
                self.position += 1;
            }
            let Operand::Path(path) = left else { return Err(self.error()) };
            let condition = if self.at_keyword("MISSING") {
                self.position += 1;
                Condition::AttributeNotExists(path)
            } else if self.at_keyword("NULL") {
                self.position += 1;
                Condition::AttributeType(path, Operand::Value(AttributeValue::s("NULL")))
            } else {
                return Err(self.error());
            };
            return Ok(if negated { Condition::Not(Box::new(condition)) } else { condition });
        }
        if self.at_keyword("BETWEEN") {
            self.position += 1;
            let low = self.operand()?;
            self.keyword("AND")?;
            let high = self.operand()?;
            return Ok(Condition::Between(left, low, high));
        }
        if self.at_keyword("IN") {
            self.position += 1;
            let close = if self.at_symbol("[") { "]" } else { ")" };
            self.position += 1;
            let mut candidates = Vec::new();
            while !self.at_symbol(close) {
                candidates.push(self.operand()?);
                if !self.at_symbol(close) {
                    self.symbol(",")?;
                }
            }
            self.position += 1;
            return Ok(Condition::In(left, candidates));
        }
        let comparator = match self.next() {
            Some(Token::Symbol("=")) => Comparator::Eq,
            Some(Token::Symbol("<>" | "!=")) => Comparator::Ne,
            Some(Token::Symbol("<")) => Comparator::Lt,
            Some(Token::Symbol("<=")) => Comparator::Le,
            Some(Token::Symbol(">")) => Comparator::Gt,
            Some(Token::Symbol(">=")) => Comparator::Ge,
            _ => {
                self.position = self.position.saturating_sub(1);
                return Err(self.error());
            }
        };
        Ok(Condition::Compare(left, comparator, self.operand()?))
    }

    fn update_operand(&mut self) -> Result<UpdateOperand> {
        if self.is_value_start() {
            return Ok(UpdateOperand::Value(self.literal()?));
        }
        Ok(UpdateOperand::Path(self.path()?))
    }

    fn returning(&mut self) -> Result<Returning> {
        if !self.at_keyword("RETURNING") {
            return Ok(Returning::None);
        }
        self.position += 1;
        self.keyword("ALL")?;
        let returning = if self.at_keyword("OLD") {
            Returning::AllOld
        } else if self.at_keyword("NEW") {
            Returning::AllNew
        } else {
            return Err(self.error());
        };
        self.position += 1;
        if !self.at_symbol("*") {
            return Err(self.error());
        }
        self.position += 1;
        Ok(returning)
    }

    fn statement(&mut self) -> Result<Statement> {
        let statement = if self.at_keyword("SELECT") {
            self.position += 1;
            let projection = if self.at_symbol("*") {
                self.position += 1;
                None
            } else {
                let mut paths = vec![self.path()?];
                while self.at_symbol(",") {
                    self.position += 1;
                    paths.push(self.path()?);
                }
                Some(paths)
            };
            self.keyword("FROM")?;
            let (table, index) = self.table_ref()?;
            let condition = if self.at_keyword("WHERE") {
                self.position += 1;
                Some(self.condition()?)
            } else {
                None
            };
            let mut order = Vec::new();
            if self.at_keyword("ORDER") {
                self.position += 1;
                self.keyword("BY")?;
                order.push(self.order_key()?);
                while self.at_symbol(",") {
                    self.position += 1;
                    order.push(self.order_key()?);
                }
            }
            Statement::Select { table, index, projection, condition, order }
        } else if self.at_keyword("INSERT") {
            self.position += 1;
            self.keyword("INTO")?;
            let (table, _) = self.table_ref()?;
            self.keyword("VALUE")?;
            let AttributeValue::M(item) = self.literal()? else {
                return Err(invalid("Unsupported operation: Inserting a value that is not a tuple is not supported"));
            };
            Statement::Insert { table, item }
        } else if self.at_keyword("UPDATE") {
            self.position += 1;
            let (table, _) = self.table_ref()?;
            let mut update = UpdateExpression::default();
            loop {
                if self.at_keyword("SET") {
                    self.position += 1;
                    let path = self.path()?;
                    self.symbol("=")?;
                    self.set_value(path, &mut update)?;
                } else if self.at_keyword("REMOVE") {
                    self.position += 1;
                    update.remove.push(self.path()?);
                } else if self.at_symbol(",") {
                    self.position += 1;
                    // A comma continues the previous clause: `SET a = 1, b = 2`.
                    if matches!(self.peek(), Some(Token::Word(_) | Token::Quoted(_)))
                        && !self.at_keyword("SET")
                        && !self.at_keyword("REMOVE")
                    {
                        let path = self.path()?;
                        if self.at_symbol("=") {
                            self.position += 1;
                            self.set_value(path, &mut update)?;
                        } else {
                            update.remove.push(path);
                        }
                    }
                } else {
                    break;
                }
            }
            self.keyword("WHERE")?;
            let condition = self.condition()?;
            let returning = self.returning()?;
            Statement::Update { table, update, condition, returning }
        } else if self.at_keyword("DELETE") {
            self.position += 1;
            self.keyword("FROM")?;
            let (table, _) = self.table_ref()?;
            self.keyword("WHERE")?;
            let condition = self.condition()?;
            let returning = self.returning()?;
            Statement::Delete { table, condition, returning }
        } else {
            return Err(self.error());
        };
        if self.position < self.tokens.len() {
            return Err(self.error());
        }
        if self.next_parameter != self.parameters.len() {
            return Err(invalid("Number of parameters in request and statement don't match."));
        }
        Ok(statement)
    }

    fn order_key(&mut self) -> Result<OrderKey> {
        let path = self.path()?;
        let [PathElement::Name(name)] = path.0.as_slice() else {
            return Err(invalid("Argument to ORDER BY must be of type VariableReference. Type: PATH"));
        };
        let name = name.clone();
        let descending = self.at_keyword("DESC");
        if descending || self.at_keyword("ASC") {
            self.position += 1;
        }
        Ok(OrderKey { name, descending })
    }

    fn set_value(&mut self, path: Path, update: &mut UpdateExpression) -> Result<()> {
        if let Some(Token::Word(name)) = self.peek().cloned()
            && self.tokens.get(self.position + 1) == Some(&Token::Symbol("("))
        {
            let lower = name.to_ascii_lowercase();
            if ["list_append", "set_add", "set_delete", "if_not_exists"].contains(&lower.as_str()) {
                self.position += 2;
                let first = self.update_operand()?;
                self.symbol(",")?;
                let second = self.update_operand()?;
                self.symbol(")")?;
                match (lower.as_str(), second) {
                    ("list_append", second) => {
                        update.set.push((
                            path,
                            SetValue::Operand(UpdateOperand::ListAppend(Box::new(first), Box::new(second))),
                        ));
                    }
                    ("if_not_exists", second) => {
                        let UpdateOperand::Path(existing) = first else { return Err(self.error()) };
                        update
                            .set
                            .push((path, SetValue::Operand(UpdateOperand::IfNotExists(existing, Box::new(second)))));
                    }
                    ("set_add", UpdateOperand::Value(value)) => update.add.push((path, value)),
                    ("set_delete", UpdateOperand::Value(value)) => update.delete.push((path, value)),
                    _ => return Err(self.error()),
                }
                return Ok(());
            }
        }
        let left = self.update_operand()?;
        let value = if self.at_symbol("+") {
            self.position += 1;
            SetValue::Plus(left, self.update_operand()?)
        } else if self.at_symbol("-") {
            self.position += 1;
            SetValue::Minus(left, self.update_operand()?)
        } else {
            SetValue::Operand(left)
        };
        update.set.push((path, value));
        Ok(())
    }
}

fn set_value(values: Vec<AttributeValue>) -> Result<AttributeValue> {
    let mut strings = Vec::new();
    let mut numbers = Vec::new();
    let mut binaries = Vec::new();
    for value in values {
        match value {
            AttributeValue::S(text) => strings.push(text),
            AttributeValue::N(number) => numbers.push(number),
            AttributeValue::B(bytes) => binaries.push(bytes),
            _ => return Err(invalid("Unsupported type: sets may only contain strings, numbers or binaries")),
        }
    }
    match (strings.is_empty(), numbers.is_empty(), binaries.is_empty()) {
        (false, true, true) => Ok(AttributeValue::Ss(strings)),
        (true, false, true) => Ok(AttributeValue::Ns(numbers)),
        (true, true, false) => Ok(AttributeValue::Bs(binaries)),
        _ => Err(invalid("A set must be non-empty and contain values of a single type")),
    }
}

fn parse(statement: &str, parameters: &[AttributeValue]) -> Result<Statement> {
    let tokens = lex(statement)?;
    Parser { tokens, position: 0, parameters, next_parameter: 0, depth: 0 }.statement()
}

/// What a statement does, for the client's retries of attempts that may have been applied (any
/// backend: statements are classified with this parser).
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Effect {
    /// A `SELECT`.
    Read,
    /// A write that may be sent again: `INSERT`, `DELETE` or an idempotent `UPDATE`.
    Resendable {
        /// `RETURNING ALL OLD *`.
        returns_old: bool,
        /// A `DELETE`.
        delete: bool,
    },
    /// An `UPDATE` that would apply twice, or a statement this parser does not know.
    Once,
}

/// The [`Effect`] of a statement.
pub(crate) fn effect(statement: &str, parameters: &[AttributeValue]) -> Effect {
    match parse(statement, parameters) {
        Ok(Statement::Select { .. }) => Effect::Read,
        Ok(Statement::Insert { .. }) => Effect::Resendable { returns_old: false, delete: false },
        Ok(Statement::Update { update, returning, .. }) if is_idempotent(&update) => {
            Effect::Resendable { returns_old: returning == Returning::AllOld, delete: false }
        }
        Ok(Statement::Update { .. }) => Effect::Once,
        Ok(Statement::Delete { returning, .. }) => {
            Effect::Resendable { returns_old: returning == Returning::AllOld, delete: true }
        }
        // Syntax this subset does not parse: reads are safe to send again, writes may not be.
        Err(_) => {
            let first = statement.split_whitespace().next().unwrap_or_default();
            if first.eq_ignore_ascii_case("SELECT") { Effect::Read } else { Effect::Once }
        }
    }
}

/// The values of top-level `attr = value` equalities joined by `AND`.
fn equalities(condition: &Condition, out: &mut Vec<(String, AttributeValue)>) {
    match condition {
        Condition::And(left, right) => {
            equalities(left, out);
            equalities(right, out);
        }
        Condition::Compare(Operand::Path(path), Comparator::Eq, Operand::Value(value))
        | Condition::Compare(Operand::Value(value), Comparator::Eq, Operand::Path(path))
            if path.is_top_level() =>
        {
            out.push((path.root().to_owned(), value.clone()));
        }
        _ => {}
    }
}

/// The partition key values a WHERE clause requires (DynamoDB's "non-optional hash key
/// conditions"): `key = value` and `key IN [...]`, joined by `AND`, or `OR`s of them. `None` when
/// an item with another partition key value could match.
fn partition_values(condition: &Condition, partition: &str) -> Option<Vec<AttributeValue>> {
    let names_partition = |path: &Path| path.is_top_level() && path.root() == partition;
    match condition {
        Condition::Compare(Operand::Path(path), Comparator::Eq, Operand::Value(value))
        | Condition::Compare(Operand::Value(value), Comparator::Eq, Operand::Path(path))
            if names_partition(path) =>
        {
            Some(vec![value.clone()])
        }
        Condition::In(Operand::Path(path), candidates) if names_partition(path) => candidates
            .iter()
            .map(|candidate| match candidate {
                Operand::Value(value) => Some(value.clone()),
                _ => None,
            })
            .collect(),
        Condition::And(left, right) => partition_values(left, partition).or_else(|| partition_values(right, partition)),
        Condition::Or(left, right) => {
            let mut values = partition_values(left, partition)?;
            values.extend(partition_values(right, partition)?);
            Some(values)
        }
        _ => None,
    }
}

/// Checks an `ORDER BY` clause like DynamoDB and returns the directions of the partition and
/// the sort key (`true`: descending).
fn order_directions(order: &[OrderKey], condition: Option<&Condition>, key: &KeyInfo) -> Result<(bool, bool)> {
    let condition =
        condition.ok_or_else(|| invalid("Must have WHERE clause in the statement when using ORDER BY clause."))?;
    let values = partition_values(condition, &key.partition.name).ok_or_else(|| {
        invalid("Must have at least one non-optional hash key condition in WHERE clause when using ORDER BY clause.")
    })?;
    let sort = key.sort.as_ref().map(|sort| sort.name.as_str());
    if let Some(other) =
        order.iter().find(|entry| entry.name != key.partition.name && Some(entry.name.as_str()) != sort)
    {
        return Err(invalid(format!(
            "Variable reference {} in ORDER BY clause must be part of the primary key",
            other.name
        )));
    }
    let direction =
        |name: Option<&str>| order.iter().find(|entry| Some(entry.name.as_str()) == name).map(|entry| entry.descending);
    let partition = direction(Some(&key.partition.name));
    if values.len() > 1 && partition.is_none() {
        return Err(invalid(
            "Must have hash key in ORDER BY clause when more than one hash key condition specified in WHERE clause.",
        ));
    }
    Ok((partition.unwrap_or(false), direction(sort).unwrap_or(false)))
}

/// The order of two values of a key attribute (by value, as DynamoDB orders keys).
fn key_order(left: &Item, right: &Item, name: &str) -> std::cmp::Ordering {
    match (left.get(name), right.get(name)) {
        (Some(left), Some(right)) => left.cmp_scalar(right).unwrap_or(std::cmp::Ordering::Equal),
        _ => std::cmp::Ordering::Equal,
    }
}

/// The full primary key named by a WHERE clause (required by UPDATE and DELETE).
fn full_key(table: &Table, condition: &Condition) -> Result<Item> {
    let mut found = Vec::new();
    equalities(condition, &mut found);
    let mut key = Item::new();
    for name in table.key.names() {
        let value = found
            .iter()
            .find(|(attribute, _)| attribute == name)
            .map(|(_, value)| value.clone())
            .ok_or_else(|| invalid("Where clause does not contain a mandatory equality on all key attributes"))?;
        key.insert(name.to_owned(), value);
    }
    Ok(key)
}

/// Prepares a write statement (shared by single statements, batches and transactions).
fn prepare_write(state: &State, statement: &Statement) -> Result<(Prepared, Returning), Failure> {
    let table = state.table(statement.table())?;
    let none = ReturnValuesOnConditionCheckFailure::None;
    match statement {
        Statement::Insert { item, .. } => {
            let key = table.validate_item(item)?;
            if table.items.contains_key(&key) {
                return Err(Failure::Invalid(
                    ServiceError::new("DuplicateItemException", "Duplicate primary key exists in table").into(),
                ));
            }
            Ok((prepare_put(table, item.clone(), None, none)?, Returning::None))
        }
        Statement::Update { update, condition, returning, .. } => {
            validate_update(update, &table.key.names())?;
            let key = full_key(table, condition)?;
            let stored = table.key.exact_key(&key)?;
            if !table.items.contains_key(&stored) {
                return Err(Failure::Condition(None));
            }
            Ok((prepare_update(table, &key, Some(update), Some(condition), none)?, *returning))
        }
        Statement::Delete { condition, returning, .. } => {
            let key = full_key(table, condition)?;
            let stored = table.key.exact_key(&key)?;
            if !table.items.contains_key(&stored) {
                let prepared = Prepared {
                    table: table.name.clone(),
                    key: stored,
                    new: None,
                    writes: false,
                    read_size: 0,
                    updated_paths: Vec::new(),
                };
                return Ok((prepared, *returning));
            }
            Ok((prepare_delete(table, &key, Some(condition), none)?, *returning))
        }
        Statement::Select { .. } => Err(Failure::Invalid(invalid("not a write statement"))),
    }
}

fn returned(prepared_old: Option<Item>, prepared_new: Option<Item>, returning: Returning) -> Vec<Item> {
    match returning {
        Returning::None => Vec::new(),
        Returning::AllOld => prepared_old.into_iter().collect(),
        Returning::AllNew => prepared_new.into_iter().collect(),
    }
}

fn select(
    state: &State,
    statement: &Statement,
    consistent: bool,
    limit: Option<u32>,
    next_token: Option<&str>,
) -> Result<ExecuteStatementOutput> {
    let Statement::Select { table: table_name, index, projection, condition, order } = statement else {
        return Err(invalid("not a SELECT statement"));
    };
    let table = state.table(table_name)?;
    let index = match index {
        Some(name) => Some(table.index(name)?),
        None => None,
    };
    if consistent && index.is_some_and(|index| index.global) {
        return Err(invalid("Consistent reads are not supported on global secondary indexes"));
    }
    let key = index.map_or(&table.key, |index| &index.key);
    let directions = if order.is_empty() { None } else { Some(order_directions(order, condition.as_ref(), key)?) };
    // A WHERE clause that names the partition key values is one query per value (no scan): only
    // their items are read, evaluated (`Limit`) and charged.
    let partitions: Option<Vec<StoredKey>> = condition
        .as_ref()
        .and_then(|condition| partition_values(condition, &key.partition.name))
        .map(|values| values.into_iter().map(|value| StoredKey::new(value, None)).collect());
    let queried = |stored: &StoredKey| {
        partitions.as_ref().is_none_or(|partitions| {
            partitions.iter().any(|partition| stored.hash == partition.hash && stored.partition == partition.partition)
        })
    };
    let mut candidates: Vec<&Item> = match index {
        None => table.items.iter().filter(|(stored, _)| queried(stored)).map(|(_, item)| item).collect(),
        Some(index) => index
            .entries
            .iter()
            .filter(|(stored, _)| queried(stored))
            .filter_map(|(_, table_key)| table.items.get(table_key))
            .collect(),
    };
    if let Some((partition_descending, sort_descending)) = directions {
        // Candidates are in key order within a partition, but partitions are in hash order.
        let directed =
            |ordering: std::cmp::Ordering, descending: bool| if descending { ordering.reverse() } else { ordering };
        candidates.sort_by(|left, right| {
            directed(key_order(left, right, &key.partition.name), partition_descending).then_with(|| match &key.sort {
                Some(sort) => directed(key_order(left, right, &sort.name), sort_descending),
                None => std::cmp::Ordering::Equal,
            })
        });
    }
    let offset: usize = match next_token {
        Some(token) => token
            .strip_prefix("offset:")
            .and_then(|offset| offset.parse().ok())
            .ok_or_else(|| invalid("Invalid NextToken"))?,
        None => 0,
    };
    candidates.drain(..offset.min(candidates.len()));
    let mut output = ExecuteStatementOutput::default();
    let mut read = 0usize;
    let mut evaluated = 0usize;
    for item in &candidates {
        let visible = match index {
            Some(index) => index.project(item, &table.key),
            None => (*item).clone(),
        };
        evaluated += 1;
        read += item_size(&visible);
        if condition.as_ref().is_none_or(|condition| evaluate(condition, &visible)) {
            output.items.push(match projection {
                Some(paths) => project(&visible, paths),
                None => visible,
            });
        }
        if limit.is_some_and(|limit| evaluated >= limit as usize) || read >= 1024 * 1024 {
            break;
        }
    }
    if evaluated < candidates.len() {
        output.next_token = Some(format!("offset:{}", offset + evaluated));
    }
    let mut meter = CapacityMeter::default();
    meter.read(&table.name, index.map(|index| (index.name.as_str(), index.global)), read_units(read, consistent));
    output.consumed_capacity = meter.table_report(&table.name, crate::types::ReturnConsumedCapacity::Total);
    Ok(output)
}

/// `ExecuteStatement`.
pub fn execute_statement(state: &mut State, input: &ExecuteStatementInput) -> Result<ExecuteStatementOutput> {
    let statement = parse(&input.statement, &input.parameters)?;
    if statement.is_read() {
        let mut output = select(state, &statement, input.consistent_read, input.limit, input.next_token.as_deref())?;
        if input.return_consumed_capacity == crate::types::ReturnConsumedCapacity::None {
            output.consumed_capacity = None;
        }
        return Ok(output);
    }
    let (prepared, returning) = prepare_write(state, &statement).map_err(|failure| match failure {
        Failure::Condition(_) => {
            let item = match input.return_values_on_condition_check_failure {
                ReturnValuesOnConditionCheckFailure::AllOld => {
                    state.tables.get(statement.table()).and_then(|table| match &statement {
                        Statement::Update { condition, .. } | Statement::Delete { condition, .. } => {
                            full_key(table, condition)
                                .ok()
                                .and_then(|key| table.key.exact_key(&key).ok())
                                .and_then(|key| table.items.get(&key).cloned())
                        }
                        _ => None,
                    })
                }
                ReturnValuesOnConditionCheckFailure::None => None,
            };
            ServiceError::conditional_check_failed(item).into()
        }
        Failure::Invalid(error) => error,
    })?;
    let table_name = prepared.table.clone();
    let new = prepared.new.clone();
    let mut meter = CapacityMeter::default();
    let old = if prepared.writes { apply(state, prepared, &mut meter, 1.0) } else { None };
    Ok(ExecuteStatementOutput {
        items: returned(old, new, returning),
        next_token: None,
        last_evaluated_key: None,
        consumed_capacity: meter.table_report(&table_name, input.return_consumed_capacity),
    })
}

fn statement_error(error: &Error) -> BatchStatementError {
    let (code, message) = match error {
        Error::Service(service) => {
            let code = match service.code.as_str() {
                "ConditionalCheckFailedException" => "ConditionalCheckFailed",
                "DuplicateItemException" => "DuplicateItem",
                "ResourceNotFoundException" => "ResourceNotFound",
                "ProvisionedThroughputExceededException" => "ProvisionedThroughputExceeded",
                "TransactionConflictException" => "TransactionConflict",
                _ => "ValidationError",
            };
            (code.to_owned(), service.message.clone())
        }
        other => ("ValidationError".to_owned(), other.to_string()),
    };
    BatchStatementError { code, message: Some(message), item: None }
}

/// `BatchExecuteStatement`: up to 25 statements, each succeeding or failing on its own.
pub fn batch_execute_statement(
    state: &mut State,
    input: &BatchExecuteStatementInput,
) -> Result<BatchExecuteStatementOutput> {
    if input.statements.is_empty() || input.statements.len() > 25 {
        return Err(invalid(
            "1 validation error detected: Value at 'statements' failed to satisfy constraint: Member must have length less than or equal to 25 and greater than or equal to 1",
        ));
    }
    let mut output = BatchExecuteStatementOutput::default();
    let mut meter = CapacityMeter::default();
    for request in &input.statements {
        let response = match parse(&request.statement, &request.parameters) {
            Err(error) => BatchStatementResponse { error: Some(statement_error(&error)), table_name: None, item: None },
            Ok(statement) if statement.is_read() => {
                let table_name = statement.table().to_owned();
                let has_key = state.tables.get(&table_name).is_some_and(|table| match &statement {
                    Statement::Select { condition: Some(condition), index: None, .. } => {
                        full_key(table, condition).is_ok()
                    }
                    _ => false,
                });
                if !has_key {
                    BatchStatementResponse {
                        error: Some(BatchStatementError {
                            code: "ValidationError".into(),
                            message: Some("Select statements within BatchExecuteStatement must specify the primary key in the where clause".into()),
                            item: None,
                        }),
                        table_name: Some(table_name),
                        item: None,
                    }
                } else {
                    match select(state, &statement, request.consistent_read, None, None) {
                        Ok(result) => BatchStatementResponse {
                            error: None,
                            table_name: Some(table_name),
                            item: result.items.into_iter().next(),
                        },
                        Err(error) => BatchStatementResponse {
                            error: Some(statement_error(&error)),
                            table_name: Some(table_name),
                            item: None,
                        },
                    }
                }
            }
            Ok(statement) => {
                let table_name = statement.table().to_owned();
                match prepare_write(state, &statement) {
                    Ok((prepared, _)) => {
                        if prepared.writes {
                            apply(state, prepared, &mut meter, 1.0);
                        }
                        BatchStatementResponse { error: None, table_name: Some(table_name), item: None }
                    }
                    Err(failure) => {
                        let error = failure.into_error();
                        BatchStatementResponse {
                            error: Some(statement_error(&error)),
                            table_name: Some(table_name),
                            item: None,
                        }
                    }
                }
            }
        };
        output.responses.push(response);
    }
    output.consumed_capacity = meter.reports(input.return_consumed_capacity);
    Ok(output)
}

/// `ExecuteTransaction`: all reads or all writes; writes are applied all or nothing.
pub fn execute_transaction(state: &mut State, input: &ExecuteTransactionInput) -> Result<ExecuteTransactionOutput> {
    if input.transact_statements.is_empty() || input.transact_statements.len() > 100 {
        return Err(invalid(
            "1 validation error detected: Value at 'transactStatements' failed to satisfy constraint: Member must have length less than or equal to 100 and greater than or equal to 1",
        ));
    }
    let statements = input
        .transact_statements
        .iter()
        .map(|statement: &ParameterizedStatement| parse(&statement.statement, &statement.parameters))
        .collect::<Result<Vec<_>>>()?;
    let reads = statements.iter().filter(|statement| statement.is_read()).count();
    if reads != 0 && reads != statements.len() {
        return Err(invalid("Transaction must contain either all reads or all writes"));
    }
    if let Some(token) = &input.client_request_token {
        let fingerprint = {
            use std::hash::{DefaultHasher, Hash, Hasher};
            let mut hasher = DefaultHasher::new();
            format!("{:?}", input.transact_statements).hash(&mut hasher);
            hasher.finish()
        };
        let current = now();
        if let Some(record) = state.tokens.get(token, current) {
            if record.fingerprint != fingerprint {
                return Err(ServiceError::new(
                    "IdempotentParameterMismatchException",
                    "Specified idempotency token has already been used for a different request",
                )
                .into());
            }
            return Ok(ExecuteTransactionOutput::default());
        }
        if reads == 0 {
            state.tokens.insert(token.clone(), TokenRecord { fingerprint, at: current });
        }
    }
    let mut output = ExecuteTransactionOutput::default();
    if reads > 0 {
        for statement in &statements {
            let Statement::Select { condition: Some(condition), table, .. } = statement else {
                return Err(invalid(
                    "Select statements within a transaction must specify the primary key in the where clause",
                ));
            };
            let table = state.table(table)?;
            full_key(table, condition)?;
            output.responses.push(select(state, statement, true, None, None)?.items.into_iter().next());
        }
        return Ok(output);
    }
    let mut prepared = Vec::new();
    let mut reasons = Vec::new();
    let mut canceled = false;
    for statement in &statements {
        match prepare_write(state, statement) {
            Ok((write, _)) => {
                reasons.push(CancellationReason::none());
                prepared.push(write);
            }
            Err(Failure::Condition(item)) => {
                canceled = true;
                reasons.push(CancellationReason {
                    code: "ConditionalCheckFailed".into(),
                    message: Some("The conditional request failed".into()),
                    item,
                });
            }
            Err(Failure::Invalid(error)) => {
                canceled = true;
                let code = statement_error(&error).code;
                let message = match &error {
                    Error::Service(service) => service.message.clone(),
                    other => other.to_string(),
                };
                reasons.push(CancellationReason { code, message: Some(message), item: None });
            }
        }
    }
    if canceled {
        if let Some(token) = &input.client_request_token {
            state.tokens.remove(token);
        }
        return Err(ServiceError::transaction_canceled(reasons).into());
    }
    let mut meter = CapacityMeter::default();
    for write in prepared {
        if write.writes {
            apply(state, write, &mut meter, 2.0);
        }
    }
    output.consumed_capacity = meter.reports(input.return_consumed_capacity);
    Ok(output)
}
