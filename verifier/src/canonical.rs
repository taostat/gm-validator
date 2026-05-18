//! RFC 8785 JSON Canonicalization Scheme (JCS) for `ValidatorLogRecord`.
//!
//! We do not need a fully general JCS implementation. The validator log
//! schema is fixed and the values inside records are all of types JCS
//! handles unambiguously:
//!
//! - strings (always JCS-escaped via `serde_json`'s default string escaping,
//!   which matches JCS for our charset since records only carry ASCII +
//!   timestamps + base64)
//! - integers (no fractional component, no scientific notation, decimal)
//! - booleans, null
//! - arrays
//! - objects (keys sorted lexicographically by code-unit, no insignificant
//!   whitespace)
//!
//! We implement JCS by walking a `serde_json::Value` and writing a
//! deterministic byte stream.
//!
//! Fixture: `tests/fixtures/raw_hash/` pins canonical output of a sample
//! `ValidatorLogRecord` to a known SHA-256.

use serde_json::Value;

use crate::errors::VerifierError;

/// Serialise a `serde_json::Value` to its RFC 8785 JCS canonical form as
/// UTF-8 bytes.
///
/// # Errors
///
/// Returns [`VerifierError::Canonical`] when the value contains a JSON
/// `Number` that is not an integer (we do not allow floats in the
/// validator log; their canonical representation is the wart of JCS).
pub fn canonicalize(value: &Value) -> Result<Vec<u8>, VerifierError> {
    let mut out = Vec::with_capacity(256);
    write_value(value, &mut out)?;
    Ok(out)
}

/// Convenience: canonicalise + parse from raw JSON bytes.
///
/// # Errors
///
/// Returns [`VerifierError::Json`] on parse failure,
/// [`VerifierError::Canonical`] on canonicalisation failure.
pub fn canonicalize_bytes(json_bytes: &[u8]) -> Result<Vec<u8>, VerifierError> {
    let value: Value = serde_json::from_slice(json_bytes)?;
    canonicalize(&value)
}

fn write_value(value: &Value, out: &mut Vec<u8>) -> Result<(), VerifierError> {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(b) => out.extend_from_slice(if *b { b"true" } else { b"false" }),
        Value::Number(n) => write_number(n, out)?,
        Value::String(s) => write_string(s, out),
        Value::Array(arr) => write_array(arr, out)?,
        Value::Object(map) => write_object(map, out)?,
    }
    Ok(())
}

fn write_number(n: &serde_json::Number, out: &mut Vec<u8>) -> Result<(), VerifierError> {
    if let Some(u) = n.as_u64() {
        out.extend_from_slice(u.to_string().as_bytes());
        Ok(())
    } else if let Some(i) = n.as_i64() {
        out.extend_from_slice(i.to_string().as_bytes());
        Ok(())
    } else {
        Err(VerifierError::Canonical(
            "non-integer number in canonical input; floats are forbidden in \
             the validator log schema"
                .into(),
        ))
    }
}

fn write_string(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\u{0008}' => out.extend_from_slice(b"\\b"),
            '\u{000C}' => out.extend_from_slice(b"\\f"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            c if (c as u32) < 0x20 => {
                let escaped = format!("\\u{:04x}", c as u32);
                out.extend_from_slice(escaped.as_bytes());
            }
            c => {
                let mut buf = [0u8; 4];
                let encoded = c.encode_utf8(&mut buf);
                out.extend_from_slice(encoded.as_bytes());
            }
        }
    }
    out.push(b'"');
}

fn write_array(arr: &[Value], out: &mut Vec<u8>) -> Result<(), VerifierError> {
    out.push(b'[');
    for (idx, item) in arr.iter().enumerate() {
        if idx > 0 {
            out.push(b',');
        }
        write_value(item, out)?;
    }
    out.push(b']');
    Ok(())
}

