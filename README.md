# gm-validator

Validator software for the gm Bittensor subnet.

| Path | Owner | Description |
|---|---|---|
| `validator/` | W6 | Python service that watches S3 for finalized epoch markers, verifies a sample of signatures, computes weights, submits to Bittensor. |
| `verifier/` | W6 | Rust binary `gm-verifier` — re-verifies an epoch end-to-end. Shared library used by the validator's hot path so producer (finalizer) and consumer (validator) share the verification code. |
| `docs/` | W6 | Operator-facing docs (running a validator). |

Scaffolded in Phase 0 by `agent-foundation`; implemented in Phase 1 by
`agent-validator` (W6). The W6 agent owns both this repo and the
`epoch-finalizer/` subdirectory of `taostat/gm` so the producer/consumer
verification code stays in lockstep.

## Getting started for Phase 1 / W6

```bash
git clone git@github-taostat:taostat/gm-validator.git
cd gm-validator
wt switch phase1/validator-pipeline   # see workstreams.md
```

Workstream scope and Definition of Done in
`taostat/gm` → `workstreams.md` → W6.

## License

Apache-2.0.
