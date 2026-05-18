//! `gm-verifier` CLI entry point.
//!
//! Subcommands:
//!
//! - `verify --epoch <N> --dir <path>` — full-epoch verification of a
//!   local mirror of `s3://gm-validator-logs/v1/finalized/epoch={N}/`.
//!   Recomputes `raw_hash` for every `(miner_id, product)` aggregation
//!   row and verifies a configurable sample of record signatures. Exits
//!   0 on success, non-zero on any failure.
//! - `hash-fixture --file <path>` — read a JSONL fixture of records,
//!   print the canonical `raw_hash`. Used in CI to pin the canonical
//!   construction.
//! - `canonicalize --file <path>` — print the canonical bytes of a
//!   single JSON record. Hex-encoded so binary-safe.

#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::Value;

use gm_verifier::canonical;
use gm_verifier::hash;
use gm_verifier::signature;
use gm_verifier::{parse_record, ValidatorLogRecord, VerificationError};

#[derive(Parser)]
#[command(
    name = "gm-verifier",
    version,
    about = "gm epoch artifact verifier (signature + raw_hash)"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Verify a full epoch against a local mirror of the S3 prefix.
    Verify {
        /// Epoch id to verify.
        #[arg(long)]
        epoch: u64,

        /// Path to the local mirror of `s3://.../finalized/epoch={N}/`.
        /// Must contain `aggregated.jsonl`, `raw.jsonl.zst`,
        /// `gateway_keys.json`, and `_FINALIZED`.
        #[arg(long)]
        dir: PathBuf,

        /// Sample at most N records per `(miner_id, product)` for
        /// signature verification. 0 = signature checks skipped.
        #[arg(long, default_value_t = 16)]
        sample: usize,
    },

    /// Read a JSONL fixture of `ValidatorLogRecord` and print its
    /// canonical `raw_hash`.
    HashFixture {
        /// Path to a JSONL file (uncompressed). Lines may be in any
        /// order; the hash sorts by `request_id` ascending per the
        /// contract.
        #[arg(long)]
        file: PathBuf,
    },

    /// Print the canonical JSON bytes of a single record (hex-encoded).
    Canonicalize {
        /// Path to a single JSON record (one object).
        #[arg(long)]
        file: PathBuf,
    },
}

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Verify { epoch, dir, sample } => run_verify(epoch, &dir, sample),
        Command::HashFixture { file } => run_hash_fixture(&file),
        Command::Canonicalize { file } => run_canonicalize(&file),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            tracing::error!("gm-verifier failed: {err:#}");
            ExitCode::from(1)
        }
    }
}

fn run_hash_fixture(path: &Path) -> Result<()> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let records = parse_jsonl(BufReader::new(file))?;
    let h = hash::raw_hash(&records)?;
    tracing::info!(records = records.len(), raw_hash = %h, "fixture hashed");
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{h}").context("write stdout")?;
    Ok(())
}

fn run_canonicalize(path: &Path) -> Result<()> {
    let mut bytes = Vec::new();
    File::open(path)
        .with_context(|| format!("open {}", path.display()))?
        .read_to_end(&mut bytes)?;
    let value: Value = serde_json::from_slice(&bytes).context("parse record JSON")?;
    let canon = canonical::canonicalize(&value)?;
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{}", hex::encode(&canon)).context("write stdout")?;
    Ok(())
}

