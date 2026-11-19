"""gm validator package.

One ``Validator.process_once`` tick (see :mod:`gm_validator.validator`):

1. Read the chain head and, when available, fetch ``finalized/latest.json``
   for the newest finalized epoch. Compare its block geometry with the head.
2. Mirror that epoch's finalized artifact set
   (``aggregated.jsonl``, ``epoch_summary.json``, ``_FINALIZED``) to a
   local directory. The gm-operated finalizer has already cost-derived
   each row; the validator treats those numbers as authoritative and
   does not re-verify hashes or signatures.
3. Compute per-miner score: sum of
   ``earnings_ndollars + surcharge_ndollars`` across products.
4. Convert to a u16 weight vector via the cap+burn pipeline, using the
   alpha USD price and emission from ``epoch_summary.json``.
5. Submit weights via the configured Bittensor adapter (a mock during
   build; real ``bittensor-py`` ``subtensor.set_weights()`` in deploy).
"""

__all__: list[str] = []
