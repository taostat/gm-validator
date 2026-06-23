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
- A validator hotkey registered on the gm subnet (netuid 28 on mainnet)
- **No S3 credentials** on mainnet — the finalized-artifacts bucket is
  public-read

## Run a validator (gm mainnet, netuid 28)

The only value you supply is your own hotkey seed — everything else is in the
bundled [`.env.mainnet`](.env.mainnet) and is identical for every validator.

**1. Register a hotkey on netuid 28** (one-time, burns TAO):

```bash
btcli subnet register --netuid 28 --network finney \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>
```

**2. Copy the mainnet config and add your seed:**

```bash
cp .env.mainnet .env
# edit .env → set BITTENSOR_HOTKEY_SEED (your hotkey's BIP-39 mnemonic or 0x seed)
```

**3. Run:**

```bash
cd validator
uv sync
set -a && source ../.env && set +a
uv run python -m gm_validator.main
```

That's it — no S3 credentials, no per-validator config. The signing keypair is
built in memory from `BITTENSOR_HOTKEY_SEED`; no wallet keyfile is read from or
written to disk. Keep the seed out of git and logs (`.env` is gitignored).

## Development

```bash
cd validator
uv sync --group dev

# lint, type-check, test (ruff and ty pinned to the CI versions)
uv tool run --from "ruff==0.15.12" ruff check .
uv tool run --from "ruff==0.15.12" ruff format --check .
uv tool run --from "ty==0.0.37" ty check src
uv run pytest -q
```

Smoke-run against a mock chain — the mock cursor reports no open epoch, so the
loop only prunes local mirrors and never reaches S3 discovery, scoring, or
submission:

```bash
BITTENSOR_MOCK=1 S3_BUCKET=gm-mainnet SUBNET_OWNER_UID=3 \
  uv run python -m gm_validator.main
```

## Configuration

For gm mainnet you don't need to set these by hand — [`.env.mainnet`](.env.mainnet)
has them filled in. The table is the full reference (defaults shown; the
mainnet values are noted where they differ).

| Variable | Default | Purpose |
|---|---|---|
| `S3_BUCKET` | required | Bucket with finalized artifacts (mainnet: `gm-mainnet`) |
| `SUBNET_OWNER_UID` | required | Uid that absorbs the burn slot + floor-rounding dust (mainnet: `3`) |
| `S3_PREFIX` | `v1` | Key prefix |
| `S3_ENDPOINT_URL` | — | S3 endpoint (mainnet: `https://s3.gra.io.cloud.ovh.net`) |
| `AWS_REGION` | `gra` | S3 region (mainnet: `gra`) |
| `GM_VALIDATOR_S3_ANONYMOUS` | `0` | Skip request signing for public-read buckets (mainnet: `1`, no AWS keys) |
| `BLOCKS_PER_EPOCH` | `361` | Epoch length (`tempo + 1`); must equal the finalizer's divisor |
| `BITTENSOR_NETUID` | `0` | Subnet UID (mainnet: `28`) |
| `BITTENSOR_ENDPOINT` | — | Subtensor `wss://` URL (mainnet: `wss://entrypoint-finney.opentensor.ai:443`) |
| `BITTENSOR_HOTKEY_SEED` | — | Validator hotkey seed; required unless `BITTENSOR_MOCK=1` |
| `BITTENSOR_MOCK` | `0` | Run without on-chain submission |

`CLAUDE.md` documents the full set, including the local mirror, retention,
timeout, polling, and metrics knobs.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues privately
per [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0.
