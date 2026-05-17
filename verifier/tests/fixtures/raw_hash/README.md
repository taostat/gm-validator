# `raw_hash` fixture

This directory pins the canonical `raw_hash` construction for
`docs/contracts/epoch-artifacts.md` so that the Python Finalizer (producer
in `taostat/gm`) and the Rust verifier (consumer in `taostat/gm-validator`)
agree byte-for-byte.

The fixture is a JSONL file of three `ValidatorLogRecord`-shaped objects.
A line at the bottom of `expected.txt` records the expected SHA-256 hex
hash under the canonical construction:

1. sort by `request_id` ascending
2. canonicalise each record (RFC 8785 JCS: sorted keys, no insignificant
   whitespace)
3. join with single LF, **no trailing newline**
4. SHA-256
5. lower-case hex

Both implementations must produce the same hash. Any drift fails
`cargo test --package gm-verifier --test fixture_raw_hash` on the Rust
side and `pytest tests/test_aggregation.py::test_raw_hash_fixture` on the
Python side.

To regenerate, run:

```
cargo run --package gm-verifier --bin gm-verifier -- \
    hash-fixture --file verifier/tests/fixtures/raw_hash/input.jsonl
```

and copy the hex into `expected.txt`.
