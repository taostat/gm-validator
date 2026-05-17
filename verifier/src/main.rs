//! gm-verifier CLI - Phase 0 scaffold.
//!
//! Phase 1 W6 implements `gm-verifier verify --epoch <n>` which pulls
//! `aggregated.jsonl`, `raw.jsonl.zst`, and `gateway_keys.json` from
//! S3 and fully re-verifies the epoch. Same logic backs the validator
//! service's hot path.
//! See `taostat/gm/docs/contracts/epoch-artifacts.md`.

#![forbid(unsafe_code)]

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "gm-verifier", version, about = "gm epoch verifier")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Placeholder for `gm-verifier verify --epoch N` (Phase 1 W6).
    Version,
}

fn main() {
    tracing_subscriber::fmt().with_env_filter("info").init();
    let cli = Cli::parse();
    match cli.command {
        Command::Version => {
            tracing::info!(
                "phase 0 gm-verifier scaffold v{}; epoch verification lands in W6",
                env!("CARGO_PKG_VERSION")
            );
        }
    }
}
