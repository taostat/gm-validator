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
- `src/gm_validator/epoch_summary.py` — Pydantic model + S3 reader for the finalizer's per-epoch `epoch_summary.json` (alpha USD price snapshot)
- `src/gm_validator/bittensor_adapter.py` — `Submitter` protocol; `MockSubmitter` for testing
- `src/gm_validator/bittensor_real.py` — `RealSubmitter`: lazily-imported to avoid loading bittensor-py in tests
- `src/gm_validator/metagraph.py` — lazy hotkey→uid lookup from the subnet metagraph
- `src/gm_validator/config.py` — `ValidatorConfig.from_env()`
- `src/gm_validator/processed_state.py` — persisted set of already-processed epoch ids (JSON file)

## Build / lint / test

```bash
cd validator
uv sync --group dev

uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src
uv run pytest -q

# run (requires S3_BUCKET, BITTENSOR_* set or BITTENSOR_MOCK=1)
uv run python -m gm_validator.main
```

## Key env vars (validator)

| Variable | Default | Purpose |
|---|---|---|
| `S3_BUCKET` | required | Bucket containing finalized epoch artifacts |
| `S3_PREFIX` | `v1` | Key prefix |
| `S3_ENDPOINT_URL` | — | Override (MinIO for local dev) |
| `GM_VALIDATOR_S3_ANONYMOUS` | `0` | Skip request signing (OVH public-read buckets) |
| `LOCAL_MIRROR_DIR` | `/var/cache/gm-validator` | Local audit cache of finalized artifacts |
| `MIRROR_RETENTION_EPOCHS` | `10` | How many recent epoch mirrors to keep on disk |
| `PROCESSED_STATE_PATH` | `<mirror_dir>/processed.json` | Crash-safe record of submitted epochs |
| `BITTENSOR_NETUID` | `0` | Subnet UID |
| `BITTENSOR_ENDPOINT` | — | Subtensor `wss://` URL (SDK default network when unset) |
| `BITTENSOR_HOTKEY_SEED` | — | Validator hotkey seed — a BIP-39 mnemonic or `0x`-prefixed hex seed. The signing keypair is built in memory; no keyfile on disk. Required for real weight submission |
| `BITTENSOR_MOCK` | `0` | Use `MockSubmitter` (records submissions in memory) |
| `SUBNET_OWNER_UID` | required | Uid that absorbs the burn slot + floor-rounding dust. Static knob until a follow-up resolves it from `SubnetOwnerHotkey`. |

The pool denominator's `emissions_alpha` is read from `epoch_summary.json` (chain-truth, written by the gm finalizer); see `validator/src/gm_validator/epoch_summary.py`.

## Key conventions

- `bittensor` is lazily imported to keep the test path independent of the bittensor-py wheel.
- Processed epoch state is persisted to disk so a validator restart does not re-submit weights for epochs already finalized on-chain.
- Scoring uses nano-dollar-precision integer arithmetic throughout. Weights are the only floating-point values, derived only after all integer sums are complete.
