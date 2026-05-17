"""Validator orchestrator.

Loop:

1. Discover finalized epoch ids in S3.
2. For each not yet processed:
   a. Mirror artifacts locally.
   b. Invoke `gm-verifier verify`. On failure: alert + skip submission.
   c. Compute per-miner scores from `aggregated.jsonl`.
   d. Normalise weights, submit via the configured `Submitter`.
   e. Mark the epoch processed.
3. Prune local mirrors older than the retention window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gm_validator.bittensor_adapter import Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.s3_mirror import S3Mirror
from gm_validator.scoring import aggregated_path, load_aggregated, normalise_weights, score
from gm_validator.verifier import VerifierResult, verify_epoch

LOGGER = logging.getLogger(__name__)


@dataclass
class EpochOutcome:
    """Per-epoch processing result, surfaced for tests + metrics."""

    epoch_id: int
    verifier_ok: bool
    weights_submitted: bool
    miner_count: int
    total_pdollars: int


class Validator:
    """Top-level validator service."""

    def __init__(
        self,
        config: ValidatorConfig,
        mirror: S3Mirror,
        submitter: Submitter,
        miner_uid_lookup: dict[str, int] | None = None,
    ) -> None:
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._miner_uid_lookup = miner_uid_lookup or {}
        self._processed: set[int] = set()

    def process_once(self) -> list[EpochOutcome]:
        """One iteration: process every newly-finalized epoch."""
        outcomes: list[EpochOutcome] = []
        for epoch_id in self._mirror.discover_finalized_epochs():
            if epoch_id in self._processed:
                continue
            try:
                outcome = self._process_epoch(epoch_id)
            except Exception:
                LOGGER.exception("epoch %d processing failed", epoch_id)
                continue
            self._processed.add(epoch_id)
            outcomes.append(outcome)
        self._mirror.prune(self._processed)
        return outcomes

    def _process_epoch(self, epoch_id: int) -> EpochOutcome:
        mirror_dir = self._mirror.mirror_epoch(epoch_id)
        verifier_result = self._verify(epoch_id, mirror_dir)
        if not verifier_result.ok:
            LOGGER.error(
                "epoch %d: verifier failed (stderr=%s); skipping weight submission",
                epoch_id,
                verifier_result.stderr.strip(),
            )
            return EpochOutcome(
                epoch_id=epoch_id,
                verifier_ok=False,
                weights_submitted=False,
                miner_count=0,
                total_pdollars=0,
            )

        rows = load_aggregated(aggregated_path(mirror_dir))
        scores = normalise_weights(score(rows))
        total = sum(s.earnings_pdollars + s.surcharge_pdollars for s in scores.values())

        uids, weights = self._uids_and_weights(scores)
        submitted = False
        if uids:
            self._submitter.submit(
                netuid=self._config.bittensor_netuid,
                uids=uids,
                weights=weights,
                epoch_id=epoch_id,
            )
            submitted = True

        return EpochOutcome(
            epoch_id=epoch_id,
            verifier_ok=True,
            weights_submitted=submitted,
            miner_count=len(scores),
            total_pdollars=total,
        )

    def _verify(self, epoch_id: int, mirror_dir: str) -> VerifierResult:
        return verify_epoch(
            verifier_bin=self._config.verifier_bin,
            epoch_id=epoch_id,
            mirror_dir=mirror_dir,
            sample_per_tuple=self._config.verifier_sample_per_tuple,
        )

    def _uids_and_weights(self, scores: dict[str, Any]) -> tuple[list[int], list[float]]:
        """Translate hotkey-keyed scores to (uid, weight) lists.

        Miners not in the lookup table are skipped silently; in real
        deployment the lookup is populated from the subnet's metagraph
        at startup.
        """
        uids: list[int] = []
        weights: list[float] = []
        for miner_id, s in scores.items():
            uid = self._miner_uid_lookup.get(miner_id)
            if uid is None:
                continue
            uids.append(uid)
            weights.append(s.weight)
        return uids, weights
