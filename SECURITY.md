# Security Policy

## Reporting a vulnerability

Report security issues privately. **Do not open a public GitHub issue for
a vulnerability.**

Email **security@saygm.com** with:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- affected component(s) and version/commit, and
- any suggested remediation.

If you prefer, open a private advisory via GitHub's
[Security Advisories](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/about-coordinated-disclosure-of-security-vulnerabilities)
feature on this repository.

We ask that you give us a reasonable window to investigate and ship a fix
before any public disclosure.

## Response expectations

| Stage | Target |
|---|---|
| Acknowledgement of report | within 3 business days |
| Initial assessment / severity triage | within 7 business days |
| Status updates during investigation | at least every 7 days |
| Fix or mitigation for confirmed high-severity issues | as soon as practical, coordinated with the reporter |

## Supported versions

gm-validator ships from `main`; the deployed validator tracks the latest
release. Security fixes land on `main` and are rolled out from there. Older
commits are not separately patched.

| Version | Supported |
|---|---|
| `main` (latest) | Yes |
| older commits | No |

## Threat model

The validator is the on-chain weight-setter for the gm subnet. It reads
finalized epoch artifacts (`aggregated.jsonl` + `epoch_summary.json`) from
S3 and submits a u16 weight vector via `subtensor.set_weights()`. It does
**not** re-derive cost or re-verify hashes or signatures — the gm-operated
epoch-finalizer is the single source of truth, and the published artifact
set is treated as authoritative.

Reports are treated as high severity when they concern:

- the validator hotkey seed (`BITTENSOR_HOTKEY_SEED`) — it is read from the
  environment and the signing keypair is built in memory; any path that
  could leak it, write it to disk, or log it,
- weight-vector manipulation — an input or code path that lets a miner earn
  emission disproportionate to its scored consumption, or that pushes
  emission to an unintended uid, and
- artifact-trust boundary issues — a way to make the validator score a
  forged or tampered artifact set as if it were authoritative.
