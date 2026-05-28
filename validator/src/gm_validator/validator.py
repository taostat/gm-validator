"""Validator orchestrator.

Loop:

1. Discover finalized epoch ids in S3.
2. For each not yet processed:
   a. Mirror artifacts locally (raw, aggregated, gateway_keys,
      _FINALIZED, and best-effort epoch_summary.json).
   b. Invoke `gm-verifier verify`. On failure: alert + skip submission.
   c. Compute per-miner scores from `aggregated.jsonl`.
   d. Build a u16 weight vector — emission cap + burn when
      `USE_EMISSION_CAP=true` and `epoch_summary.json` is present;
      naive normalisation otherwise.
   e. Submit via the configured `Submitter`.
   f. Mark the epoch processed.
3. Prune local mirrors older than the retention window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gm_validator.bittensor_adapter import Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import EpochSummary, epoch_summary_path, load_epoch_summary
from gm_validator.processed_state import ProcessedState
from gm_validator.s3_mirror import S3Mirror
from gm_validator.scoring import (
    aggregated_path,
    compute_weights,
    load_aggregated,
    score,
)
from gm_validator.verifier import VerifierResult, verify_epoch

LOGGER = logging.getLogger(__name__)


@dataclass
class EpochOutcome:
    """Per-epoch processing result, surfaced for tests + metrics."""

    epoch_id: int
    verifier_ok: bool
    weights_submitted: bool
    miner_count: int
    total_ndollars: int
    used_emission_cap: bool


class Validator:
    """Top-level validator service."""

    def __init__(
        self,
        config: ValidatorConfig,
        mirror: S3Mirror,
        submitter: Submitter,
        miner_uid_lookup: dict[str, int] | None = None,
        processed_state: ProcessedState | None = None,
    ) -> None:
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._miner_uid_lookup = miner_uid_lookup or {}
        self._processed = processed_state or ProcessedState(config.processed_state_path)

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
            self._processed.mark(epoch_id)
            outcomes.append(outcome)
        self._mirror.prune(self._config.mirror_retention_epochs)
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
                total_ndollars=0,
                used_emission_cap=False,
            )

        rows = load_aggregated(aggregated_path(mirror_dir))
        scores = score(rows)
        total = sum(s.earnings_ndollars + s.surcharge_ndollars for s in scores.values())

        # Only the cap path consumes epoch_summary.json; reading it when
        # the flag is off lets a malformed artifact fail an opted-out
        # deployment, so gate the read. Schema errors here are loud on
        # purpose when the cap is on.
        epoch_summary: EpochSummary | None = None
        if self._config.use_emission_cap:
            epoch_summary = load_epoch_summary(epoch_summary_path(mirror_dir))

        vector = compute_weights(
            scores,
            self._miner_uid_lookup,
            use_emission_cap=self._config.use_emission_cap,
            epoch_summary=epoch_summary,
            alpha_emission_per_epoch=self._config.alpha_emission_per_epoch,
            subnet_owner_uid=self._config.subnet_owner_uid,
        )

        submitted = False
        if vector.uids:
            self._submitter.submit(
                netuid=self._config.bittensor_netuid,
                uids=vector.uids,
                weights=vector.weights,
                epoch_id=epoch_id,
            )
            submitted = True
            if vector.epoch_result is not None:
                LOGGER.info(
                    "epoch %d: cap+burn scale=%s burn_weight=%s pool_usd=%s consumed_usd=%s",
                    epoch_id,
                    vector.epoch_result.scale,
                    vector.epoch_result.burn_weight,
                    vector.epoch_result.pool_usd_total,
                    vector.epoch_result.total_consumed_usd,
                )

        return EpochOutcome(
            epoch_id=epoch_id,
            verifier_ok=True,
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
            used_emission_cap=vector.used_emission_cap,
        )

    def _verify(self, epoch_id: int, mirror_dir: str) -> VerifierResult:
        return verify_epoch(
            verifier_bin=self._config.verifier_bin,
            epoch_id=epoch_id,
            mirror_dir=mirror_dir,
            sample_per_tuple=self._config.verifier_sample_per_tuple,
        )
