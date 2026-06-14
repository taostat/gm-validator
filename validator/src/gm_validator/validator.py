"""Validator orchestrator.

Loop (one ``process_once`` tick):

1. Read the chain head block and derive the open epoch
   ``E = head_block // blocks_per_epoch`` — the same derivation the
   epoch-finalizer uses. The chain head IS the discovery cursor.
2. Target the newest *closed* epoch ``E-1``. If its ``_FINALIZED`` marker
   is absent (the finalizer is lagging), walk back a small bounded
   window to find the newest finalized epoch. Each probe is a single
   targeted ``head_object`` — never a list+scan of the epoch history.
   If none in the window is finalized yet, do nothing this tick.
3. Epoch-window guard: skip if the target is ``<= _last_submitted_epoch``
   (already handled this epoch). Submitting at most once per epoch
   respects the chain's ~100-block weight-set rate limit, which is well
   inside one epoch.
4. Mirror the target's artifacts, score, and submit:
   a. Mirror artifacts locally (`aggregated.jsonl`,
      `epoch_summary.json`, `_FINALIZED`).
   b. Compute per-miner scores from `aggregated.jsonl`.
   c. Build a u16 weight vector via cap+burn — miner i gets
      ``consumed_usd_i / pool_usd``; residue routes to the subnet-owner
      uid as burn weight.
   d. Submit via the configured `Submitter`.
   e. On success, advance ``_last_submitted_epoch`` to the target.
5. Prune local mirrors older than the retention window.

On-chain weights are a current snapshot, not a per-epoch ledger: only
the latest finalized epoch is ever scored, and older un-targeted epochs
are simply never enumerated. The cursor is recomputed from the chain
every tick, so no persisted processed-state is needed — a restart at
worst re-scores the current epoch once, which is idempotent (the same
weight vector).

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

from gm_validator.bittensor_adapter import ChainCursor, Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import (
    EPOCH_SUMMARY_FILENAME,
    epoch_summary_path,
    load_epoch_summary,
)
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
        cursor: ChainCursor,
        miner_uid_lookup: dict[str, int] | None = None,
    ) -> None:
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._cursor = cursor
        self._miner_uid_lookup = miner_uid_lookup or {}
        # Two complementary guards on the submit, both recomputed from the
        # chain each tick. They are deliberately in-memory only: the
        # chain-cursor design drops the persisted processed.json, so a
        # restart resets both to None and re-scores the current target. The
        # re-submit is the identical weight vector — idempotent, exactly the
        # bm validator's no-persisted-dedup contract. If the restart lands
        # inside the chain's weight-set rate-limit window the duplicate is
        # rejected and retried each poll until the window clears (bounded by
        # one epoch), then succeeds and the guards latch; a clean accept on
        # the first try latches immediately. Either way the on-chain weight
        # vector never changes.
        #
        # - Open-epoch rate guard: at most one submit per open chain epoch
        #   (mirroring bm's `block // tempo` guard). When the finalizer
        #   catches up from lag it can publish several closed epochs while
        #   the chain head stays in one open epoch; submitting each would
        #   trip the chain's ~100-block weight-set rate limit.
        # - Target dedup guard: never re-submit a finalized epoch already
        #   submitted. When the finalizer stalls but the chain advances,
        #   the bounded walk-back keeps resolving the same older target
        #   across successive open epochs; the rate guard alone would let
        #   each new open epoch re-submit that stale vector.
        #
        # A submit needs BOTH a fresh open epoch and a newer target.
        self._last_submitted_open_epoch: int | None = None
        self._last_submitted_epoch: int | None = None

    def process_once(self) -> list[EpochOutcome]:
        """One tick: target the newest finalized epoch and submit its weights.

        The chain head block is the discovery cursor — there is no S3
        scan. Targets the newest closed epoch ``E-1``, walking back a
        bounded window if the finalizer lags, then submits at most once
        per open chain epoch (the ``_last_submitted_open_epoch`` guard).
        Returns the processed epoch's outcome, or an empty list when
        nothing was ready or this open epoch was already handled.
        """
        outcomes: list[EpochOutcome] = []
        selection = self._select_target_epoch()
        if selection is not None:
            open_epoch, target = selection
            outcome = self._process_target_epoch(open_epoch, target)
            if outcome is not None:
                outcomes.append(outcome)
        self._mirror.prune(self._config.mirror_retention_epochs)
        return outcomes

    def _select_target_epoch(self) -> tuple[int, int] | None:
        """Resolve ``(open_epoch, target)`` to score this tick, or None.

        Reads the chain head, derives the newest closed epoch ``E-1``,
        finds the newest finalized epoch within the bounded walk-back
        window, and applies both submit guards (one submit per open epoch,
        never re-submit a target already submitted). Returns None when the
        chain head is unreadable, the chain is too early to have a closed
        epoch, no probed epoch is finalized yet, this open epoch already
        submitted, or the target was already submitted.
        """
        open_epoch = self._cursor.current_epoch()
        if open_epoch is None:
            return None
        newest_closed = open_epoch - 1
        if newest_closed < 0:
            LOGGER.info("chain at epoch %d: no closed epoch yet, nothing to do", open_epoch)
            return None

        last_open = self._last_submitted_open_epoch
        if last_open is not None and open_epoch <= last_open:
            LOGGER.debug("open epoch %d already submitted this window, skipping", open_epoch)
            return None

        target = self._mirror.latest_finalized_epoch(
            newest_closed, self._config.finalized_lookback_epochs
        )
        if target is None:
            LOGGER.info(
                "no finalized epoch in [%d, %d]: finalizer still lagging, retry next tick",
                max(newest_closed - self._config.finalized_lookback_epochs, 0),
                newest_closed,
            )
            return None

        last_target = self._last_submitted_epoch
        if last_target is not None and target <= last_target:
            LOGGER.debug("epoch %d already submitted, skipping (finalizer stalled)", target)
            return None
        return open_epoch, target

    def _process_target_epoch(self, open_epoch: int, epoch_id: int) -> EpochOutcome | None:
        """Process the targeted epoch; return its outcome, or None if deferred.

        On success both guards advance (open-epoch to *open_epoch*, target
        to *epoch_id*). Deferrals (stale metagraph / stale epoch_summary /
        submit failure / unexpected error) leave the guards unchanged so
        the next tick retries the same epoch.
        """
        try:
            outcome = self._process_epoch(epoch_id)
        except StaleMetagraphError as exc:
            # Transient — metagraph hotkey->uid lookup is stale. Leave the
            # cursor unadvanced so the next tick retries once the lookup
            # has refreshed.
            LOGGER.warning(
                "epoch %d deferred (stale metagraph): %s — next tick will retry",
                epoch_id,
                exc,
            )
            return None
        except StaleEpochSummaryError as exc:
            # Transient — epoch_summary.json was written by a
            # pre-chain-read finalizer and so lacks emissions_alpha. Leave
            # the cursor unadvanced so the next tick retries once the
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
        self._last_submitted_open_epoch = open_epoch
        self._last_submitted_epoch = epoch_id
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
            earnings_multiplier=self._config.weight_earnings_multiplier,
        )

        submitted = False
        if vector.uids:
            # A submit failure raises WeightSubmissionError out of this
            # method; _process_target_epoch defers the epoch (leaves the
            # cursor unadvanced) so the next tick retries with the same
            # idempotent weight vector.
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
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
        )
