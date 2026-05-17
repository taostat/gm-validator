//! Error types for the verifier crate.

use thiserror::Error;

/// Errors raised by the canonical-JSON and hash code paths.
#[derive(Debug, Error)]
pub enum VerifierError {
    /// The JSON payload could not be parsed.
    #[error("JSON parse error: {0}")]
    Json(#[from] serde_json::Error),

    /// The value could not be canonicalised (e.g. contains a float).
    #[error("canonicalisation error: {0}")]
    Canonical(String),

    /// I/O error reading a fixture or artifact.
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// The record lacked a required field (`request_id`, `gateway_id`,
    /// `signature`, etc.).
    #[error("malformed validator log record: {0}")]
    MalformedRecord(String),

    /// base64 decoding failure.
    #[error("base64 decode error: {0}")]
    Base64(#[from] base64::DecodeError),

    /// hex decoding failure.
    #[error("hex decode error: {0}")]
    Hex(#[from] hex::FromHexError),
}

/// Errors raised by signature verification specifically. Kept separate so
/// callers can distinguish "the signature was bad" from "the input was
/// malformed."
#[derive(Debug, Error)]
pub enum VerificationError {
    /// Pre-flight: the record's structure was invalid.
    #[error(transparent)]
    Verifier(#[from] VerifierError),

    /// The ed25519 signature did not verify under any registered pubkey
    /// for the record's `gateway_id`.
    #[error("ed25519 signature did not verify under any registered pubkey")]
    BadSignature,

    /// The record's `gateway_id` is unknown in `gateway_keys.json`.
    #[error("unknown gateway_id: {0}")]
    UnknownGateway(String),

    /// The signature blob was not a 64-byte ed25519 signature.
    #[error("invalid signature length: {0} bytes")]
    InvalidSignatureLength(usize),

    /// A pubkey blob was not 32 bytes.
    #[error("invalid pubkey length: {0} bytes")]
    InvalidPubkeyLength(usize),
}
