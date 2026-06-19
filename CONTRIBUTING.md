# Contributing to gm-validator

Thanks for your interest in contributing. `gm-validator` is the on-chain
weight-setter for the gm Bittensor subnet: a single Python service that reads
finalized epoch artifacts from S3 and submits weights via
`subtensor.set_weights()`. Read `CLAUDE.md` for the design and the layout
before making changes.

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

The validator is a [`uv`](https://docs.astral.sh/uv/) project targeting
**Python 3.13**. From the repo root:

```bash
cd validator
uv sync --group dev
```

Optionally install the [`prek`](https://github.com/j178/prek) git hooks
(a standalone CLI, not a project dependency) to run the formatters and
linters before each commit:

```bash
prek install
prek run
```

## Building, testing, and linting

All commands run from `validator/`. Pin `ruff` and `ty` to the CI versions so
a local pass matches the checked result:

```bash
uv tool run --from "ruff==0.15.12" ruff check .
uv tool run --from "ruff==0.15.12" ruff format --check .
uv tool run --from "ty==0.0.37" ty check src
uv run pytest -q
```

Fix every warning before committing — a clean output is the baseline.

To run the validator locally without on-chain submission (a mock-chain smoke
run — the mock cursor reports no open epoch, so the loop only prunes local
mirrors and never reaches S3 discovery, scoring, or submission):

```bash
BITTENSOR_MOCK=1 S3_BUCKET="your-bucket" SUBNET_OWNER_UID="0" \
  uv run python -m gm_validator.main
```

See the env-var reference in `README.md` and `CLAUDE.md` for the full set of
knobs.

## Money units

All money — earnings, surcharges, pool totals — is integer **nano-dollars**
(1 nUSD = 10⁻⁹ USD). No floats in money math: weights are the only
floating-point values, derived only after every integer sum is complete.

## Commit and PR conventions

- Use **imperative mood** subject lines, ≤72 characters, one logical change
  per commit.
- Work on **feature branches**; never push directly to `main`. Open a pull
  request and let CI run.
- **Do not** add `Co-Authored-By` trailers or AI-attribution lines to
  commits or PR descriptions.
- Describe what the code does now — not discarded approaches or prior
  iterations. Use plain, factual language.

## Comments

Default to no comment. Add one only when the *why* is non-obvious — a hidden
constraint, a subtle invariant, or a workaround. Never explain *what*
well-named code already says. See `CLAUDE.md` for examples.
