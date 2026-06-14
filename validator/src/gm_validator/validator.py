"""Validator orchestrator.

Loop:

1. Discover finalized epoch ids in S3, drop the already-processed ones.
2. Retire all but the newest ``max_epochs_per_tick`` to processed-state
   without a submit — weights are a current snapshot, so a stale backlog
   does not need re-submitting on chain.
3. For each of the newest unprocessed epochs:
   a. Mirror artifacts locally (`aggregated.jsonl`,
      `epoch_summary.json`, `_FINALIZED`).
   b. Compute per-miner scores from `aggregated.jsonl`.
   c. Build a u16 weight vector via cap+burn — miner i gets
      ``consumed_usd_i / pool_usd``; residue routes to the subnet-owner
      uid as burn weight.
   d. Submit via the configured `Submitter`.
   e. Mark the epoch processed.
4. Prune local mirrors older than the retention window.

The validator does not re-derive cost or re-verify `raw_hash` /
signatures. The gm-operated epoch-finalizer is the single source of
truth for cost re-derivation, and validators are operated by external
parties — rolling out pricing-math changes through them is expensive,
so the artifact set (`aggregated.jsonl` + `epoch_summary.json`) is
treated as authoritative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gm_validator.bittensor_adapter import Submitter
from gm_validator.bittensor_real import AlreadySubmittedError
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import (
    EPOCH_SUMMARY_FILENAME,
    epoch_summary_path,
    load_epoch_summary,
)
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

LOGGER = logging.getLogger(__name__)


@dataclass
class EpochOutcome:
    """Per-epoch processing result, surfaced for tests + metrics."""

    epoch_id: int
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
        """One iteration: submit the newest epochs, retire the rest.

        On-chain weights are a current snapshot, not a per-epoch ledger,
        so only the newest unprocessed epochs need a fresh submit. The
        older unprocessed backlog (which a fresh deploy rediscovers in
        full from S3) is retired to processed-state without a chain
        round-trip. This bounds the extrinsics issued per tick to
        ``max_epochs_per_tick`` — without it the whole backlog submits
        serially in one tick, each ``wait_for_inclusion`` leaking
        websocket state until the pod is OOMKilled mid-tick.
        """
        unprocessed = [
            epoch_id
            for epoch_id in self._mirror.discover_finalized_epochs()
            if epoch_id not in self._processed
        ]
        n = max(self._config.max_epochs_per_tick, 1)
        stale, recent = unprocessed[:-n], unprocessed[-n:]

        if stale:
            LOGGER.info("retiring %d superseded epoch(s) without submit", len(stale))
            self._processed.mark_many(set(stale))

        outcomes: list[EpochOutcome] = []
        for epoch_id in recent:
            outcome = self._process_recent_epoch(epoch_id)
            if outcome is not None:
                outcomes.append(outcome)
        self._mirror.prune(self._config.mirror_retention_epochs)
        return outcomes

    def _process_recent_epoch(self, epoch_id: int) -> EpochOutcome | None:
        """Process one recent epoch; return its outcome, or None if deferred.

        Deferrals (stale metagraph / stale epoch_summary / unexpected
        error) skip the processed-state mark so the next tick retries.
        """
        try:
            outcome = self._process_epoch(epoch_id)
        except StaleMetagraphError as exc:
            # Transient — metagraph hotkey->uid lookup is stale. Skip
            # marking processed so the next tick retries once the lookup
            # has refreshed.
            LOGGER.warning(
                "epoch %d deferred (stale metagraph): %s — next tick will retry",
                epoch_id,
                exc,
            )
            return None
        except StaleEpochSummaryError as exc:
            # Transient — epoch_summary.json was written by a
            # pre-chain-read finalizer and so lacks emissions_alpha. Skip
            # marking processed so the next tick retries once the
            # finalizer has republished. Invalidate the cached copy too:
            # S3Mirror._download is a no-op when the local file exists, so
            # without this the validator would keep rereading the stale
            # cached summary and defer forever.
            self._mirror.invalidate_artifact(epoch_id, EPOCH_SUMMARY_FILENAME)
            LOGGER.warning(
                "epoch %d deferred (stale epoch_summary): %s — next tick will retry",
                epoch_id,
                exc,
            )
            return None
        except Exception:
            LOGGER.exception("epoch %d processing failed", epoch_id)
            return None
        self._processed.mark(epoch_id)
        return outcome

    def _process_epoch(self, epoch_id: int) -> EpochOutcome:
        mirror_dir = self._mirror.mirror_epoch(epoch_id)

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
            try:
                self._submitter.submit(
                    netuid=self._config.bittensor_netuid,
                    uids=vector.uids,
                    weights=vector.weights,
                    epoch_id=epoch_id,
                )
            except AlreadySubmittedError as exc:
                # A previous submit for this epoch is already on chain
                # (the bittensor SDK re-broadcast after a websocket
                # reconnect). Treat the epoch as submitted and let the
                # outer loop mark it processed; retrying would re-emit
                # the same extrinsic and hit the same error.
                LOGGER.info(
                    "epoch %d: already submitted on chain (%s); marking processed",
                    epoch_id,
                    exc,
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
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
        )
