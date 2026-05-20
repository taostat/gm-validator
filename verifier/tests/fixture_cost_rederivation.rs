//! End-to-end coverage for aggregated **cost** re-derivation.
//!
//! `check_aggregated_totals` re-runs the finalizer's pricing math over
//! the raw records and fails the epoch if the published
//! `earnings_pdollars` / `surcharge_pdollars` do not match. These tests
//! drive the `gm-verifier` binary over a fixture epoch directory:
//!
//! - `cost_epoch/aggregated.jsonl` — correct totals; `verify` exits 0.
//! - `cost_epoch/aggregated_tampered.jsonl` — `earnings_pdollars`
//!   inflated from `1_500_000_000_000` to `9_900_000_000_000` while
//!   counts and `raw_hash` stay valid; `verify` must exit non-zero,
//!   proving the cost check (not the count or hash check) caught it.
//!
//! The fixture's three records (`raw.jsonl`, also stored zstd-compressed
//! as `raw.jsonl.zst`) re-derive by hand to:
//!   - record A: input `1_000_000` at $1/Mtok = `1e12`, `batch_bps` 5000
//!     → `5e11`; surcharge count 2 × `1e10` = `2e10`.
//!   - record B: output `500_000` at $2/Mtok = `1e12`; no
//!     modifiers/surcharge.
//!   - record C: `success` false → contributes 0.
//!
//! Totals: earnings = `1_500_000_000_000`, surcharge = `20_000_000_000`.
//!
//! Signature verification is skipped (`--sample 0`): the fixture
//! signatures are placeholders and the cost check runs independently of
//! signature sampling.

#![expect(clippy::expect_used, reason = "integration test fixture")]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn fixture_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/cost_epoch")
}

/// Materialise an epoch directory in `dest`, using `aggregated_name` as
/// the source for the `aggregated.jsonl` artifact.
fn stage_epoch(dest: &Path, aggregated_name: &str) {
    fs::create_dir_all(dest).expect("create epoch dir");
    let src = fixture_dir();
    for artifact in ["raw.jsonl.zst", "gateway_keys.json", "_FINALIZED"] {
        fs::copy(src.join(artifact), dest.join(artifact)).expect("copy artifact");
    }
    fs::copy(src.join(aggregated_name), dest.join("aggregated.jsonl"))
        .expect("copy aggregated artifact");
}

fn run_verify(dir: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_gm-verifier"))
        .args(["verify", "--epoch", "700", "--sample", "0", "--dir"])
        .arg(dir)
        .output()
        .expect("spawn gm-verifier")
}

/// Combined stdout+stderr of the verifier run. The `tracing` subscriber
/// writes diagnostics to stdout; collecting both keeps the assertions
/// robust if that ever changes.
fn combined_logs(output: &std::process::Output) -> String {
    let mut logs = String::from_utf8_lossy(&output.stdout).into_owned();
    logs.push_str(&String::from_utf8_lossy(&output.stderr));
    logs
}

#[test]
fn correct_costs_pass_verification() {
    let tmp = std::env::temp_dir().join("gm_cost_ok");
    let _ = fs::remove_dir_all(&tmp);
    stage_epoch(&tmp, "aggregated.jsonl");

    let output = run_verify(&tmp);
    let logs = combined_logs(&output);
    let _ = fs::remove_dir_all(&tmp);

    assert!(
        output.status.success(),
        "verify should succeed on correct costs; logs:\n{logs}",
    );
}

#[test]
fn tampered_earnings_fail_verification() {
    let tmp = std::env::temp_dir().join("gm_cost_tampered");
    let _ = fs::remove_dir_all(&tmp);
    stage_epoch(&tmp, "aggregated_tampered.jsonl");

    let output = run_verify(&tmp);
    let logs = combined_logs(&output);
    let _ = fs::remove_dir_all(&tmp);

    assert!(
        !output.status.success(),
        "verify must fail when earnings_pdollars is tampered; logs:\n{logs}",
    );
    assert!(
        logs.contains("earnings_pdollars"),
        "failure should name the earnings_pdollars mismatch; logs:\n{logs}",
    );
    assert!(
        logs.contains("9900000000000") && logs.contains("1500000000000"),
        "failure should report claimed vs re-derived; logs:\n{logs}",
    );
}
