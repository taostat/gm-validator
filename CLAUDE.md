# gm-validator

The validator is the on-chain weight-setter for the gm subnet. It watches S3
for epoch artifacts produced by the gm-operated epoch-finalizer, loads the
per-`(miner, product)` cost-derived rows out of `aggregated.jsonl`, computes
per-miner earnings scores, normalises them to a u16 weight vector via the
cap+burn pipeline, and calls `subtensor.set_weights()`.

The validator does not re-derive cost or re-verify `raw_hash` /
signatures. Validators are operated by external parties — rolling
out pricing-math changes through them is expensive, so the
gm-operated finalizer is the single source of truth and the
artifact set is treated as authoritative.

## Layout

### validator/ (Python)

- `src/gm_validator/main.py` — entry point; wires S3 mirror, submitter, miner-uid lookup
- `src/gm_validator/validator.py` — `Validator.process_once()`: discover finalized epochs, mirror artifacts locally, score, submit
- `src/gm_validator/s3_mirror.py` — `S3Mirror`: syncs S3 epoch artifacts to a local directory; prunes old epochs
- `src/gm_validator/scoring.py` — `score()` + `compute_weights()`: per-miner totals → u16 vector summing to `MAX_WEIGHT`; cap+burn pipeline only
- `src/gm_validator/alpha_economics.py` — `compute_epoch_weights()` (per-miner `consumed_usd / pool_usd`) + `normalize_weights()` (float→u16, renorms when sum > 1, burn slot absorbs the residue when sum < 1); ported from bm-validator
- `src/gm_validator/epoch_summary.py` — Pydantic model + local reader for the mirrored per-epoch `epoch_summary.json` (alpha USD price + emission snapshot)
- `src/gm_validator/bittensor_adapter.py` — `Submitter` + `ChainCursor` protocols; `MockSubmitter`/`MockChainCursor` for testing
- `src/gm_validator/bittensor_real.py` — `RealSubmitter`: lazily-imported to avoid loading bittensor-py in tests; holds the one long-lived socket and serves the startup hotkey→uid lookup over it
- `src/gm_validator/subtensor_connect.py` — `connect_subtensor`: retrying, timeout-bounded subtensor construction (a hung connect becomes a retryable `TimeoutError`)
- `src/gm_validator/metrics.py` — Prometheus gauges tracking the last successful weight submission (epoch id + timestamp)
- `src/gm_validator/config.py` — `ValidatorConfig.from_env()`

## Build / lint / test

```bash
cd validator
uv sync --group dev

uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src
uv run pytest -q

# run (requires S3_BUCKET and SUBNET_OWNER_UID; plus a nonblank
# BITTENSOR_HOTKEY_SEED unless BITTENSOR_MOCK=1)
uv run python -m gm_validator.main
```

## Key env vars (validator)

| Variable | Default | Purpose |
|---|---|---|
| `S3_BUCKET` | required | Bucket containing finalized epoch artifacts |
| `S3_PREFIX` | `v1` | Key prefix |
| `S3_ENDPOINT_URL` | — | Override (MinIO for local dev) |
| `AWS_REGION` | `us-east-1` | Region for the S3 client |
| `GM_VALIDATOR_S3_ANONYMOUS` | `0` | Skip request signing (public-read buckets) |
| `LOCAL_MIRROR_DIR` | `/var/cache/gm-validator` | Local audit cache of finalized artifacts |
| `MIRROR_RETENTION_EPOCHS` | `10` | How many recent epoch mirrors to keep on disk |
| `BLOCKS_PER_EPOCH` | `361` | Epoch length (`tempo + 1`); the chain head divided by this gives the open epoch id. **Must equal the finalizer/registry divisor** or the `finalized/epoch=<N>/` S3 paths desync silently |
| `FINALIZED_LOOKBACK_EPOCHS` | `3` | How many epochs back to probe for a `_FINALIZED` marker before giving up for the tick (tolerates finalizer lag) |
| `BITTENSOR_NETUID` | `0` | Subnet UID |
| `BITTENSOR_ENDPOINT` | — | Subtensor `wss://` URL (SDK default network when unset) |
| `BITTENSOR_HOTKEY_SEED` | — | Validator hotkey seed — a BIP-39 mnemonic or `0x`-prefixed hex seed. The signing keypair is built in memory; no keyfile on disk. Required for real weight submission |
| `BITTENSOR_MOCK` | `0` | Use `MockSubmitter` (records submissions in memory) |
| `SUBTENSOR_CONNECT_TIMEOUT_SECS` | `30` | Wall-clock budget for one subtensor connect attempt; a hang past this becomes a retryable `TimeoutError` |
| `SUBTENSOR_RPC_TIMEOUT_SECS` | `30` | Per-RPC timeout for chain-head reads, metagraph reads, and `set_weights` over the long-lived socket |
| `SUBNET_OWNER_UID` | required | Uid that absorbs the burn slot + floor-rounding dust. Static knob until a follow-up resolves it from `SubnetOwnerHotkey`. |
| `POLL_INTERVAL_SECS` | `60` | Seconds between `process_once` ticks |
| `METRICS_PORT` | `9092` | Prometheus metrics HTTP port |
| `GM_WEIGHT_EARNINGS_MULTIPLIER` | `1` | **TESTNET-ONLY** demo knob. Scales each miner's aggregated earnings in memory before the alpha/weight conversion so tiny test earnings cross the `1/65535` weight floor and light up on-chain. **MUST stay `1` (unset) on mainnet** — any other value distorts real payouts. |

The pool denominator's `emissions_alpha` is read from `epoch_summary.json` (chain-truth, written by the gm finalizer); see `validator/src/gm_validator/epoch_summary.py`.

## Key conventions

- `bittensor` is lazily imported to keep the test path independent of the bittensor-py wheel.
- Epoch discovery is driven by the chain head, not persisted state. The submit guards are in-memory only; a restart re-scores the current target and re-submits the identical (idempotent) weight vector. A duplicate landing inside the chain's weight-set rate-limit window is rejected, which leaves the guards unset so the validator re-scores each poll until the window clears and the submit is accepted.
- Scoring uses nano-dollar-precision integer arithmetic throughout. Weights are the only floating-point values, derived only after all integer sums are complete.
