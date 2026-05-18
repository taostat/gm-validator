"""gm validator package.

Per `workstreams.md W6` and `docs/contracts/epoch-artifacts.md`:

1. Watch `s3://gm-validator-logs/v1/finalized/` for new `_FINALIZED`
   markers.
2. On detection: fetch `aggregated.jsonl`, `gateway_keys.json`, and
   `raw.jsonl.zst` to a local mirror.
3. Invoke the Rust `gm-verifier` binary (subprocess) for full-epoch
   verification: it recomputes `raw_hash` per `(miner_id, product)`
   tuple, verifies a sample of ed25519 signatures against
   `gateway_keys.json`. Mismatches => alert + skip weight submission.
4. Compute per-miner score:
   sum of `earnings_pdollars + surcharge_pdollars` across products.
5. Convert to subnet alpha at current exchange rate.
6. Submit weights via the configured Bittensor adapter (a mock during
   build; real `bittensor-py` `subtensor.set_weights()` in deploy).
"""

__all__: list[str] = []
