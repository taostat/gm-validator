"""Validator orchestrator.

Loop:

1. Discover finalized epoch ids in S3.
2. For each not yet processed:
   a. Mirror artifacts locally (raw, aggregated, gateway_keys,
      _FINALIZED, epoch_summary.json).
   b. Invoke `gm-verifier verify`. On failure: alert + skip submission.
   c. Compute per-miner scores from `aggregated.jsonl`.
   d. Build a u16 weight vector via cap+burn — miner i gets
      ``consumed_usd_i / pool_usd``; residue routes to the subnet-owner
      uid as burn weight.
   e. Submit via the configured `Submitter`.
   f. Mark the epoch processed.
3. Prune local mirrors older than the retention window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gm_validator.bittensor_adapter import Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import epoch_summary_path, load_epoch_summary
from gm_validator.processed_state import ProcessedState
from gm_validator.s3_mirror import S3Mirror
from gm_validator.scoring import (
    StaleEpochSummaryError,
    StaleMetagraphError,
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
            except StaleMetagraphError as exc:
                # Transient — metagraph hotkey->uid lookup is stale. Skip
                # marking processed so the next tick retries once the
                # lookup has refreshed.
                LOGGER.warning(
                    "epoch %d deferred (stale metagraph): %s — next tick will retry",
                    epoch_id,
                    exc,
                )
                continue
            except StaleEpochSummaryError as exc:
                # Transient — epoch_summary.json was written by a
                # pre-chain-read finalizer and so lacks emissions_alpha.
                # Skip marking processed so the next tick retries once
                # the finalizer has republished (the artifact's
                # _FINALIZED rewrite path is the operator escalation).
                LOGGER.warning(
                    "epoch %d deferred (stale epoch_summary): %s — next tick will retry",
                    epoch_id,
                    exc,
                )
                continue
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
            )

        rows = load_aggregated(aggregated_path(mirror_dir))
        scores = score(rows)
        total = sum(s.earnings_ndollars + s.surcharge_ndollars for s in scores.values())

        epoch_summary = load_epoch_summary(epoch_summary_path(mirror_dir))

        vector = compute_weights(
            scores,
            self._miner_uid_lookup,
            epoch_summary=epoch_summary,
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
            LOGGER.info(
                "epoch %d: miners=%d pool_usd=%s consumed_usd=%s",
                epoch_id,
                len(scores),
                vector.epoch_result.pool_usd_total,
                vector.epoch_result.total_consumed_usd,
            )

        return EpochOutcome(
            epoch_id=epoch_id,
            verifier_ok=True,
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
        )

    def _verify(self, epoch_id: int, mirror_dir: str) -> VerifierResult:
        return verify_epoch(
            verifier_bin=self._config.verifier_bin,
            epoch_id=epoch_id,
            mirror_dir=mirror_dir,
            sample_per_tuple=self._config.verifier_sample_per_tuple,
        )
