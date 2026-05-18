//! Pins the canonical `raw_hash` construction.
//!
//! The companion Python Finalizer (in `taostat/gm/epoch-finalizer/`) reads
//! the same fixture and asserts the same hex hash. Any drift fails both
//! sides loudly.

#![expect(clippy::expect_used, reason = "integration test fixture")]

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

use gm_verifier::{hash, parse_record};

fn fixture_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/raw_hash")
}

#[test]
fn pinned_raw_hash_matches_expected() {
    let input_path = fixture_root().join("input.jsonl");
    let file = File::open(&input_path).expect("open fixture input.jsonl");

    let mut records = Vec::new();
    for line in BufReader::new(file).lines() {
        let line = line.expect("read line");
        if line.trim().is_empty() {
            continue;
        }
        let record = parse_record(line.as_bytes()).expect("parse record");
        records.push(record);
    }

    let computed = hash::raw_hash(&records).expect("compute raw_hash");

    let expected_path = fixture_root().join("expected.txt");
    let expected_raw = std::fs::read_to_string(&expected_path).expect("read expected.txt");
    let expected = expected_raw.trim();

    assert_eq!(
        computed,
        expected,
        "canonical raw_hash drift detected; if intentional, regenerate \
         expected.txt via:\n  cargo run -p gm-verifier --bin gm-verifier -- \
         hash-fixture --file {}",
        input_path.display()
    );
}
