//! Minimal `ValidatorLogRecord` accessors.
//!
//! We deliberately avoid a strongly-typed Rust struct mirror of the schema
//! here. The verifier's job is to operate on JSON bytes byte-for-byte; a
//! struct mirror would risk silently coercing missing optional fields or
//! reordering on re-serialisation. Instead we keep records as
//! `serde_json::Value` and pull out only the fields we need.

use serde_json::Value;

use crate::errors::VerifierError;

/// A parsed validator log record kept as raw JSON `Value` plus a few
/// commonly-needed accessors.
#[derive(Debug, Clone)]
pub struct ValidatorLogRecord {
    /// The full parsed JSON value (used for canonical hashing).
    pub value: Value,
}

impl ValidatorLogRecord {
    /// The `request_id` field (ULID, 26 chars Crockford base32).
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] if the field is missing
    /// or not a string.
    pub fn request_id(&self) -> Result<&str, VerifierError> {
        self.value
            .get("request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing string `request_id`".into()))
    }

    /// The `miner_id` field (SS58 hotkey).
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] if the field is missing
    /// or not a string.
    pub fn miner_id(&self) -> Result<&str, VerifierError> {
        self.value
            .get("miner_id")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing string `miner_id`".into()))
    }

    /// The `gateway_id` field.
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] if the field is missing
    /// or not a string.
    pub fn gateway_id(&self) -> Result<&str, VerifierError> {
        self.value
            .get("gateway_id")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing string `gateway_id`".into()))
    }

    /// `(provider, model)` tuple from the `product` field.
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] when the field is
    /// missing or malformed.
    pub fn product(&self) -> Result<(&str, &str), VerifierError> {
        let product = self
            .value
            .get("product")
            .ok_or_else(|| VerifierError::MalformedRecord("missing object `product`".into()))?;
        let provider = product
            .get("provider")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing `product.provider`".into()))?;
        let model = product
            .get("model")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing `product.model`".into()))?;
        Ok((provider, model))
    }

    /// `success` field.
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] when the field is
    /// missing or not a boolean.
    pub fn success(&self) -> Result<bool, VerifierError> {
        self.value
            .get("success")
            .and_then(Value::as_bool)
            .ok_or_else(|| VerifierError::MalformedRecord("missing boolean `success`".into()))
    }

    /// `signature` field (base64).
    ///
    /// # Errors
    ///
    /// Returns [`VerifierError::MalformedRecord`] when the field is
    /// missing or not a string.
    pub fn signature_b64(&self) -> Result<&str, VerifierError> {
        self.value
            .get("signature")
            .and_then(Value::as_str)
            .ok_or_else(|| VerifierError::MalformedRecord("missing string `signature`".into()))
    }

    /// A clone of the record with the `signature` field removed. Used as
    /// the input to canonical-JSON serialisation for signature
    /// verification.
    #[must_use]
    pub fn without_signature(&self) -> Value {
        let mut value = self.value.clone();
        if let Some(map) = value.as_object_mut() {
            map.remove("signature");
        }
        value
    }
}

/// Parse a single JSONL line into a `ValidatorLogRecord`.
///
/// # Errors
///
/// Returns [`VerifierError::Json`] on JSON parse failure.
pub fn parse_record(json: &[u8]) -> Result<ValidatorLogRecord, VerifierError> {
    let value: Value = serde_json::from_slice(json)?;
    Ok(ValidatorLogRecord { value })
}

#[cfg(test)]
mod tests {
    #![expect(clippy::expect_used, reason = "test code")]

    use super::*;

    fn sample_record() -> &'static str {
        r#"{
            "schema_version": "1",
            "request_id": "01JZK4F2P3R5W7XY8Z9TBQM6V0",
            "timestamp": "2026-05-17T18:34:21.451Z",
            "epoch_id": 142,
            "gateway_id": "gw-prod-1",
            "miner_id": "5EhmFv4P1qg6yQ3xPzGn7sJ8a1KdR2bU9N4eC0wY5T6dQJ8",
            "product": { "provider": "anthropic", "model": "claude-sonnet-4-6" },
            "miner_price": {
                "price_id": "mp-v1-142-0017",
                "dimensions": {
                    "input_per_mtok_ndollars": 2800000,
                    "output_per_mtok_ndollars": 14000000
                }
            },
            "usage": { "input_tokens": 812, "output_tokens": 1456 },
            "modifiers": {},
            "surcharges": {},
            "success": true,
            "signature": "AAAA"
        }"#
    }

    #[test]
    fn parses_and_extracts_fields() {
        let rec = parse_record(sample_record().as_bytes()).expect("parse");
        assert_eq!(rec.request_id().expect("rid"), "01JZK4F2P3R5W7XY8Z9TBQM6V0");
        assert_eq!(rec.gateway_id().expect("gw"), "gw-prod-1");
        assert_eq!(
            rec.miner_id().expect("m"),
            "5EhmFv4P1qg6yQ3xPzGn7sJ8a1KdR2bU9N4eC0wY5T6dQJ8"
        );
        assert_eq!(
            rec.product().expect("p"),
            ("anthropic", "claude-sonnet-4-6")
        );
        assert!(rec.success().expect("s"));
        assert_eq!(rec.signature_b64().expect("sig"), "AAAA");
    }

    #[test]
    fn without_signature_strips_the_field() {
        let rec = parse_record(sample_record().as_bytes()).expect("parse");
        let stripped = rec.without_signature();
        assert!(stripped.get("signature").is_none());
        assert!(stripped.get("request_id").is_some());
    }

    #[test]
    fn missing_request_id_errors() {
        let rec = parse_record(br#"{"foo": "bar"}"#).expect("parse");
        assert!(matches!(
            rec.request_id(),
            Err(VerifierError::MalformedRecord(_))
        ));
    }
}
