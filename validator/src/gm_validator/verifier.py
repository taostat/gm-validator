"""Thin wrapper around the `gm-verifier` Rust subprocess.

The Rust verifier owns the canonical `raw_hash` construction and
ed25519 signature verification. The Python validator never replicates
this logic — drift is the failure mode that this whole workstream is
designed to prevent (see `workstreams.md` W6 cross-repo justification).

Sample size and the verifier binary path are taken from
`ValidatorConfig`. The binary is expected to be on PATH or pointed at
by `GM_VERIFIER_BIN`.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


class VerifierError(RuntimeError):
    """The verifier subprocess returned a non-zero exit code."""


@dataclass
class VerifierResult:
    """Outcome of a `gm-verifier verify` invocation."""

    epoch_id: int
    ok: bool
    stdout: str
    stderr: str


def verify_epoch(
    *, verifier_bin: str, epoch_id: int, mirror_dir: str, sample_per_tuple: int
) -> VerifierResult:
    """Invoke `gm-verifier verify --epoch N --dir D --sample S`.

    Returns a `VerifierResult` with `ok=True` on exit 0 and `ok=False`
    otherwise. The stderr is preserved so the caller can attach it to
    the alert/log.

    Raises:
        FileNotFoundError: The `verifier_bin` could not be executed.
    """
    cmd = [
        verifier_bin,
        "verify",
        "--epoch",
        str(epoch_id),
        "--dir",
        mirror_dir,
        "--sample",
        str(sample_per_tuple),
    ]
    LOGGER.info("running %s", " ".join(cmd))
    proc = subprocess.run(  # noqa: S603 - cmd is fully constructed from typed args
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    return VerifierResult(
        epoch_id=epoch_id,
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
