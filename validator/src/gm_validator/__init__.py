"""gm validator package.

Per `docs/contracts/epoch-artifacts.md`:

1. Watch `s3://gm-validator-logs/v1/finalized/` for new `_FINALIZED`
   markers.
2. On detection: fetch the artifact set (`aggregated.jsonl`,
   `epoch_summary.json`, etc.) to a local mirror. The gm-operated
   finalizer has already cost-derived each row; the validator treats
   those numbers as authoritative.
3. Compute per-miner score:
   sum of `earnings_ndollars + surcharge_ndollars` across products.
4. Convert to subnet alpha at the chain-snapshot price in
   `epoch_summary.json`.
5. Submit weights via the configured Bittensor adapter (a mock during
   build; real `bittensor-py` `subtensor.set_weights()` in deploy).
"""

__all__: list[str] = []
