"""Validator orchestrator.

Loop (one ``process_once`` tick):

1. Refresh the metagraph hotkey -> uid lookup when a metagraph source is
   configured, keeping the last-good lookup on transient read failures.
2. Read the chain head and, when available, the ``finalized/latest.json`` pointer.
   The pointer supplies the epoch id and block geometry; the head determines
   whether that epoch has closed and whether the pointer is stale.
3. Skip an epoch that is not closed by this chain view or whose next epoch
   has closed without finalization. The target guard prevents duplicate
   submissions while the pointer remains on the same epoch.
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
are simply never enumerated. Each tick with a head fetches the pointer fresh. The
submit guard is in memory only, so a restart can re-submit the same epoch
with the identical vector; the rate-limit pre-gate defers it while needed.

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

from pydantic import ValidationError

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
    EpochSummary,
    epoch_summary_path,
    load_epoch_summary,
)
from gm_validator.latest_pointer import LatestFinalizedPointer
from gm_validator.metrics import (
    record_epoch_geometry_mismatch,
    record_latest_pointer_error,
    record_submit_failure,
    record_weight_submission,
)
from gm_validator.s3_mirror import LatestPointerUnavailable, S3Mirror
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
_EPOCH_GEOMETRY_FIELDS = ("epoch_id", "start_block", "close_block", "next_close_block")


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


class EpochGeometryMismatch(Exception):
    """Mirrored summary identity or geometry does not match the latest pointer."""


class InvalidEpochSummary(Exception):
    """The mirrored epoch summary cannot be parsed or validated."""


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
        *,
        metagraph_source: MetagraphSource | None = None,
    ) -> None:
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._cursor = cursor
        self._miner_uid_lookup = miner_uid_lookup or {}
        self._metagraph_source = metagraph_source
        # In-memory only: a restart can re-submit the identical vector for
        # this epoch, with the rate-limit pre-gate deferring it when needed.
        self._last_submitted_epoch: int | None = None
        # Last on-chain ``last_update`` block we observed for this validator.
        # Tracked across ticks to detect committed/applied weights updates.
        self._last_update_block: int | None = None

    def process_once(self) -> list[EpochOutcome]:
        """Process one pointer-driven epoch tick, then prune local mirrors."""
        refresh = self._refresh_miner_uid_lookup()
        status = self._submitter.weight_status()
        self._observe_weight_status(status)
        outcomes: list[EpochOutcome] = []
        selection = self._select_target_epoch()
        if selection is not None and not self._rate_limit_defers(selection, status):
            outcome = self._process_target_epoch(selection)
            if outcome is not None:
                outcomes.append(outcome)
        self._mirror.prune(self._config.mirror_retention_epochs)
        self._log_tick_summary(selection, outcomes, refresh)
        return outcomes

    def _observe_weight_status(self, status: ValidatorWeightStatus | None) -> None:
        """Log weight status and detect a committed/applied on-chain update."""
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
        latest = status.last_update_block
        if (
            self._last_update_block is not None
            and latest is not None
            and latest > self._last_update_block
        ):
            LOGGER.info(
                "validator weights committed/applied on-chain (last_update advanced %d -> %d)",
                self._last_update_block,
                latest,
            )
        if latest is not None:
            self._last_update_block = latest

    def _log_tick_summary(
        self,
        selection: LatestFinalizedPointer | None,
        outcomes: list[EpochOutcome],
        refresh: _UidRefresh,
    ) -> None:
        """Emit one INFO line per tick so the loop is observable in logs."""
        target = selection.epoch_id if selection is not None else None
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
        except Exception as exc:  # noqa: BLE001 — keep last-good weights on read failures.
            LOGGER.warning("metagraph refresh failed: %s; keeping last-good miner uid lookup", exc)
            return _UidRefresh(entries=len(self._miner_uid_lookup), status="failed")
        self._miner_uid_lookup = lookup
        return _UidRefresh(entries=len(lookup), status="refreshed")

    def _select_target_epoch(self) -> LatestFinalizedPointer | None:
        """Read latest.json and select its epoch only when chain geometry permits."""
        head = self._cursor.head_block()
        if head is None:
            return None
        try:
            pointer = self._mirror.read_latest_pointer()
        except LatestPointerUnavailable as exc:
            LOGGER.warning("latest.json %s: %s", exc.reason, exc.detail)
            record_latest_pointer_error(exc.reason)
            return None
        if head > pointer.next_close_block:
            LOGGER.info(
                "latest pointer is stale at epoch %d; waiting for epoch %d to finalize",
                pointer.epoch_id,
                pointer.epoch_id + 1,
            )
            return None
        if head <= pointer.close_block:
            LOGGER.info(
                "chain head %d has not passed epoch %d close block %d",
                head,
                pointer.epoch_id,
                pointer.close_block,
            )
            return None
        if (
            self._last_submitted_epoch is not None
            and pointer.epoch_id <= self._last_submitted_epoch
        ):
            LOGGER.info(
                "epoch %d already submitted — waiting for the finalized pointer to advance",
                pointer.epoch_id,
            )
            return None
        LOGGER.info(
            "weight window: target_epoch=%d head_block=%d last_submitted=%s",
            pointer.epoch_id,
            head,
            self._last_submitted_epoch if self._last_submitted_epoch is not None else "none",
        )
        return pointer

    @staticmethod
    def _rate_limit_defers(
        pointer: LatestFinalizedPointer,
        status: ValidatorWeightStatus | None,
    ) -> bool:
        """Return whether the chain status says this epoch must wait before submit."""
        if status is None or not status.within_rate_limit_window:
            return False
        LOGGER.info(
            "epoch %d ready but only %d/%d blocks since last on-chain update — "
            "inside weight-set rate-limit window, deferring submit",
            pointer.epoch_id,
            status.blocks_since_last_update,
            status.weights_rate_limit,
        )
        return True

    def _process_target_epoch(self, pointer: LatestFinalizedPointer) -> EpochOutcome | None:
        """Process the targeted epoch; return its outcome, or None if deferred."""
        epoch_id = pointer.epoch_id
        try:
            outcome = self._process_epoch(pointer)
        except EpochGeometryMismatch as exc:
            self._mirror.invalidate_artifact(epoch_id, EPOCH_SUMMARY_FILENAME)
            record_epoch_geometry_mismatch()
            LOGGER.error("epoch %d summary disagrees with latest.json: %s", epoch_id, exc)
            return None
        except InvalidEpochSummary:
            self._mirror.invalidate_artifact(epoch_id, EPOCH_SUMMARY_FILENAME)
            LOGGER.exception("epoch %d has an invalid epoch_summary.json", epoch_id)
            return None
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
        self._last_submitted_epoch = epoch_id
        return outcome

    def _verified_summary(self, pointer: LatestFinalizedPointer, mirror_dir: str) -> EpochSummary:
        try:
            epoch_summary = load_epoch_summary(epoch_summary_path(mirror_dir))
        except ValidationError as exc:
            if any(
                error["loc"] and error["loc"][0] in _EPOCH_GEOMETRY_FIELDS for error in exc.errors()
            ):
                raise EpochGeometryMismatch(f"invalid summary geometry: {exc}") from exc
            raise InvalidEpochSummary(str(exc)) from exc
        except ValueError as exc:
            raise InvalidEpochSummary(str(exc)) from exc
        summary_geometry = tuple(getattr(epoch_summary, field) for field in _EPOCH_GEOMETRY_FIELDS)
        pointer_geometry = tuple(getattr(pointer, field) for field in _EPOCH_GEOMETRY_FIELDS)
        if summary_geometry != pointer_geometry:
            raise EpochGeometryMismatch(f"summary={summary_geometry}, pointer={pointer_geometry}")
        return epoch_summary

    @staticmethod
    def _verified_rows(epoch_id: int, mirror_dir: str) -> list[dict]:
        rows = load_aggregated(aggregated_path(mirror_dir))
        for row in rows:
            row_epoch = row.get("epoch_id")
            if type(row_epoch) is not int or row_epoch != epoch_id:
                raise MalformedArtifactError(
                    f"aggregated.jsonl row epoch_id={row_epoch!r} does not match epoch {epoch_id}"
                )
        return rows

    def _process_epoch(self, pointer: LatestFinalizedPointer) -> EpochOutcome:
        epoch_id = pointer.epoch_id
        mirror_dir = self._mirror.mirror_epoch(epoch_id)
        epoch_summary = self._verified_summary(pointer, mirror_dir)

        rows = self._verified_rows(epoch_id, mirror_dir)
        scores = score(rows)
        total = sum(s.earnings_ndollars + s.surcharge_ndollars for s in scores.values())

        if scores:
            LOGGER.info("processing epoch %d: %d miners with usage", epoch_id, len(scores))
        else:
            LOGGER.info("processing epoch %d: no miner usage — all-burn", epoch_id)

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
