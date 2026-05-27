"""Validator orchestrator.

Loop:

1. Discover finalized epoch ids in S3.
2. For each not yet processed:
   a. Mirror artifacts locally.
   b. Invoke `gm-verifier verify`. On failure: alert + skip submission.
   c. Compute per-miner scores from `aggregated.jsonl`.
   d. Resolve current alpha price, apply emission cap, normalise.
   e. Submit via the configured `Submitter`.
   f. Mark the epoch processed.
3. Prune local mirrors older than the retention window.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from gm_validator.alpha_oracle import AlphaPriceOracle
from gm_validator.bittensor_adapter import Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.processed_state import ProcessedState
from gm_validator.s3_mirror import S3Mirror
from gm_validator.scoring import (
    EpochCapSummary,
    aggregated_path,
    apply_emission_cap,
    load_aggregated,
    score,
)
from gm_validator.verifier import VerifierResult, verify_epoch

LOGGER = logging.getLogger(__name__)

AlphaPriceFetcher = Callable[[], Coroutine[Any, Any, Decimal]]


@dataclass
class EpochOutcome:
    """Per-epoch processing result, surfaced for tests + metrics."""

    epoch_id: int
    verifier_ok: bool
    weights_submitted: bool
    miner_count: int
    total_ndollars: int
    burn_fraction: float = 0.0


class Validator:
    """Top-level validator service."""

    def __init__(
        self,
        config: ValidatorConfig,
        mirror: S3Mirror,
        submitter: Submitter,
        miner_uid_lookup: dict[str, int] | None = None,
        processed_state: ProcessedState | None = None,
        alpha_price_fetcher: AlphaPriceFetcher | None = None,
    ) -> None:
        """Wire dependencies.

        Args:
            config: Resolved env config.
            mirror: S3 → local artifact mirror.
            submitter: Bittensor weight submission backend.
            miner_uid_lookup: Hotkey → uid map (empty when no chain).
            processed_state: Durable processed-epoch state; defaults to
                the configured state-file path.
            alpha_price_fetcher: Override the default Taostats oracle.
                Tests inject a stub; production builds one from the
                configured override / API credentials.
        """
        self._config = config
        self._mirror = mirror
        self._submitter = submitter
        self._miner_uid_lookup = miner_uid_lookup or {}
        self._processed = processed_state or ProcessedState(config.processed_state_path)
        self._fetch_alpha_price_usd = alpha_price_fetcher or _default_alpha_price_fetcher(config)

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
            )

        rows = load_aggregated(aggregated_path(mirror_dir))
        scores = score(rows)
        total = sum(s.earnings_ndollars + s.surcharge_ndollars for s in scores.values())

        emissions_alpha = self._config.epoch_alpha_emission_override
        if emissions_alpha is None:
            # PR 1 sources epoch emission from config. PR 2 will pull it
            # from chain state (dynamic_info.alpha_out_emission × tempo).
            # Submitting without a cap would be silently incorrect, so we
            # skip submission entirely until the operator wires it in.
            LOGGER.error(
                "epoch %d: EPOCH_ALPHA_EMISSION_OVERRIDE unset; cannot apply emission cap, "
                "skipping submission",
                epoch_id,
            )
            return EpochOutcome(
                epoch_id=epoch_id,
                verifier_ok=True,
                weights_submitted=False,
                miner_count=len(scores),
                total_ndollars=total,
            )

        try:
            alpha_price_usd = asyncio.run(self._fetch_alpha_price_usd())
        except Exception:
            LOGGER.exception("epoch %d: alpha price oracle failed; skipping submission", epoch_id)
            return EpochOutcome(
                epoch_id=epoch_id,
                verifier_ok=True,
                weights_submitted=False,
                miner_count=len(scores),
                total_ndollars=total,
            )

        summary = apply_emission_cap(
            scores,
            alpha_price_usd=alpha_price_usd,
            emissions_alpha=emissions_alpha,
            miner_emission_pct=self._config.miner_emission_pct,
        )

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
        self._log_burn_visibility(epoch_id, summary, submitted_uids=len(uids))

        return EpochOutcome(
            epoch_id=epoch_id,
            verifier_ok=True,
            weights_submitted=submitted,
            miner_count=len(scores),
            total_ndollars=total,
            burn_fraction=summary.burn_fraction,
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

    def _log_burn_visibility(
        self,
        epoch_id: int,
        summary: EpochCapSummary,
        *,
        submitted_uids: int,
    ) -> None:
        """One structured log line per epoch so PR 2 has a paper trail.

        The submitted weight vector currently sums to ``1 - burn_fraction``
        when scale < 1 or there are unsubmitted miners. PR 2 will route
        ``burn_fraction`` to the subnet-owner UID so the on-chain vector
        once again sums to 1.0.
        """
        LOGGER.info(
            "epoch %d cap summary: scale=%s submitted_weight_sum=%.6f "
            "burn_fraction=%.6f submitted_uids=%d (PR 2 will route burn to owner UID)",
            epoch_id,
            summary.scale,
            summary.submitted_weight_sum,
            summary.burn_fraction,
            submitted_uids,
        )


def _default_alpha_price_fetcher(config: ValidatorConfig) -> AlphaPriceFetcher:
    """Build the production alpha-price oracle as a per-call coroutine.

    A fresh ``AlphaPriceOracle`` is constructed each call so the
    underlying ``httpx.AsyncClient`` lives only for one ``asyncio.run``
    invocation — this avoids "Event loop is closed" errors when the
    validator tick loop creates and tears down event loops between
    epochs.
    """

    async def fetcher() -> Decimal:
        oracle = AlphaPriceOracle(
            netuid=config.bittensor_netuid,
            api_key=config.taostats_api_key,
            base_url=config.taostats_api_url,
            override_usd=config.alpha_price_override_usd,
        )
        try:
            return await oracle.get_alpha_price_usd()
        finally:
            await oracle.close()

    return fetcher
