# gm-validator

The on-chain weight-setter for the gm Bittensor subnet.

Each tick the validator derives the open epoch from the chain head, targets
the newest finalized epoch, mirrors its artifact set
(`aggregated.jsonl` + `epoch_summary.json`) from S3, scores each miner from
the cost-derived rows, and submits a u16 weight vector via
`subtensor.set_weights()`.

The gm-operated epoch-finalizer is the single source of truth for per-record
cost derivation; the validator treats the published artifact set as
authoritative and does not re-derive cost or re-verify hashes or signatures.
It only computes the weight vector.

See [`CLAUDE.md`](CLAUDE.md) for the module layout, design decisions, and the
full env-var reference.

## Prerequisites

- **Python 3.13**
- [`uv`](https://docs.astral.sh/uv/) for dependency and environment
  management
- Read access to the S3 bucket holding finalized epoch artifacts
- For real (on-chain) submission: a validator hotkey seed and a reachable
  subtensor endpoint

## Quickstart

```bash
cd validator
uv sync --group dev

# lint, type-check, test (ruff and ty pinned to the CI versions)
uv tool run --from "ruff==0.15.12" ruff check .
uv tool run --from "ruff==0.15.12" ruff format --check .
uv tool run --from "ty==0.0.37" ty check src
uv run pytest -q
```

Run against a mock chain — the mock cursor reports no open epoch, so the loop
only prunes local mirrors and never reaches S3 discovery, scoring, or
submission (a build-phase smoke run):

```bash
BITTENSOR_MOCK=1 \
S3_BUCKET="your-bucket" \
SUBNET_OWNER_UID="0" \
  uv run python -m gm_validator.main
```

Run for real on-chain submission (substitute your own values):

```bash
S3_BUCKET="your-bucket" \
SUBNET_OWNER_UID="0" \
BITTENSOR_NETUID="0" \
BITTENSOR_ENDPOINT="wss://your-subtensor-endpoint" \
BITTENSOR_HOTKEY_SEED="your-bip39-mnemonic-or-0x-hex-seed" \
  uv run python -m gm_validator.main
```

The signing keypair is built in memory from `BITTENSOR_HOTKEY_SEED`; no
wallet keyfile is read from or written to disk. Never commit or log the seed.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `S3_BUCKET` | required | Bucket containing finalized epoch artifacts |
| `SUBNET_OWNER_UID` | required | Uid that absorbs the burn slot + floor-rounding dust |
| `S3_PREFIX` | `v1` | Key prefix |
| `S3_ENDPOINT_URL` | — | Endpoint override (e.g. MinIO for local dev) |
| `GM_VALIDATOR_S3_ANONYMOUS` | `0` | Skip request signing for public-read buckets |
| `BLOCKS_PER_EPOCH` | `361` | Epoch length (`tempo + 1`); must equal the finalizer's divisor |
| `BITTENSOR_NETUID` | `0` | Subnet UID |
| `BITTENSOR_ENDPOINT` | — | Subtensor `wss://` URL (SDK default network when unset) |
| `BITTENSOR_HOTKEY_SEED` | — | Validator hotkey seed; required unless `BITTENSOR_MOCK=1` |
| `BITTENSOR_MOCK` | `0` | Run without on-chain submission |

`CLAUDE.md` documents the full set, including the local mirror, retention,
timeout, polling, and metrics knobs.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues privately
per [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0.