fn run_verify(epoch: u64, dir: &Path, sample: usize) -> Result<()> {
    let marker = dir.join("_FINALIZED");
    if !marker.exists() {
        bail!(
            "epoch {epoch}: _FINALIZED marker missing at {}",
            marker.display()
        );
    }

    let aggregated_path = dir.join("aggregated.jsonl");
    let aggregated = load_aggregated(&aggregated_path)
        .with_context(|| format!("load {}", aggregated_path.display()))?;

    let keys_path = dir.join("gateway_keys.json");
    let keys_by_gateway =
        load_gateway_keys(&keys_path).with_context(|| format!("load {}", keys_path.display()))?;

    let raw_path = dir.join("raw.jsonl.zst");
    let raw_records =
        load_raw_zst(&raw_path).with_context(|| format!("load {}", raw_path.display()))?;

    let mut by_tuple: BTreeMap<(String, String, String), Vec<ValidatorLogRecord>> = BTreeMap::new();
    for record in raw_records {
        let miner_id = record.miner_id()?.to_string();
        let (provider, model) = record.product()?;
        let key = (miner_id, provider.to_string(), model.to_string());
        by_tuple.entry(key).or_default().push(record);
    }

    let mut errors: Vec<String> = Vec::new();

    for entry in &aggregated {
        verify_row(entry, &by_tuple, &keys_by_gateway, sample, &mut errors)?;
    }

    tracing::info!(
        epoch = epoch,
        aggregated_rows = aggregated.len(),
        unique_tuples_in_raw = by_tuple.len(),
        sample_per_tuple = sample,
        errors = errors.len(),
        "verification complete"
    );

    if errors.is_empty() {
        Ok(())
    } else {
        for err in &errors {
            tracing::error!("verification error: {err}");
        }
        bail!(
            "epoch {epoch} verification failed with {} errors",
            errors.len()
        )
    }
}

fn verify_row(
    entry: &Value,
    by_tuple: &BTreeMap<(String, String, String), Vec<ValidatorLogRecord>>,
    keys_by_gateway: &BTreeMap<String, Vec<String>>,
    sample: usize,
    errors: &mut Vec<String>,
) -> Result<()> {
    let miner_id = entry
        .get("miner_id")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("aggregated row missing miner_id"))?
        .to_string();
    let product = entry
        .get("product")
        .ok_or_else(|| anyhow!("aggregated row missing product"))?;
    let provider = product
        .get("provider")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("aggregated row missing product.provider"))?
        .to_string();
    let model = product
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("aggregated row missing product.model"))?
        .to_string();
    let expected_hash = entry
        .get("raw_hash")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("aggregated row missing raw_hash"))?
        .to_string();

    let tuple_key = (miner_id.clone(), provider.clone(), model.clone());
    let Some(records) = by_tuple.get(&tuple_key) else {
        // A row in aggregated.jsonl with no backing records in
        // raw.jsonl.zst is a forged or corrupt tuple. Without this
        // check, hashing the empty slice produces the well-known
        // SHA-256 of "", letting any phantom row pass with that
        // hash. Fail loud instead.
        errors.push(format!(
            "aggregated row ({miner_id}, {provider}, {model}) has no \
             backing raw records — phantom tuple, refusing to verify"
        ));
        return Ok(());
    };
    let computed = hash::raw_hash(records)?;
    if computed != expected_hash {
        errors.push(format!(
            "raw_hash mismatch for ({miner_id}, {provider}, {model}): \
             expected={expected_hash} computed={computed}"
        ));
        return Ok(());
    }
    if let Err(why) = check_aggregated_totals(entry, records) {
        errors.push(format!(
            "aggregated totals mismatch for ({miner_id}, {provider}, {model}): {why}"
        ));
        return Ok(());
    }
    if sample > 0 {
        for (idx, record) in records.iter().take(sample).enumerate() {
            match signature::verify_record_signature(record, keys_by_gateway) {
                Ok(()) => {}
                Err(VerificationError::UnknownGateway(g)) => {
                    errors.push(format!(
                        "signature verify: unknown gateway {g} (miner={miner_id}, idx={idx})"
                    ));
                }
                Err(e) => {
                    errors.push(format!(
                        "signature verify failed for ({miner_id}, {provider}, {model}) idx={idx}: {e}"
                    ));
                }
            }
        }
    }
    Ok(())
}

