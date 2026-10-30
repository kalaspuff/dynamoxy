//! Lexer and recursive-descent parser for DynamoDB expressions.
//!
//! Grammar (precedence from loosest to tightest: `OR`, `AND`, `NOT`, comparisons/functions):
//!
//! ```text
//! condition   := and ("OR" and)*
//! and         := not ("AND" not)*
//! not         := "NOT" not | primary
//! primary     := "(" condition ")" | function | operand comparison
//! comparison  := cmp operand | "BETWEEN" operand "AND" operand | "IN" "(" operand ("," operand)* ")"
//! function    := attribute_exists(path) | attribute_not_exists(path) | attribute_type(path, operand)
//!              | begins_with(path, operand) | contains(path, operand)
//! operand     := path | :value | size(path)
//! path        := (name | #name) ("." (name | #name) | "[" digits "]")*
//! update      := ("SET" set ("," set)* | "REMOVE" path ("," path)* | "ADD" path :value ("," ...)*
//!                 | "DELETE" path :value ("," ...)*)+
//! set         := path "=" uoperand (("+" | "-") uoperand)?
//! uoperand    := path | :value | if_not_exists(path, uoperand) | list_append(uoperand, uoperand)
//! projection  := path ("," path)*
//! ```
//!
//! Keywords are case-insensitive; function names are not. Placeholders are resolved while
//! parsing, and every placeholder that is used is recorded so that unused ones can be reported.

use std::collections::{BTreeMap, BTreeSet};

use super::ast::{Comparator, Condition, Operand, Path, PathElement, SetValue, UpdateExpression, UpdateOperand};
use super::reserved::is_reserved;
use crate::error::{Error, Result};
use crate::value::AttributeValue;

/// Which expression is being parsed (DynamoDB names it in error messages).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExpressionKind {
    /// `ConditionExpression`
    Condition,
    /// `FilterExpression`
    Filter,
    /// `KeyConditionExpression`
    KeyCondition,
    /// `UpdateExpression`
    Update,
    /// `ProjectionExpression`
    Projection,
}

impl ExpressionKind {
    /// The request parameter name.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Condition => "ConditionExpression",
            Self::Filter => "FilterExpression",
            Self::KeyCondition => "KeyConditionExpression",
            Self::Update => "UpdateExpression",
            Self::Projection => "ProjectionExpression",
        }
    }
}

/// Expression attribute names and values of one request, and which of them were used.
#[derive(Debug)]
pub struct Placeholders<'a> {
    names: &'a BTreeMap<String, String>,
    values: &'a BTreeMap<String, AttributeValue>,
    used_names: BTreeSet<&'a str>,
    used_values: BTreeSet<&'a str>,
}

impl<'a> Placeholders<'a> {
    /// Wraps a request's `ExpressionAttributeNames` and `ExpressionAttributeValues`.
    pub fn new(names: &'a BTreeMap<String, String>, values: &'a BTreeMap<String, AttributeValue>) -> Self {
        Self { names, values, used_names: BTreeSet::new(), used_values: BTreeSet::new() }
    }

    /// Placeholder names that no expression used.
    pub fn unused_names(&self) -> Vec<&str> {
        self.names.keys().map(String::as_str).filter(|name| !self.used_names.contains(name)).collect()
    }

    /// Placeholder values that no expression used.
    pub fn unused_values(&self) -> Vec<&str> {
        self.values.keys().map(String::as_str).filter(|name| !self.used_values.contains(name)).collect()
    }
}

