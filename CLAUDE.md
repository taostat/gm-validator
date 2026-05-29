# gm-validator

The validator is the on-chain weight-setter for the gm subnet. It watches S3
for epoch artifacts produced by the epoch-finalizer, re-verifies a random
sample of signed `ValidatorLogRecord` entries, re-derives each miner's
`raw_hash` from the raw JSONL, loads `aggregated.jsonl`, computes per-miner
earnings scores, normalizes them to weights, and calls
`subtensor.set_weights()`. It has two components:

- **`validator/`** — Python service: S3 polling, score computation, Bittensor weight submission
- **`verifier/`** — Rust library + CLI: canonical `raw_hash` construction and ed25519 signature verification

## Layout

### validator/ (Python)

- `src/gm_validator/main.py` — entry point; wires S3 mirror, submitter, miner-uid lookup
- `src/gm_validator/validator.py` — `Validator.process_once()`: discover finalized epochs, mirror artifacts locally, run verifier subprocess, score, submit
- `src/gm_validator/s3_mirror.py` — `S3Mirror`: syncs S3 epoch artifacts to a local directory; prunes old epochs
- `src/gm_validator/scoring.py` — `score()` + `compute_weights()`: per-miner totals plus the legacy or emission-cap weight path; emits a u16 vector summing to `MAX_WEIGHT`
- `src/gm_validator/alpha_economics.py` — `compute_epoch_weights()` (cap + scale) + `normalize_weights()` (float→u16, burn slot absorbs floor-rounding dust); ported from bm-validator
- `src/gm_validator/epoch_summary.py` — Pydantic model + S3 reader for the finalizer's per-epoch `epoch_summary.json` (alpha USD price snapshot)
- `src/gm_validator/verifier.py` — subprocess wrapper for the `gm-verifier` binary
- `src/gm_validator/bittensor_adapter.py` — `Submitter` protocol; `MockSubmitter` for testing
- `src/gm_validator/bittensor_real.py` — `RealSubmitter`: lazily-imported to avoid loading bittensor-py in tests
- `src/gm_validator/metagraph.py` — lazy hotkey→uid lookup from the subnet metagraph
- `src/gm_validator/config.py` — `ValidatorConfig.from_env()`
- `src/gm_validator/processed_state.py` — persisted set of already-processed epoch ids (JSON file)

### verifier/ (Rust)

- `src/lib.rs` — public API; re-exports `raw_hash`, `verify_record_signature`, `parse_record`, etc.
- `src/hash.rs` — `raw_hash()`: JCS-serialize records, sort by `request_id`, SHA-256, lowercase hex
- `src/canonical.rs` — RFC 8785 canonical JSON serialization (sorted keys, no insignificant whitespace)
- `src/signature.rs` — ed25519 signature verification; tries each registered gateway pubkey
- `src/record.rs` — `ValidatorLogRecord` deserialization
- `src/cost.rs` — `compute_record_cost()`: derives earnings from token counts and price block
- `src/main.rs` — `gm-verifier` CLI binary for operator/auditor use

## Build / lint / test

### validator

```bash
cd validator
uv sync --group dev

uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src
uv run pytest -q

# run (requires S3_BUCKET, REGISTRY_URL, BITTENSOR_* set or BITTENSOR_MOCK=1)
uv run python -m gm_validator.main
```

### verifier

```bash
cd verifier
cargo clippy --all-targets --all-features -- -D warnings
cargo fmt --check
cargo test

# build the CLI binary
cargo build --release -p gm-verifier
```

## Key env vars (validator)

| Variable | Default | Purpose |
|---|---|---|
| `S3_BUCKET` | required | Bucket containing finalized epoch artifacts |
| `S3_PREFIX` | `v1` | Key prefix |
| `S3_ENDPOINT_URL` | — | Override (MinIO for local dev) |
| `GM_VALIDATOR_S3_ANONYMOUS` | `0` | Skip request signing (OVH public-read buckets) |
| `LOCAL_MIRROR_DIR` | `/var/cache/gm-validator` | Local mirror for verifier subprocess |
| `MIRROR_RETENTION_EPOCHS` | `10` | How many recent epoch mirrors to keep on disk |
| `PROCESSED_STATE_PATH` | `<mirror_dir>/processed.json` | Crash-safe record of submitted epochs |
| `BITTENSOR_NETUID` | `0` | Subnet UID |
| `BITTENSOR_WALLET_NAME` / `BITTENSOR_WALLET_HOTKEY` | — | Required for real weight submission |
| `BITTENSOR_MOCK` | `0` | Use `MockSubmitter` (records submissions in memory) |
| `GM_VERIFIER_BIN` | `gm-verifier` | Path to the verifier binary |
| `VERIFIER_SAMPLE_PER_TUPLE` | `16` | Number of records sampled per `(miner, product)` tuple |
| `USE_EMISSION_CAP` | `0` | When true, apply the bm-style cap+burn from `epoch_summary.json`. Falls back to the naive normalisation when the summary artifact is absent (legacy epochs). |
| `ALPHA_EMISSION_PER_EPOCH` | `100` | Full-epoch alpha emission, used only by the cap path. Static knob until a follow-up pulls it from chain. |
| `SUBNET_OWNER_UID` | `0` | Uid that absorbs the burn slot + floor-rounding dust under the cap path. Static knob until a follow-up resolves it from `SubnetOwnerHotkey`. |

## Key conventions

- The `raw_hash` algorithm is byte-for-byte identical between the epoch-finalizer (Python, calling the verifier binary) and this validator (calling the same binary). Any drift fails the verification sample and triggers an alert.
- `bittensor` is lazily imported to keep the test path independent of the bittensor-py wheel.
- Processed epoch state is persisted to disk so a validator restart does not re-submit weights for epochs already finalized on-chain.
- Scoring uses nano-dollar-precision integer arithmetic throughout. Weights are the only floating-point values, derived only after all integer sums are complete.
- Supply-chain: `deny.toml` governs the Rust workspace (`cargo deny check`).