/// Re-derive the aggregated row's request-count fields from the raw
/// records and compare against what `aggregated.jsonl` published.
///
/// Cost fields (`earnings_pdollars`, `surcharge_pdollars`) are left as
/// a follow-up: re-deriving them requires porting the finalizer's
/// pricing-and-modifier logic into Rust, which is meaningful work and
/// is filed separately. Verifying counts here closes the phantom-row
/// class of attack and catches off-by-one bugs in the aggregator.
fn check_aggregated_totals(entry: &Value, records: &[ValidatorLogRecord]) -> Result<()> {
    let claimed = |key: &str| -> Result<u64> {
        let v = entry
            .get(key)
            .ok_or_else(|| anyhow!("aggregated row missing {key}"))?;
        v.as_u64()
            .ok_or_else(|| anyhow!("aggregated row field {key} is not a non-negative integer"))
    };

    let claimed_total = claimed("raw_record_count")?;
    let actual_total = u64::try_from(records.len()).context("record count overflows u64")?;
    if claimed_total != actual_total {
        return Err(anyhow!(
            "raw_record_count claimed={claimed_total} actual={actual_total}"
        ));
    }

    let mut actual_ok: u64 = 0;
    let mut actual_failed: u64 = 0;
    for record in records {
        if record.success()? {
            actual_ok += 1;
        } else {
            actual_failed += 1;
        }
    }

    let claimed_ok = claimed("successful_requests")?;
    if claimed_ok != actual_ok {
        return Err(anyhow!(
            "successful_requests claimed={claimed_ok} actual={actual_ok}"
        ));
    }

    let claimed_failed = claimed("failed_requests")?;
    if claimed_failed != actual_failed {
        return Err(anyhow!(
            "failed_requests claimed={claimed_failed} actual={actual_failed}"
        ));
    }

    Ok(())
}

fn parse_jsonl<R: BufRead>(reader: R) -> Result<Vec<ValidatorLogRecord>> {
    let mut out = Vec::new();
    for (line_no, line) in reader.lines().enumerate() {
        let line = line.with_context(|| format!("read line {line_no}"))?;
        if line.trim().is_empty() {
            continue;
        }
        let record =
            parse_record(line.as_bytes()).with_context(|| format!("parse line {line_no}"))?;
        out.push(record);
    }
    Ok(out)
}

fn load_aggregated(path: &Path) -> Result<Vec<Value>> {
    let file = File::open(path)?;
    let mut out = Vec::new();
    for (line_no, line) in BufReader::new(file).lines().enumerate() {
        let line = line.with_context(|| format!("read aggregated line {line_no}"))?;
        if line.trim().is_empty() {
            continue;
        }
        let value: Value = serde_json::from_str(&line)
            .with_context(|| format!("parse aggregated line {line_no}"))?;
        out.push(value);
    }
    Ok(out)
}

fn load_gateway_keys(path: &Path) -> Result<BTreeMap<String, Vec<String>>> {
    let mut bytes = Vec::new();
    File::open(path)?.read_to_end(&mut bytes)?;
    let value: Value = serde_json::from_slice(&bytes).context("parse gateway_keys.json")?;
    let gateways = value
        .get("gateways")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("gateway_keys.json missing object `gateways`"))?;
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (gateway_id, entries) in gateways {
        let arr = entries
            .as_array()
            .ok_or_else(|| anyhow!("gateways.{gateway_id} is not an array"))?;
        let mut pubkeys = Vec::new();
        for entry in arr {
            let pubkey = entry
                .get("pubkey")
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow!("gateways.{gateway_id} entry missing pubkey"))?;
            pubkeys.push(pubkey.to_string());
        }
        out.insert(gateway_id.clone(), pubkeys);
    }
    Ok(out)
}

fn load_raw_zst(path: &Path) -> Result<Vec<ValidatorLogRecord>> {
    let file = File::open(path)?;
    let decoder = zstd::stream::read::Decoder::new(file)?;
    let reader = BufReader::new(decoder);
    parse_jsonl(reader)
}
