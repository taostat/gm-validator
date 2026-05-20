//! gm-verifier — canonical verification logic for gm epoch artifacts.
//!
//! This crate is the single source of truth for two byte-sensitive operations
//! that the Epoch Finalizer (producer in `taostat/gm`) and the Validator
//! (consumer in `taostat/gm-validator`) MUST agree on byte-for-byte:
//!
//! 1. The `raw_hash` construction for each `(miner_id, product)` aggregation
//!    entry, per `docs/contracts/epoch-artifacts.md`:
//!
//!    - Filter raw records to the tuple
//!    - Sort by `request_id` ascending (lexicographic on ULID)
//!    - Serialise each record as canonical JSON (RFC 8785 JCS): sorted keys,
//!      no insignificant whitespace, UTF-8
//!    - Join consecutive canonical records with a single LF (no trailing
//!      newline)
//!    - SHA-256
//!    - Lower-case hex
//!
//! 2. The ed25519 signature verification of a `ValidatorLogRecord`: signature
//!    is over the canonical JSON of the record with the `signature` field
//!    removed; the gateway pubkey comes from `gateway_keys.json` (try each
//!    pubkey registered for the `gateway_id` during the epoch).
//!
//! The Python Finalizer calls into this crate via the `gm-verifier` binary
//! (or via `PyO3` in the future); the Python Validator calls the same binary
//! (or library) for sample verification. CI pins a fixture under
//! `tests/fixtures/raw_hash/` so any drift in either implementation fails
//! loudly.

#![forbid(unsafe_code)]
#![deny(missing_docs)]

pub mod canonical;
pub mod cost;
pub mod errors;
pub mod hash;
pub mod record;
pub mod signature;

pub use cost::{compute_record_cost, RecordCost};
pub use errors::{VerificationError, VerifierError};
pub use hash::{raw_hash, raw_hash_lines};
pub use record::{parse_record, ValidatorLogRecord};
pub use signature::{verify_record_signature, verify_signature_with_keys};
