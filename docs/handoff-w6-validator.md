# Handoff — W6 Validator + gm-verifier

**Branch (gm-validator)**: `phase1/validator-pipeline`.
**Worktree (local)**: `/Users/mark/Contracts/gm-validator-w6/`.
**Status**: Phase 1 W6 (consumer half) complete. Companion branch on
`taostat/gm` is `phase1/validator-pipeline-finalizer`.

## What landed

### Rust `gm-verifier` (library + CLI)

Under `verifier/`. Library `gm_verifier` + binary `gm-verifier`. Owns
two byte-sensitive operations the W6 spec requires to be drift-free
between producer (Finalizer) and consumer (Validator):

- **`canonical`** — RFC 8785 JCS canonical-JSON. Sorts object keys by
  UTF-16 code unit, escapes control characters and the structural
  punctuation per spec, refuses to canonicalise floats.
- **`hash`** — `raw_hash` construction: sort by `request_id`,
  canonicalise each record, LF-join with no trailing newline, SHA-256,
  lower-case hex. Pinned in
  `verifier/tests/fixtures/raw_hash/input.jsonl` +
  `expected.txt`. The Python Finalizer hashes the same input and
  asserts the same expected value, so the two implementations cannot
  drift silently.
- **`signature`** — ed25519 verification of `ValidatorLogRecord`. The
  signature is over `SHA-256(canonical_json(record - signature_field))`.
  At verification time the caller doesn't know which of the gateway's
  pubkeys (per `gateway_keys.json`) signed the record; the verifier
  iterates the list and tries each one until one verifies.

CLI subcommands:

- `verify --epoch N --dir D --sample S` — full-epoch verification of a
  local mirror of `s3://.../finalized/epoch={N}/`. Recomputes
  `raw_hash` for every aggregated row; verifies at most S signatures
  per `(miner_id, product)` against `gateway_keys.json`. Exits 0 on
  success, non-zero on any mismatch with structured stderr.
- `hash-fixture --file f` — operator/CI helper for pinning canonical
  output. Used in both repos' test suites.
- `canonicalize --file f` — hex-encoded canonical JSON of a single
  record.

### Python `gm-validator` service

Under `validator/`. Watches S3 for `_FINALIZED` markers and consumes:

- **`s3_mirror.py`** — discovers finalized epochs, materialises the four
  artifacts into `LOCAL_MIRROR_DIR/epoch={N}/`. Idempotent (a re-tick
  is a no-op). Prunes mirrors for epochs that have been processed.
- **`verifier.py`** — invokes `gm-verifier verify` as a subprocess.
  This is the language boundary the W6 launch prompt mandated; drift
  cannot happen because the Python service never replicates the
  canonical-hash or signature logic.
- **`scoring.py`** — per-miner score = sum of `earnings_pdollars +
  surcharge_pdollars` across products. `normalise_weights` produces a
  weight vector summing to 1.0 (or all zeros when total earnings = 0).
- **`bittensor_adapter.py`** — `Submitter` protocol with a
  `MockSubmitter` for tests. Real `bittensor-py` adapter (lazy import
  in `main.py`) lands in Phase 2 alongside the testnet deploy.
- **`validator.py`** — `process_once()` runs the discover →
  mirror → verify → score → submit loop and returns `EpochOutcome`
  records suitable for metrics and tests.

## Aggregation tested against synthesized epochs

`validator/tests/test_validator_integration.py` populates moto S3 with a
synthesized finalized epoch (uses `gm-verifier hash-fixture` to compute
the per-tuple `raw_hash` so the in-test aggregator produces verifier-
accepted input — i.e. the Rust verifier validates its own producer
contract end-to-end). The validator then:

- mirrors the four artifacts to a temp dir
- invokes `gm-verifier verify` (the real binary)
- computes weights
- submits to the `MockSubmitter`
- asserts the weights vector sums to 1.0 and contains both miners

A second test corrupts an aggregated row's `raw_hash` to `0xff...ff`,
re-runs the validator, and asserts the verifier exits non-zero and the
validator skips weight submission.

`validator/tests/test_scoring.py` adds 6 unit + hypothesis tests on
weight normalisation: total weight == 1.0 (or 0), surcharges included,
default_factory correctness on `MinerScore.per_product`.

`verifier/tests/fixture_raw_hash.rs` is the Rust side of the cross-
language fixture pin (Python side: `epoch-finalizer/tests/test_canonical.py`).

## Verifier reproducibility

The CLI is fully reproducible: given the four S3 artifacts, `gm-verifier
verify` recomputes every `raw_hash` and (sample-limited) ed25519
signature. Auditors can drop into `target/release/gm-verifier verify
--epoch N --dir /path/to/mirror` for any epoch the validator processed.

The CLI is also the audit primitive for the launch-prompts'
"reusable lib used by both Python services" requirement: the
Python Validator drives the same binary the operator would.

## Contract questions surfaced

None. The contract walk-throughs are sufficient. Two implementation
choices worth flagging for review:

- I chose to mirror artifacts to disk and shell out to `gm-verifier`
  rather than embed the Rust code via PyO3. Subprocess overhead is
  ~milliseconds per epoch, and the file mirror doubles as an auditor-
  friendly local cache. PyO3 stays available as a future optimisation.
- The validator pubkey lookup (hotkey → uid) is left empty by default;
  Phase 2 I3 fills it from the subnet metagraph at startup. With an
  empty lookup the `MockSubmitter` receives empty `uids`/`weights`
  lists, which is logged but not an error — useful for build-phase
  smoke tests.

## Integration notes for Phase 2 (I1, I3)

- Real `bittensor-py` `RealSubmitter` is referenced in `main.py` but
  not implemented (`bittensor_real` module is intentionally absent so
  the test path doesn't pull in the heavy dep). Land it in I3 at the
  same time as the metagraph-driven UID lookup.
- The default sample size is 16 records per tuple. With ed25519 verify
  cost ~50µs and ~50 tuples per epoch at v1 scale, full-epoch
  verification is sub-second.
- `S3_PREFIX` defaults to `v1`; consumers and producers must agree.
- `gm-verifier verify` deliberately accepts a `--sample 0` mode that
  skips signature checks. Useful for offline testing against gateways
  that haven't yet implemented signing. Production deploys MUST run
  with `--sample >= 1`.
