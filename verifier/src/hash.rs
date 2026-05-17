//! `raw_hash` construction per `docs/contracts/epoch-artifacts.md`.
//!
//! Step by step (must match this exactly; producer and consumer):
//!
//! 1. The caller pre-filters raw records to the `(miner_id, product)`
//!    tuple. This module operates on the resulting slice.
//! 2. Sort by `request_id` ascending (lexicographic byte order on the
//!    ULID, which is Crockford base32 — equivalent to lexicographic
//!    on the unicode codepoints for ASCII).
//! 3. Serialise each record to canonical JSON per RFC 8785 (JCS).
//! 4. Join consecutive canonical records with `\n` (LF, 0x0A). **No
//!    trailing newline.**
//! 5. SHA-256 of the joined bytes.
//! 6. Lower-case hex.

use sha2::{Digest, Sha256};

use crate::canonical;
use crate::errors::VerifierError;
use crate::record::ValidatorLogRecord;

/// Compute the canonical `raw_hash` over a slice of records.
///
/// The slice does **not** need to be pre-sorted; this function sorts a
/// copy by `request_id` ascending before hashing.
///
/// # Errors
///
/// Returns [`VerifierError::MalformedRecord`] if any record lacks a
/// `request_id`, or [`VerifierError::Canonical`] on canonicalisation
/// failure.
pub fn raw_hash(records: &[ValidatorLogRecord]) -> Result<String, VerifierError> {
    let mut indexed: Vec<(&str, &ValidatorLogRecord)> = records
        .iter()
        .map(|r| Ok::<_, VerifierError>((r.request_id()?, r)))
        .collect::<Result<_, _>>()?;
    indexed.sort_by(|(a, _), (b, _)| a.cmp(b));

    let mut hasher = Sha256::new();
    for (idx, (_rid, record)) in indexed.iter().enumerate() {
        if idx > 0 {
            hasher.update(b"\n");
        }
        let canon = canonical::canonicalize(&record.value)?;
        hasher.update(&canon);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Like [`raw_hash`] but takes a vector of already-canonicalised record
/// byte buffers, pre-sorted by `request_id`. Useful in tests and for the
/// fixture pinning.
///
/// # Errors
///
/// None — the inputs are assumed valid.
#[must_use]
pub fn raw_hash_lines(canonical_lines: &[Vec<u8>]) -> String {
    let mut hasher = Sha256::new();
    for (idx, line) in canonical_lines.iter().enumerate() {
        if idx > 0 {
            hasher.update(b"\n");
        }
        hasher.update(line);
    }
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    #![expect(clippy::expect_used, reason = "test code")]

    use super::*;
    use crate::record::parse_record;

    #[test]
    fn empty_input_hashes_empty_string() {
        let result = raw_hash(&[]).expect("hash");
        // sha256 of empty input
        assert_eq!(
            result,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn deterministic_under_input_reorder() {
        let r1 = parse_record(
            br#"{"request_id":"01AAAAAAAAAAAAAAAAAAAAAAAA","miner_id":"m","gateway_id":"g","signature":"x"}"#,
        )
        .expect("parse");
        let r2 = parse_record(
            br#"{"request_id":"01BBBBBBBBBBBBBBBBBBBBBBBB","miner_id":"m","gateway_id":"g","signature":"y"}"#,
        )
        .expect("parse");
        let h1 = raw_hash(&[r1.clone(), r2.clone()]).expect("h1");
        let h2 = raw_hash(&[r2, r1]).expect("h2");
        assert_eq!(h1, h2);
    }

    #[test]
    fn single_record_lines_matches_raw_hash() {
        let r = parse_record(
            br#"{"request_id":"01AAAAAAAAAAAAAAAAAAAAAAAA","miner_id":"m","gateway_id":"g","signature":"x"}"#,
        )
        .expect("parse");
        let h1 = raw_hash(std::slice::from_ref(&r)).expect("h1");
        let canon = canonical::canonicalize(&r.value).expect("canon");
        let h2 = raw_hash_lines(&[canon]);
        assert_eq!(h1, h2);
    }

    #[test]
    fn lf_separator_between_records() {
        let r1 = parse_record(br#"{"request_id":"01A"}"#).expect("parse");
        let r2 = parse_record(br#"{"request_id":"01B"}"#).expect("parse");
        let h_pair = raw_hash(&[r1.clone(), r2.clone()]).expect("pair");

        // Manually: canonicalize each, join with single LF, SHA-256.
        let c1 = canonical::canonicalize(&r1.value).expect("c1");
        let c2 = canonical::canonicalize(&r2.value).expect("c2");
        let mut joined = c1.clone();
        joined.push(b'\n');
        joined.extend_from_slice(&c2);
        let expected = hex::encode(Sha256::digest(&joined));

        assert_eq!(h_pair, expected);
    }
}
