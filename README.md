# gm-validator

Validator software for the gm Bittensor subnet.

| Path | Description |
|---|---|
| `validator/` | Python service that watches S3 for finalized epoch markers, computes weights from the cost-derived rows, and submits to Bittensor. |
| `docs/` | Operator-facing docs (running a validator). |

The gm-operated epoch-finalizer is the single source of truth for
per-record cost derivation; the validator treats the published
artifact set (`aggregated.jsonl` + `epoch_summary.json`) as
authoritative and only computes the weight vector.

## License

Apache-2.0.
