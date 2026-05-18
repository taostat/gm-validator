//! ed25519 signature verification for `ValidatorLogRecord` instances.
//!
//! The signing scheme (per `docs/contracts/validator-log-record.md`):
//!
//! - Take the record JSON.
//! - Remove the `signature` field.
//! - Canonicalise the remainder per RFC 8785 (JCS).
//! - Compute SHA-256 of the canonical bytes.
//! - Sign the SHA-256 digest with ed25519. Verify the same way.
//!
//! The signature is stored as base64 (Phase 0 decision Q1 in
//! `docs/contracts.md`).
//!
//! At verification time the caller does not know which of the gateway's
//! pubkeys (per `gateway_keys.json`) signed the record — containers can
//! rotate mid-epoch. We try each registered pubkey for the record's
//! `gateway_id` until one verifies, or return [`VerificationError::BadSignature`].

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use sha2::{Digest, Sha256};

use crate::canonical;
use crate::errors::{VerificationError, VerifierError};
use crate::record::ValidatorLogRecord;

/// Try to verify a record's signature against the supplied ed25519 pubkeys
/// (in the order returned by `gateway_keys.json`). Returns `Ok(())` if any
/// pubkey verifies; otherwise [`VerificationError::BadSignature`].
///
/// # Errors
///
/// Returns [`VerificationError`] variants on malformed input or
/// signature-verification failure under every key.
pub fn verify_signature_with_keys(
    record: &ValidatorLogRecord,
    pubkeys_b64: &[&str],
) -> Result<(), VerificationError> {
    let sig_b64 = record.signature_b64()?;
    let sig_bytes = STANDARD.decode(sig_b64).map_err(VerifierError::from)?;
    if sig_bytes.len() != 64 {
        return Err(VerificationError::InvalidSignatureLength(sig_bytes.len()));
    }
    let sig_array: [u8; 64] = sig_bytes
        .as_slice()
        .try_into()
        .map_err(|_| VerificationError::InvalidSignatureLength(sig_bytes.len()))?;
    let signature = Signature::from_bytes(&sig_array);

    let without_sig = record.without_signature();
    let canon = canonical::canonicalize(&without_sig)?;
    let digest = Sha256::digest(&canon);

    // Gateways rotate keys, so the list can carry several pubkeys for
    // the same gateway_id. A structurally bad entry (bad base64, wrong
    // length, off-curve) must not short-circuit the loop — a later
    // entry may still be the one that signed this record.
    for pubkey_b64 in pubkeys_b64 {
        let Ok(pubkey_bytes) = STANDARD.decode(pubkey_b64) else {
            continue;
        };
        let Ok(pubkey_arr): Result<[u8; 32], _> = pubkey_bytes.as_slice().try_into() else {
            continue;
        };
        let Ok(verifying) = VerifyingKey::from_bytes(&pubkey_arr) else {
            continue;
        };
        if verifying.verify(&digest, &signature).is_ok() {
            return Ok(());
        }
    }
    Err(VerificationError::BadSignature)
}

/// Convenience wrapper around [`verify_signature_with_keys`] for a record
/// whose `gateway_id` is looked up in a `gateway_keys.json` mapping. The
/// `keys_by_gateway` argument is owned by the caller (typically loaded
/// from `gateway_keys.json`).
///
/// # Errors
///
/// Returns [`VerificationError::UnknownGateway`] when the record's
/// `gateway_id` is absent from the map; otherwise the same errors as
/// [`verify_signature_with_keys`].
pub fn verify_record_signature(
    record: &ValidatorLogRecord,
    keys_by_gateway: &std::collections::BTreeMap<String, Vec<String>>,
) -> Result<(), VerificationError> {
    let gateway_id = record.gateway_id()?;
    let keys = keys_by_gateway
        .get(gateway_id)
        .ok_or_else(|| VerificationError::UnknownGateway(gateway_id.to_string()))?;
    let key_refs: Vec<&str> = keys.iter().map(String::as_str).collect();
    verify_signature_with_keys(record, &key_refs)
}

