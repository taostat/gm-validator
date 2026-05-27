# `cost_epoch` fixture

A complete epoch directory used by `tests/fixture_cost_rederivation.rs`
to exercise the verifier's cost re-derivation: `check_aggregated_totals`
re-runs the finalizer's pricing math over the raw records and fails the
epoch when the published `earnings_ndollars` / `surcharge_ndollars` do
not match.

## Files

- `raw.jsonl` — three `ValidatorLogRecord` lines, the human-readable
  source of truth.
- `raw.jsonl.zst` — zstd-compressed `raw.jsonl`; the artifact the
  `verify` command actually reads. Regenerate after editing `raw.jsonl`:
  `zstd -q -f -o raw.jsonl.zst raw.jsonl`.
- `aggregated.jsonl` — one aggregated row with **correct** totals;
  `verify` exits 0.
- `aggregated_tampered.jsonl` — identical, but `earnings_ndollars` is
  inflated from `1500000000000` to `9900000000000` while
  `successful_requests`, `failed_requests`, `raw_record_count`, and
  `raw_hash` stay valid. `verify` must exit non-zero, proving the cost
  check (not the count or hash check) caught the tamper.
- `gateway_keys.json`, `_FINALIZED` — required artifacts for `verify`.

The test stages `aggregated.jsonl` from either source into a temp dir.

## Re-derivation by hand

- record A: `input_tokens` 1,000,000 at $1/Mtok (`1e12` nUSD/Mtok)
  = `1e12`; `batch_bps` 5000 → `5e11`. Surcharge: count 2 × `1e10`
  = `2e10`.
- record B: `output_tokens` 500,000 at $2/Mtok (`2e12` nUSD/Mtok)
  = `1e12`; no modifiers, no surcharges.
- record C: `success: false` → contributes 0 to both totals.

Totals: `earnings_ndollars = 1500000000000`,
`surcharge_ndollars = 20000000000`.

## `raw_hash`

The aggregated rows pin `raw_hash` so the count/hash checks pass and the
test isolates the cost check. Regenerate after editing `raw.jsonl`:

```
cargo run --package gm-verifier --bin gm-verifier -- \
    hash-fixture --file verifier/tests/fixtures/cost_epoch/raw.jsonl
```

and copy the hex into the `raw_hash` field of both `aggregated*.jsonl`.
