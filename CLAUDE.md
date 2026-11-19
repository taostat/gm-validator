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
- `src/gm_validator/validator.py` — `Validator.process_once()`: read the head and latest pointer, check geometry, mirror artifacts locally, score, submit
- `src/gm_validator/s3_mirror.py` — `S3Mirror`: reads `finalized/latest.json`, syncs S3 epoch artifacts to a local directory, and prunes old epochs
- `src/gm_validator/scoring.py` — `score()` + `compute_weights()`: per-miner totals → u16 vector summing to `MAX_WEIGHT`; cap+burn pipeline only
- `src/gm_validator/alpha_economics.py` — `compute_epoch_weights()` (per-miner `consumed_usd / pool_usd`) + `normalize_weights()` (float→u16, renorms when sum > 1, burn slot absorbs the residue when sum < 1); ported from bm-validator
- `src/gm_validator/latest_pointer.py` — strict, frozen model for the finalizer's `finalized/latest.json` shape
- `src/gm_validator/epoch_summary.py` — Pydantic model + local reader for the mirrored per-epoch `epoch_summary.json` (block geometry, alpha USD price + emission snapshot)
- `src/gm_validator/bittensor_adapter.py` — `Submitter` + `ChainCursor` protocols; `MockSubmitter`/`MockChainCursor` for testing
- `src/gm_validator/bittensor_real.py` — `RealSubmitter`: lazily-imported to avoid loading bittensor-py in tests; holds the one long-lived socket and serves the startup hotkey→uid lookup over it
- `src/gm_validator/subtensor_connect.py` — `connect_subtensor`: retrying, timeout-bounded subtensor construction (a hung connect becomes a retryable `TimeoutError`)
- `src/gm_validator/metrics.py` — Prometheus gauges and counters for submissions, pointer errors, and geometry mismatches, including `gm_validator_latest_pointer_errors_total` (reason: missing, unreadable, malformed) and `gm_validator_epoch_geometry_mismatch_total`
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
| `BLOCKS_PER_EPOCH` | ignored | Legacy setting; ignored with a warning when present. Epoch targets come from `finalized/latest.json` |
| `FINALIZED_LOOKBACK_EPOCHS` | ignored | Legacy setting; ignored with a warning when present. The validator only targets `finalized/latest.json` |
| `BITTENSOR_NETUID` | `0` | Subnet UID |
| `BITTENSOR_ENDPOINT` | — | Subtensor `wss://` URL (SDK default network when unset) |
| `BITTENSOR_HOTKEY_SEED` | — | Validator hotkey seed — a BIP-39 mnemonic or `0x`-prefixed hex seed. The signing keypair is built in memory; no keyfile on disk. Required for real weight submission |
| `BITTENSOR_MOCK` | `0` | Use `MockSubmitter` (records submissions in memory) |
| `SUBTENSOR_CONNECT_TIMEOUT_SECS` | `30` | Wall-clock budget for one subtensor connect attempt; a hang past this becomes a retryable `TimeoutError` |
| `SUBTENSOR_RPC_TIMEOUT_SECS` | `30` | Per-RPC timeout for chain-head reads, metagraph reads, and `set_weights` over the long-lived socket |
| `SUBNET_OWNER_UID` | required | Uid that absorbs the burn slot + floor-rounding dust. Static knob until a follow-up resolves it from `SubnetOwnerHotkey`. |
| `POLL_INTERVAL_SECS` | `60` | Seconds between `process_once` ticks |
| `GM_VALIDATOR_METRICS_BIND` | unset | `host:port` (or bare `port`, host defaults `0.0.0.0`) for the Prometheus metrics server. Unset means no metrics endpoint is opened — default-off so a third-party operator never exposes an unexpected listening socket. |
| `GM_WEIGHT_EARNINGS_MULTIPLIER` | `1` | **TESTNET-ONLY** demo knob. Scales each miner's aggregated earnings in memory before the alpha/weight conversion so tiny test earnings cross the `1/65535` weight floor and light up on-chain. **MUST stay `1` (unset) on mainnet** — any other value distorts real payouts. |

The pool denominator's `emissions_alpha` is read from `epoch_summary.json` (chain-truth, written by the gm finalizer); see `validator/src/gm_validator/epoch_summary.py`.

## Key conventions

- `bittensor` is lazily imported to keep the test path independent of the bittensor-py wheel.
- Epoch discovery reads the chain head first and, when available, fetches a fresh
  `finalized/latest.json` each tick. The validator waits if its head has not passed
  the pointer epoch's close block or if the next epoch has closed without finalization.
  Its target guard is in-memory only, so a restart may re-submit the same epoch's
  identical weight vector; the rate-limit pre-gate defers submission while it is
  inside the chain window.
- Scoring uses nano-dollar-precision integer arithmetic throughout. Weights are the only floating-point values, derived only after all integer sums are complete.
