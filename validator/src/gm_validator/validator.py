"""Validator orchestrator.

Loop (one ``process_once`` tick):

1. Refresh the metagraph hotkey -> uid lookup when a metagraph source is
   configured, keeping the last-good lookup on transient read failures.
2. Read the chain head block and derive the open epoch
   ``E = head_block // blocks_per_epoch`` — the same derivation the
   epoch-finalizer uses. The chain head IS the discovery cursor.
3. Target the newest *closed* epoch ``E-1``. If its ``_FINALIZED`` marker
   is absent (the finalizer is lagging), walk back a small bounded
   window to find the newest finalized epoch. Each probe is a single
   targeted ``head_object`` — never a list+scan of the epoch history.
   If none in the window is finalized yet, do nothing this tick.
4. Epoch-window guard: skip if the target is ``<= _last_submitted_epoch``
   (already handled this epoch). Submitting at most once per epoch
   respects the chain's ~100-block weight-set rate limit, which is well
   inside one epoch.
5. Mirror the target's artifacts, score, and submit:
   a. Mirror artifacts locally (`aggregated.jsonl`,
      `epoch_summary.json`, `_FINALIZED`).
   b. Compute per-miner scores from `aggregated.jsonl`.
   c. Build a u16 weight vector via cap+burn — miner i gets
      ``consumed_usd_i / pool_usd``; residue routes to the subnet-owner
      uid as burn weight.
   d. Submit via the configured `Submitter`.
   e. On success, advance ``_last_submitted_epoch`` to the target.
6. Prune local mirrors older than the retention window.

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

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.bittensor_adapter import (
    ChainCursor,
    MetagraphSource,
    Submitter,
    ValidatorWeightStatus,
)
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import (
    EPOCH_SUMMARY_FILENAME,
    epoch_summary_path,
    load_epoch_summary,
)
from gm_validator.metrics import record_submit_failure, record_weight_submission
from gm_validator.s3_mirror import S3Mirror
from gm_validator.scoring import (
    MalformedArtifactError,
    StaleEpochSummaryError,
    StaleMetagraphError,
    WeightVector,
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


@dataclass
class _UidRefresh:
    """Outcome of one metagraph hotkey -> uid refresh, for the tick log."""

    entries: int
    status: str


def _format_weight_vector(vector: WeightVector) -> str:
    """Render a u16 weight vector as ``[(uid,w),…]`` for the per-epoch log.

    Flags the all-burn shape — the whole vector on the subnet-owner
    (burn) uid — so a 100%-burn epoch reads as ``all-burn [(99,65535)]``
    rather than an undifferentiated pair list.
    """
    pairs = list(zip(vector.uids, vector.weights, strict=True))
    body = "[" + ", ".join(f"({uid},{w})" for uid, w in pairs) + "]"
    if pairs == [(vector.burn_uid, MAX_WEIGHT)]:
        return f"all-burn {body}"
    return body


class Validator:
    """Top-level validator service.

    When a ``MetagraphSource`` is configured, each tick refreshes the
    hotkey -> uid lookup before selecting and scoring an epoch.
    """

    def __init__(
        self,
        config: ValidatorConfig,
        mirror: S3Mirror,
        submitter: Submitter,
        cursor: ChainCursor,
        miner_uid_lookup: dict[str, int] | None = None,
        metagraph_source: MetagraphSource | None = None,
    ) -> None:
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._cursor = cursor
        self._miner_uid_lookup = miner_uid_lookup or {}
        self._metagraph_source = metagraph_source
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
        # Last on-chain ``last_update`` block we observed for this validator.
        # Tracked across ticks so an advance — weights actually applied, i.e.
        # a commit-reveal *reveal* landing — is detectable and logged.
        self._last_update_block: int | None = None

    def process_once(self) -> list[EpochOutcome]:
        """One tick: target the newest finalized epoch and submit its weights.

        Refreshes the metagraph lookup when configured, then uses the
        chain head block as the discovery cursor — there is no S3 scan.
        Targets the newest closed epoch ``E-1``, walking back a bounded
        window if the finalizer lags, then submits at most once per open
        chain epoch (the ``_last_submitted_open_epoch`` guard). Returns
        the processed epoch's outcome, or an empty list when nothing was
        ready or this open epoch was already handled.
        """
        refresh = self._refresh_miner_uid_lookup()
        status = self._submitter.weight_status()
        self._observe_weight_status(status)
        outcomes: list[EpochOutcome] = []
        selection = self._select_target_epoch()
        if selection is not None:
            open_epoch, target = selection
            if status is not None and status.within_rate_limit_window:
                # The chain's weight-set rate limit gates the commit too, so a
                # submit now would be rejected. Skip it (rather than fire a
                # doomed commit that bumps submit_failures); the guards stay
                # unadvanced so the next tick retries once the window clears.
                LOGGER.info(
                    "epoch %d ready but only %d/%d blocks since last on-chain "
                    "update — inside weight-set rate-limit window, deferring submit",
                    target,
                    status.blocks_since_last_update,
                    status.weights_rate_limit,
                )
            elif status is not None and status.timelocked_commit_queue_full:
                # One pending timed commit in the active epoch bucket is enough
                # for this validator. More are duplicate submissions caused by
                # restarts or duplicate runners and can fill the chain's hard
                # unrevealed-commit limit, so defer until the bucket advances or
                # the commit clears.
                LOGGER.info(
                    "epoch %d ready but validator already has %d pending timed "
                    "weight commit(s) in the active bucket (limit %d) — deferring submit",
                    target,
                    status.pending_timelocked_commits,
                    status.pending_timelocked_commit_limit,
                )
            else:
                outcome = self._process_target_epoch(open_epoch, target)
                if outcome is not None:
                    outcomes.append(outcome)
        self._mirror.prune(self._config.mirror_retention_epochs)
        self._log_tick_summary(selection, outcomes, refresh)
        return outcomes

    def _observe_weight_status(self, status: ValidatorWeightStatus | None) -> None:
        """Log on-chain weight status and detect last-update advances.

        Mirrors bm's ``blocks_since_last_update`` visibility. On timed
        commit/reveal subnets, ``last_update`` can advance when the commit is
        accepted, so reveal health is tracked through the pending timed-commit
        queue instead. A None status (mock mode or a transient read failure) is
        silently ignored.
        """
        if status is None:
            return
        if not status.registered:
            LOGGER.info("validator hotkey not registered on subnet yet — cannot set weights")
            return
        LOGGER.info(
            "validator on-chain weights: %s blocks since last update (rate limit %d)",
            status.blocks_since_last_update,
            status.weights_rate_limit,
        )
        if status.pending_timelocked_commits is not None:
            LOGGER.info(
                "validator timed weight commits: %d pending (local submit limit %d)",
                status.pending_timelocked_commits,
                status.pending_timelocked_commit_limit,
            )
        latest = status.last_update_block
        if (
            self._last_update_block is not None
            and latest is not None
            and latest > self._last_update_block
        ):
            LOGGER.info(
                "validator weight status advanced on-chain: last_update advanced %d -> %d",
                self._last_update_block,
                latest,
            )
        if latest is not None:
            self._last_update_block = latest

    def _log_tick_summary(
        self,
        selection: tuple[int, int] | None,
        outcomes: list[EpochOutcome],
        refresh: _UidRefresh,
    ) -> None:
        """Emit one INFO line per tick so the loop is observable in logs."""
        target = selection[1] if selection is not None else None
        if outcomes:
            outcome = outcomes[0]
            disposition = "submitted" if outcome.weights_submitted else "scored-no-submit"
            miners = outcome.miner_count
        else:
            disposition = "deferred" if selection is not None else "idle"
            miners = 0
        LOGGER.info(
            "tick: target_epoch=%s miners=%d weights=%s uid_lookup=%d (%s)",
            target if target is not None else "none",
            miners,
            disposition,
            refresh.entries,
            refresh.status,
        )

    def _refresh_miner_uid_lookup(self) -> _UidRefresh:
        """Refresh the miner hotkey -> uid lookup from the metagraph source.

        Returns the post-refresh entry count and a status word
        (``refreshed`` / ``failed`` / ``static``) for the per-tick log.
        """
        if self._metagraph_source is None:
            return _UidRefresh(entries=len(self._miner_uid_lookup), status="static")
        try:
            lookup = self._metagraph_source.hotkeys()
        except Exception as exc:  # noqa: BLE001 — last-good keeps a transient read from zeroing all weights.
            LOGGER.warning("metagraph refresh failed: %s; keeping last-good miner uid lookup", exc)
            return _UidRefresh(entries=len(self._miner_uid_lookup), status="failed")
        self._miner_uid_lookup = lookup
        return _UidRefresh(entries=len(lookup), status="refreshed")

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
            LOGGER.info(
                "open epoch %d already submitted this window — deferring until it advances",
                open_epoch,
            )
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
            LOGGER.info(
                "epoch %d already submitted — deferring (finalizer stalled at this epoch)",
                target,
            )
            return None
        LOGGER.info(
            "weight window: open_epoch=%d target_epoch=%d last_submitted=%s",
            open_epoch,
            target,
            last_target if last_target is not None else "none",
        )
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
        except MalformedArtifactError:
            # Permanent — a structurally invalid aggregated.jsonl row will not
            # heal on the next tick. Surface it loudly with a traceback rather
            # than let it masquerade as a transient defer, then leave the cursor
            # unadvanced so a human investigates instead of silently zeroing a
            # miner's earnings.
            LOGGER.exception(
                "epoch %d has a malformed aggregated.jsonl artifact — refusing "
                "to score; this will not self-heal and needs investigation",
                epoch_id,
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

        if scores:
            LOGGER.info("processing epoch %d: %d miners with usage", epoch_id, len(scores))
        else:
            LOGGER.info("processing epoch %d: no miner usage — all-burn", epoch_id)

        epoch_summary = load_epoch_summary(epoch_summary_path(mirror_dir))

        vector = compute_weights(
            scores,
            self._miner_uid_lookup,
            epoch_summary=epoch_summary,
            subnet_owner_uid=self._config.subnet_owner_uid,
            earnings_multiplier=self._config.weight_earnings_multiplier,
        )

        LOGGER.info(
            "epoch %d weight vector: %s (pool_usd=%s consumed_usd=%s)",
            epoch_id,
            _format_weight_vector(vector),
            vector.epoch_result.pool_usd_total,
            vector.epoch_result.total_consumed_usd,
        )

        # Per-miner breakdown, mirroring the bm validator's per-UID submission
        # log. Shows each scored miner's earnings and the u16 weight it
        # actually received, so an all-burn epoch is explainable from the log
        # alone: a miner whose earnings round below the 1/65535 weight quantum
        # shows ``u16_weight=0`` here and its share falls to the burn uid.
        if scores:
            weight_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
            for hotkey, s in sorted(
                scores.items(),
                key=lambda kv: kv[1].earnings_ndollars + kv[1].surcharge_ndollars,
                reverse=True,
            ):
                uid = self._miner_uid_lookup.get(hotkey)
                u16 = weight_by_uid.get(uid, 0) if uid is not None else 0
                LOGGER.info(
                    "  miner %s.. uid=%s earned_ndollars=%d reqs=%d u16_weight=%d%s",
                    hotkey[:12],
                    uid if uid is not None else "?",
                    s.earnings_ndollars + s.surcharge_ndollars,
                    s.successful_requests,
                    u16,
                    "" if uid is not None else " (not in metagraph)",
                )

        submitted = False
        if vector.uids:
            # A submit failure raises WeightSubmissionError out of this
            # method; _process_target_epoch defers the epoch (leaves the
            # cursor unadvanced) so the next tick retries with the same
            # idempotent weight vector. The submitter logs the chain's
            # accept/reject outcome.
            try:
                self._submitter.submit(
                    netuid=self._config.bittensor_netuid,
                    uids=vector.uids,
                    weights=vector.weights,
                    epoch_id=epoch_id,
                )
            except Exception:
                record_submit_failure()
                raise
            submitted = True
            record_weight_submission(epoch_id)
        else:
            LOGGER.info("epoch %d: empty weight vector — nothing to submit", epoch_id)

        return EpochOutcome(
            epoch_id=epoch_id,
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
        )