/// A token: a slice of the expression for words, placeholders and integers.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Token<'t> {
    Ident(&'t str),
    NamePlaceholder(&'t str),
    ValuePlaceholder(&'t str),
    Integer(&'t str),
    LParen,
    RParen,
    LBracket,
    RBracket,
    Comma,
    Dot,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    Plus,
    Minus,
}

impl Token<'_> {
    fn text(&self) -> String {
        match self {
            Self::Ident(text) | Self::NamePlaceholder(text) | Self::ValuePlaceholder(text) | Self::Integer(text) => {
                (*text).to_owned()
            }
            Self::LParen => "(".into(),
            Self::RParen => ")".into(),
            Self::LBracket => "[".into(),
            Self::RBracket => "]".into(),
            Self::Comma => ",".into(),
            Self::Dot => ".".into(),
            Self::Eq => "=".into(),
            Self::Ne => "<>".into(),
            Self::Lt => "<".into(),
            Self::Le => "<=".into(),
            Self::Gt => ">".into(),
            Self::Ge => ">=".into(),
            Self::Plus => "+".into(),
            Self::Minus => "-".into(),
        }
    }

    fn is_keyword(&self, keyword: &str) -> bool {
        matches!(self, Self::Ident(text) if text.eq_ignore_ascii_case(keyword))
    }
}

#[derive(Clone, Copy, Debug)]
struct Spanned<'t> {
    token: Token<'t>,
    start: usize,
    end: usize,
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn lex(kind: ExpressionKind, text: &str) -> Result<Vec<Spanned<'_>>> {
    let bytes = text.as_bytes();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        let start = index;
        if byte.is_ascii_whitespace() {
            index += 1;
            continue;
        }
        let token = match byte {
            b'(' => Token::LParen,
            b')' => Token::RParen,
            b'[' => Token::LBracket,
            b']' => Token::RBracket,
            b',' => Token::Comma,
            b'.' => Token::Dot,
            b'=' => Token::Eq,
            b'+' => Token::Plus,
            b'-' => Token::Minus,
            b'<' => match bytes.get(index + 1) {
                Some(b'>') => {
                    index += 1;
                    Token::Ne
                }
                Some(b'=') => {
                    index += 1;
                    Token::Le
                }
                _ => Token::Lt,
            },
            b'>' => {
                if bytes.get(index + 1) == Some(&b'=') {
                    index += 1;
                    Token::Ge
                } else {
                    Token::Gt
                }
            }
            b'#' | b':' => {
                let mut end = index + 1;
                while end < bytes.len() && is_word_byte(bytes[end]) {
                    end += 1;
                }
                if end == index + 1 {
                    return Err(syntax_error(kind, text, &char_at(text, index), start, start + 1));
                }
                let word = &text[index..end];
                index = end - 1;
                if byte == b'#' { Token::NamePlaceholder(word) } else { Token::ValuePlaceholder(word) }
            }
            b'0'..=b'9' => {
                let mut end = index;
                while end < bytes.len() && is_word_byte(bytes[end]) {
                    end += 1;
                }
                let word = &text[index..end];
                index = end - 1;
                if word.bytes().all(|b| b.is_ascii_digit()) { Token::Integer(word) } else { Token::Ident(word) }
            }
            _ if is_word_byte(byte) => {
                let mut end = index;
                while end < bytes.len() && is_word_byte(bytes[end]) {
                    end += 1;
                }
                let word = &text[index..end];
                index = end - 1;
                Token::Ident(word)
            }
            _ => {
                return Err(syntax_error(kind, text, &char_at(text, index), start, start + 1));
            }
        };
        index += 1;
        tokens.push(Spanned { token, start, end: index });
    }
    Ok(tokens)
}

fn char_at(text: &str, index: usize) -> String {
    text[index..].chars().next().map(String::from).unwrap_or_default()
}

fn syntax_error(kind: ExpressionKind, text: &str, token: &str, start: usize, end: usize) -> Error {
    let near_start = text[..start].trim_end().rfind(char::is_whitespace).map_or(0, |position| position + 1);
    let near_end = text[end.min(text.len())..].find(char::is_whitespace).map_or(text.len(), |position| end + position);
    let near = &text[near_start.min(start)..near_end.max(end).min(text.len())];
    Error::validation_exception(format!(
        "Invalid {}: Syntax error; token: \"{token}\", near: \"{near}\"",
        kind.as_str()
    ))
}

const CONDITION_FUNCTIONS: [&str; 5] =
    ["attribute_exists", "attribute_not_exists", "attribute_type", "begins_with", "contains"];

/// How deep parentheses, `NOT` and functions may nest. DynamoDB refuses redundant parentheses,
/// so each level holds operators (300 at most per expression), and only contrived expressions
/// nest this deep. The parser refuses deeper ones before its recursion could exhaust the stack of
/// the thread it runs on (2 MiB for runtime threads), which would kill the process.
pub(super) const MAX_NESTING: usize = 100;

struct Parser<'p, 'a> {
    kind: ExpressionKind,
    text: &'p str,
    tokens: Vec<Spanned<'p>>,
    position: usize,
    placeholders: &'p mut Placeholders<'a>,
    /// How many parentheses, `NOT`s and functions enclose the current position.
    depth: usize,
}

impl<'p, 'a> Parser<'p, 'a> {
    fn new(kind: ExpressionKind, text: &'p str, placeholders: &'p mut Placeholders<'a>) -> Result<Self> {
        if text.trim().is_empty() {
            return Err(Error::validation_exception(format!(
                "Invalid {}: The expression can not be empty;",
                kind.as_str()
            )));
        }
        if text.len() > 4096 {
            return Err(Error::validation_exception(format!(
                "Invalid {}: Expression size has exceeded the maximum allowed size; expression size: {}",
                kind.as_str(),
                text.len()
            )));
        }
        let tokens = lex(kind, text)?;
        Ok(Self { kind, text, tokens, position: 0, placeholders, depth: 0 })
    }

