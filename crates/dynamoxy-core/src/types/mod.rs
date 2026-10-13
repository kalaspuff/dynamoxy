//! Inputs and outputs of every operation, and the table schema types they use.
//!
//! These are dynamoxy's own types: the backends convert them to and from their own
//! representations (`backend::aws` to SDK types, `backend::memory` to its storage).

mod ops;
mod schema;

pub use ops::*;
pub use schema::*;