/// Sign a record's canonical-minus-signature payload with a private key.
/// Useful for tests; the real gateway holds its private key inside the
/// TEE and never exposes it.
///
/// # Errors
///
/// Returns [`VerifierError`] on canonicalisation failure.
pub fn sign_record_for_test(
    record: &ValidatorLogRecord,
    signing_key: &ed25519_dalek::SigningKey,
) -> Result<String, VerifierError> {
    use ed25519_dalek::Signer;
    let canon = canonical::canonicalize(&record.without_signature())?;
    let digest = Sha256::digest(&canon);
    let sig: Signature = signing_key.sign(&digest);
    Ok(STANDARD.encode(sig.to_bytes()))
}

#[cfg(test)]
mod tests {
    #![expect(
        clippy::expect_used,
        clippy::default_trait_access,
        reason = "test code"
    )]

    use super::*;
    use crate::record::parse_record;
    use ed25519_dalek::SigningKey;
    use rand_core::OsRng;

    fn make_record(signature_b64: &str) -> ValidatorLogRecord {
        let template = format!(
            r#"{{
                "request_id":"01AAAAAAAAAAAAAAAAAAAAAAAA",
                "gateway_id":"gw-test",
                "miner_id":"m",
                "success":true,
                "signature":"{signature_b64}"
            }}"#
        );
        parse_record(template.as_bytes()).expect("parse")
    }

    #[test]
    fn sign_and_verify_roundtrip() {
        let mut rng = OsRng;
        let signing = SigningKey::generate(&mut rng);
        let verifying = signing.verifying_key();
        let pubkey_b64 = STANDARD.encode(verifying.to_bytes());

        let placeholder = make_record("AAAA");
        let sig_b64 = sign_record_for_test(&placeholder, &signing).expect("sign");

        let signed = make_record(&sig_b64);
        verify_signature_with_keys(&signed, &[&pubkey_b64]).expect("verify");
    }

    #[test]
    fn tampered_record_fails_verification() {
        let mut rng = OsRng;
        let signing = SigningKey::generate(&mut rng);
        let verifying = signing.verifying_key();
        let pubkey_b64 = STANDARD.encode(verifying.to_bytes());

        let placeholder = make_record("AAAA");
        let sig_b64 = sign_record_for_test(&placeholder, &signing).expect("sign");

        let tampered_json = format!(
            r#"{{
                "request_id":"01BBBBBBBBBBBBBBBBBBBBBBBB",
                "gateway_id":"gw-test",
                "miner_id":"m",
                "success":true,
                "signature":"{sig_b64}"
            }}"#
        );
        let tampered = parse_record(tampered_json.as_bytes()).expect("parse");
        let result = verify_signature_with_keys(&tampered, &[&pubkey_b64]);
        assert!(matches!(result, Err(VerificationError::BadSignature)));
    }

    #[test]
    fn second_pubkey_in_list_verifies_after_first_rejects() {
        let mut rng = OsRng;
        let wrong = SigningKey::generate(&mut rng);
        let right = SigningKey::generate(&mut rng);
        let wrong_pub = STANDARD.encode(wrong.verifying_key().to_bytes());
        let right_pub = STANDARD.encode(right.verifying_key().to_bytes());

        let placeholder = make_record("AAAA");
        let sig_b64 = sign_record_for_test(&placeholder, &right).expect("sign");

        let signed = make_record(&sig_b64);
        verify_signature_with_keys(&signed, &[&wrong_pub, &right_pub]).expect("verify");
    }

    #[test]
    fn unknown_gateway_errors() {
        let placeholder = make_record("AAAA");
        let map: std::collections::BTreeMap<String, Vec<String>> = Default::default();
        let result = verify_record_signature(&placeholder, &map);
        assert!(matches!(result, Err(VerificationError::UnknownGateway(_))));
    }
}