    /// Runs `parse` one nesting level deeper (see [`MAX_NESTING`]).
    fn nested<T>(&mut self, parse: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        if self.depth == MAX_NESTING {
            return Err(self.error(format!(
                "The expression nests parentheses, NOT and functions more than {MAX_NESTING} levels deep"
            )));
        }
        self.depth += 1;
        let parsed = parse(self);
        self.depth -= 1;
        parsed
    }

    fn error(&self, message: impl std::fmt::Display) -> Error {
        Error::validation_exception(format!("Invalid {}: {message}", self.kind.as_str()))
    }

    fn peek(&self) -> Option<Token<'p>> {
        self.tokens.get(self.position).map(|spanned| spanned.token)
    }

    fn peek_at(&self, offset: usize) -> Option<Token<'p>> {
        self.tokens.get(self.position + offset).map(|spanned| spanned.token)
    }

    fn next(&mut self) -> Option<Token<'p>> {
        let token = self.peek();
        if token.is_some() {
            self.position += 1;
        }
        token
    }

    fn unexpected(&self) -> Error {
        match self.tokens.get(self.position) {
            Some(spanned) => syntax_error(self.kind, self.text, &spanned.token.text(), spanned.start, spanned.end),
            None => {
                let start = self.tokens.last().map_or(0, |spanned| spanned.start);
                syntax_error(self.kind, self.text, "<EOF>", start, self.text.len())
            }
        }
    }

    fn expect(&mut self, expected: Token<'_>) -> Result<()> {
        if self.peek() == Some(expected) {
            self.position += 1;
            Ok(())
        } else {
            Err(self.unexpected())
        }
    }

    fn at_keyword(&self, keyword: &str) -> bool {
        self.peek().is_some_and(|token| token.is_keyword(keyword))
    }

    fn finish(&self) -> Result<()> {
        if self.position < self.tokens.len() { Err(self.unexpected()) } else { Ok(()) }
    }

    // ---- paths and operands -------------------------------------------------------------

    fn name_element(&mut self, token: Token<'_>) -> Result<String> {
        match token {
            Token::Ident(name) => {
                if is_reserved(name) {
                    return Err(self.error(format!("Attribute name is a reserved keyword; reserved keyword: {name}")));
                }
                if name.as_bytes()[0].is_ascii_digit() {
                    return Err(self.unexpected_previous());
                }
                Ok(name.to_owned())
            }
            Token::NamePlaceholder(placeholder) => {
                let Some((key, name)) = self.placeholders.names.get_key_value(placeholder) else {
                    return Err(self.error(format!(
                        "An expression attribute name used in the document path is not defined; attribute name: {placeholder}"
                    )));
                };
                self.placeholders.used_names.insert(key);
                Ok(name.clone())
            }
            _ => Err(self.unexpected_previous()),
        }
    }

    fn unexpected_previous(&self) -> Error {
        let spanned = &self.tokens[self.position.saturating_sub(1)];
        syntax_error(self.kind, self.text, &spanned.token.text(), spanned.start, spanned.end)
    }

    fn path(&mut self) -> Result<Path> {
        let Some(first) = self.next() else { return Err(self.unexpected()) };
        let mut elements = vec![PathElement::Name(self.name_element(first)?)];
        loop {
            match self.peek() {
                Some(Token::Dot) => {
                    self.position += 1;
                    let Some(token) = self.next() else { return Err(self.unexpected()) };
                    elements.push(PathElement::Name(self.name_element(token)?));
                }
                Some(Token::LBracket) => {
                    self.position += 1;
                    match self.next() {
                        Some(Token::Integer(digits)) => {
                            let index = digits.parse::<usize>().map_err(|_| self.unexpected_previous())?;
                            elements.push(PathElement::Index(index));
                        }
                        _ => return Err(self.unexpected_previous()),
                    }
                    self.expect(Token::RBracket)?;
                }
                _ => break,
            }
        }
        if elements.len() > 32 {
            return Err(self.error("The document path has too many nesting levels; nesting levels: 33"));
        }
        Ok(Path(elements))
    }

    fn value(&mut self, placeholder: &str) -> Result<AttributeValue> {
        let Some((key, value)) = self.placeholders.values.get_key_value(placeholder) else {
            return Err(self.error(format!(
                "An expression attribute value used in expression is not defined; attribute value: {placeholder}"
            )));
        };
        self.placeholders.used_values.insert(key);
        Ok(value.clone())
    }

    fn operand(&mut self) -> Result<Operand> {
        match self.peek() {
            Some(Token::ValuePlaceholder(placeholder)) => {
                self.position += 1;
                Ok(Operand::Value(self.value(placeholder)?))
            }
            Some(Token::Ident(name)) if self.peek_at(1) == Some(Token::LParen) => {
                if name != "size" {
                    return Err(self.error(format!("Invalid function name; function: {name}")));
                }
                self.position += 2;
                let path = self.path()?;
                self.expect(Token::RParen)?;
                Ok(Operand::Size(path))
            }
            Some(Token::Ident(_) | Token::NamePlaceholder(_)) => Ok(Operand::Path(self.path()?)),
            _ => Err(self.unexpected()),
        }
    }

    // ---- conditions ---------------------------------------------------------------------

    fn condition(&mut self) -> Result<Condition> {
        let mut left = self.and_condition()?;
        while self.at_keyword("OR") {
            self.position += 1;
            let right = self.and_condition()?;
            left = Condition::Or(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn and_condition(&mut self) -> Result<Condition> {
        let mut left = self.not_condition()?;
        while self.at_keyword("AND") {
            self.position += 1;
            let right = self.not_condition()?;
            left = Condition::And(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn not_condition(&mut self) -> Result<Condition> {
        if self.at_keyword("NOT") {
            self.position += 1;
            return self.nested(|parser| Ok(Condition::Not(Box::new(parser.not_condition()?))));
        }
        // Parentheses are handled here rather than in `primary_condition`, whose large frame then
        // stays off the stack of nested conditions.
        if self.peek() == Some(Token::LParen) {
            self.position += 1;
            let inner = self.nested(Self::condition)?;
            self.expect(Token::RParen)?;
            return Ok(inner);
        }
        self.primary_condition()
    }

    fn primary_condition(&mut self) -> Result<Condition> {
        if let Some(Token::Ident(name)) = self.peek()
            && self.peek_at(1) == Some(Token::LParen)
            && name != "size"
        {
            if !CONDITION_FUNCTIONS.contains(&name) {
                return Err(self.error(format!("Invalid function name; function: {name}")));
            }
            self.position += 2;
            return self.function_condition(name);
        }
        let left = self.operand()?;
        let comparator = match self.peek() {
            Some(Token::Eq) => Some(Comparator::Eq),
            Some(Token::Ne) => Some(Comparator::Ne),
            Some(Token::Lt) => Some(Comparator::Lt),
            Some(Token::Le) => Some(Comparator::Le),
            Some(Token::Gt) => Some(Comparator::Gt),
            Some(Token::Ge) => Some(Comparator::Ge),
            _ => None,
        };
        if let Some(comparator) = comparator {
            self.position += 1;
            let right = self.operand()?;
            return Ok(Condition::Compare(left, comparator, right));
        }
        if self.at_keyword("BETWEEN") {
            self.position += 1;
            let low = self.operand()?;
            if !self.at_keyword("AND") {
                return Err(self.unexpected());
            }
            self.position += 1;
            let high = self.operand()?;
            return Ok(Condition::Between(left, low, high));
        }
        if self.at_keyword("IN") {
            self.position += 1;
            self.expect(Token::LParen)?;
            let mut candidates = vec![self.operand()?];
            while self.peek() == Some(Token::Comma) {
                self.position += 1;
                candidates.push(self.operand()?);
            }
            self.expect(Token::RParen)?;
            if candidates.len() > 100 {
                return Err(self.error(format!(
                    "The IN operator is provided with too many operands; number of operands: {}",
                    candidates.len()
                )));
            }
            return Ok(Condition::In(left, candidates));
        }
        Err(self.unexpected())
    }

    fn function_condition(&mut self, name: &str) -> Result<Condition> {
        let path = match self.operand()? {
            Operand::Path(path) => path,
            other => {
                return Err(self.error(format!(
                    "Incorrect operand type for operator or function; operator or function: {name}, operand type: {}",
                    operand_type_name(&other)
                )));
            }
        };
        let condition = match name {
            "attribute_exists" => Condition::AttributeExists(path),
            "attribute_not_exists" => Condition::AttributeNotExists(path),
            _ => {
                self.expect(Token::Comma)?;
                let argument = self.operand()?;
                match name {
                    "attribute_type" => Condition::AttributeType(path, argument),
                    "begins_with" => Condition::BeginsWith(path, argument),
                    _ => Condition::Contains(path, argument),
                }
            }
        };
        self.expect(Token::RParen)?;
        Ok(condition)
    }

    // ---- update expressions ---------------------------------------------------------------

    fn update(&mut self) -> Result<UpdateExpression> {
        let mut update = UpdateExpression::default();
        let mut seen: Vec<&str> = Vec::with_capacity(4);
        while let Some(token) = self.next() {
            let section = match token {
                Token::Ident(word) => ["SET", "REMOVE", "ADD", "DELETE"]
                    .into_iter()
                    .find(|section| word.eq_ignore_ascii_case(section))
                    .ok_or_else(|| self.unexpected_previous())?,
                _ => return Err(self.unexpected_previous()),
            };
            if seen.contains(&section) {
                return Err(
                    self.error(format!("The \"{section}\" section can only be used once in an update expression;"))
                );
            }
            seen.push(section);
            loop {
                match section {
                    "SET" => {
                        let path = self.path()?;
                        self.expect(Token::Eq)?;
                        let left = self.update_operand()?;
                        let value = match self.peek() {
                            Some(Token::Plus) => {
                                self.position += 1;
                                SetValue::Plus(left, self.update_operand()?)
                            }
                            Some(Token::Minus) => {
                                self.position += 1;
                                SetValue::Minus(left, self.update_operand()?)
                            }
                            _ => SetValue::Operand(left),
                        };
                        update.set.push((path, value));
                    }
                    "REMOVE" => update.remove.push(self.path()?),
                    _ => {
                        let path = self.path()?;
                        let value = match self.next() {
                            Some(Token::ValuePlaceholder(placeholder)) => self.value(placeholder)?,
                            _ => return Err(self.unexpected_previous()),
                        };
                        if section == "ADD" {
                            update.add.push((path, value));
                        } else {
                            update.delete.push((path, value));
                        }
                    }
                }
                if self.peek() == Some(Token::Comma) {
                    self.position += 1;
                } else {
                    break;
                }
            }
        }
        Ok(update)
    }

    fn update_operand(&mut self) -> Result<UpdateOperand> {
        match self.peek() {
            Some(Token::ValuePlaceholder(placeholder)) => {
                self.position += 1;
                Ok(UpdateOperand::Value(self.value(placeholder)?))
            }
            Some(Token::Ident(name)) if self.peek_at(1) == Some(Token::LParen) => {
                self.position += 2;
                let operand = self.nested(|parser| match name {
                    "if_not_exists" => {
                        let path = parser.path()?;
                        parser.expect(Token::Comma)?;
                        Ok(UpdateOperand::IfNotExists(path, Box::new(parser.update_operand()?)))
                    }
                    "list_append" => {
                        let first = parser.update_operand()?;
                        parser.expect(Token::Comma)?;
                        Ok(UpdateOperand::ListAppend(Box::new(first), Box::new(parser.update_operand()?)))
                    }
                    _ => Err(parser.error(format!("Invalid function name; function: {name}"))),
                })?;
                self.expect(Token::RParen)?;
                Ok(operand)
            }
            Some(Token::Ident(_) | Token::NamePlaceholder(_)) => Ok(UpdateOperand::Path(self.path()?)),
            _ => Err(self.unexpected()),
        }
    }

    fn projection(&mut self) -> Result<Vec<Path>> {
        let mut paths = vec![self.path()?];
        while self.peek() == Some(Token::Comma) {
            self.position += 1;
            paths.push(self.path()?);
        }
        Ok(paths)
    }
}

fn operand_type_name(operand: &Operand) -> &'static str {
    match operand {
        Operand::Value(value) => value.value_type().as_str(),
        Operand::Size(_) => "N",
        Operand::Path(_) => "PATH",
    }
}

/// Parses a condition (`ConditionExpression`, `FilterExpression` or `KeyConditionExpression`).
pub fn parse_condition(kind: ExpressionKind, text: &str, placeholders: &mut Placeholders<'_>) -> Result<Condition> {
    let mut parser = Parser::new(kind, text, placeholders)?;
    let condition = parser.condition()?;
    parser.finish()?;
    Ok(condition)
}

/// Parses an `UpdateExpression`.
pub fn parse_update(text: &str, placeholders: &mut Placeholders<'_>) -> Result<UpdateExpression> {
    let mut parser = Parser::new(ExpressionKind::Update, text, placeholders)?;
    let update = parser.update()?;
    parser.finish()?;
    Ok(update)
}

/// Parses a `ProjectionExpression`.
pub fn parse_projection(text: &str, placeholders: &mut Placeholders<'_>) -> Result<Vec<Path>> {
    let mut parser = Parser::new(ExpressionKind::Projection, text, placeholders)?;
    let paths = parser.projection()?;
    parser.finish()?;
    Ok(paths)
}
