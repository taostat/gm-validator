## Summary

What this PR does and why. Describe what the code does now — not discarded
approaches or prior iterations.

## Checklist

- [ ] `uv tool run --from "ruff==0.15.12" ruff check .` passes
- [ ] `uv tool run --from "ruff==0.15.12" ruff format --check .` passes
- [ ] `uv tool run --from "ty==0.0.37" ty check src` passes
- [ ] `uv run pytest -q` passes
- [ ] No new warnings from any linter, type checker, or compiler
- [ ] Money values use integer nano-dollars (no floats in money math)
- [ ] Commit subjects are imperative mood, ≤72 chars, one logical change each

## Notes for reviewers

Anything reviewers should focus on, plus context that isn't obvious from the
diff.