fn write_object(
    map: &serde_json::Map<String, Value>,
    out: &mut Vec<u8>,
) -> Result<(), VerifierError> {
    let mut keys: Vec<&String> = map.keys().collect();
    keys.sort_by(|a, b| compare_utf16(a, b));

    out.push(b'{');
    for (idx, key) in keys.iter().enumerate() {
        if idx > 0 {
            out.push(b',');
        }
        write_string(key, out);
        out.push(b':');
        let value = map
            .get(*key)
            .ok_or_else(|| VerifierError::Canonical(format!("missing key in map: {key}")))?;
        write_value(value, out)?;
    }
    out.push(b'}');
    Ok(())
}

fn compare_utf16(a: &str, b: &str) -> std::cmp::Ordering {
    let mut iter_a = a.encode_utf16();
    let mut iter_b = b.encode_utf16();
    loop {
        match (iter_a.next(), iter_b.next()) {
            (None, None) => return std::cmp::Ordering::Equal,
            (None, _) => return std::cmp::Ordering::Less,
            (_, None) => return std::cmp::Ordering::Greater,
            (Some(x), Some(y)) => match x.cmp(&y) {
                std::cmp::Ordering::Equal => {}
                ord => return ord,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    #![expect(
        clippy::expect_used,
        clippy::needless_raw_string_hashes,
        reason = "test code may panic on bad fixtures and use literal JSON"
    )]

    use super::*;

    #[test]
    fn empty_object() {
        let v: Value = serde_json::from_str("{}").expect("parse");
        assert_eq!(canonicalize(&v).expect("canon"), b"{}");
    }

    #[test]
    fn sorts_keys_lexicographically() {
        let v: Value = serde_json::from_str(r#"{"b": 1, "a": 2}"#).expect("parse");
        assert_eq!(canonicalize(&v).expect("canon"), b"{\"a\":2,\"b\":1}");
    }

    #[test]
    fn nested_sorting() {
        let v: Value = serde_json::from_str(r#"{"z": {"y": 1, "x": 2}, "a": 3}"#).expect("parse");
        let canon = canonicalize(&v).expect("canon");
        assert_eq!(canon, b"{\"a\":3,\"z\":{\"x\":2,\"y\":1}}");
    }

    #[test]
    fn array_preserves_order() {
        let v: Value = serde_json::from_str(r#"[3, 1, 2]"#).expect("parse");
        assert_eq!(canonicalize(&v).expect("canon"), b"[3,1,2]");
    }

    #[test]
    fn string_escapes() {
        let v: Value = serde_json::from_str(r#"{"k": "a\nb\"c\\d\te"}"#).expect("parse");
        assert_eq!(
            canonicalize(&v).expect("canon"),
            b"{\"k\":\"a\\nb\\\"c\\\\d\\te\"}"
        );
    }

    #[test]
    fn control_char_uses_unicode_escape() {
        // U+0001 supplied via the JSON `\uXXXX` escape; serde_json refuses
        // raw control bytes inside strings.
        let v: Value = serde_json::from_str("{\"k\": \"\\u0001\"}").expect("parse");
        assert_eq!(canonicalize(&v).expect("canon"), b"{\"k\":\"\\u0001\"}");
    }

    #[test]
    fn integers_pass_through() {
        let v: Value = serde_json::from_str(r#"{"n": 1234567890123456}"#).expect("parse");
        assert_eq!(
            canonicalize(&v).expect("canon"),
            b"{\"n\":1234567890123456}"
        );
    }

    #[test]
    fn floats_are_rejected() {
        let v: Value = serde_json::from_str(r#"{"n": 1.5}"#).expect("parse");
        let result = canonicalize(&v);
        assert!(matches!(result, Err(VerifierError::Canonical(_))));
    }

    #[test]
    fn utf16_sort_matches_python_sort_keys() {
        // Python's json.dumps(sort_keys=True) sorts by UTF-16 code unit;
        // for our typical ASCII keys this is byte-order, but pin the
        // property with a non-ASCII key to be safe.
        let v: Value = serde_json::from_str(r#"{"é": 1, "a": 2}"#).expect("parse");
        let canon = canonicalize(&v).expect("canon");
        // 'a' (0x61) sorts before 'é' (0xE9) in both UTF-16 and ASCII.
        assert_eq!(canon, b"{\"a\":2,\"\xc3\xa9\":1}");
    }
}
