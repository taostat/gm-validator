"""Tests for the scoring → u16 weight vector pipeline."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.epoch_summary import EpochSummary
from gm_validator.scoring import (
    StaleEpochSummaryError,
    StaleMetagraphError,
    compute_weights,
    score,
)


def _summary(
    alpha_price_usd: str = "0.50",
    *,
    emissions_alpha: str | None = "50",
) -> EpochSummary:
    payload: dict[str, object] = {
        "epoch_id": 1,
        "finalized_at": "2026-05-27T12:00:00Z",
        "alpha_price_in_tao": "0.05",
        "tao_price_usd": "10",
        "alpha_price_usd": alpha_price_usd,
        "price_block_height": 1,
        "price_alpha_source": "chain",
        "price_tao_usd_source": "taostats",
        "finalizer_version": "test",
    }
    if emissions_alpha is not None:
        payload["emissions_alpha"] = emissions_alpha
        payload["emissions_alpha_source"] = "chain"
    return EpochSummary.model_validate(payload)


def _row(miner_id: str, earnings_ndollars: int) -> dict:
    return {
        "epoch_id": 1,
        "miner_id": miner_id,
        "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "totals": {"input_tokens": 0, "output_tokens": 0},
        "earnings_ndollars": str(earnings_ndollars),
        "surcharge_ndollars": "0",
        "successful_requests": 1,
        "failed_requests": 0,
        "raw_record_count": 1,
        "raw_hash": "0" * 64,
    }


def test_undersubscribed_routes_residue_to_burn_uid() -> None:
    # 3 miners, $9 of demand against a $10.25 pool — under-subscribed.
    # ndollars per miner: 5, 3, 1 (×10^9).
    rows = [
        _row("A", 5_000_000_000),
        _row("B", 3_000_000_000),
        _row("C", 1_000_000_000),
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2, "C": 3},
        epoch_summary=_summary("0.50", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.burn_uid == 99
    assert 99 in vector.uids
    assert sum(vector.weights) == MAX_WEIGHT
    # Burn slice ~ (1 - 9/10.25) of MAX_WEIGHT ≈ 0.122 * 65535 ≈ 7992.
    burn_w = dict(zip(vector.uids, vector.weights, strict=True))[99]
    assert burn_w > 6000
    assert burn_w < 10000


def test_oversubscribed_renorms_no_burn_slice() -> None:
    """Demand > pool → normalize_weights renorms; burn drops to
    floor-rounding dust."""
    rows = [
        _row("A", 10_000_000_000),  # $10
        _row("B", 5_000_000_000),  # $5
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        epoch_summary=_summary("0.50", emissions_alpha="50"),  # pool = $10.25 < $15
        subnet_owner_uid=99,
    )
    assert sum(vector.weights) == MAX_WEIGHT
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    burn_w = weights_by_uid.get(99, 0)
    assert burn_w < 5  # only dust, if any


def test_zero_consumption_all_to_burn() -> None:
    rows = [_row("A", 0), _row("B", 0)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        epoch_summary=_summary("0.50", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.uids == [99]
    assert vector.weights == [MAX_WEIGHT]


def test_miner_outside_lookup_dropped() -> None:
    rows = [_row("A", 700_000_000), _row("UNKNOWN", 300_000_000)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1},
        epoch_summary=_summary("0.50", emissions_alpha="100"),
        subnet_owner_uid=99,
    )
    assert "UNKNOWN" not in (str(u) for u in vector.uids)
    assert sum(vector.weights) == MAX_WEIGHT


def test_all_miners_missing_uid_raises_stale_metagraph() -> None:
    """All scored miners absent from the uid lookup => StaleMetagraphError.

    process_once() catches this and defers the epoch without marking it
    processed — a silent empty-vector return would get marked processed
    and the epoch would be lost.
    """
    rows = [_row("A", 1_000_000_000), _row("B", 2_000_000_000)]
    scores = score(rows)
    with pytest.raises(StaleMetagraphError) as exc_info:
        compute_weights(
            scores,
            miner_uid_lookup={},  # neither miner known
            epoch_summary=_summary("0.50", emissions_alpha="100"),
            subnet_owner_uid=99,
        )
    message = str(exc_info.value)
    assert "2 scored miners missing" in message
    assert "A" in message
    assert "B" in message


def test_partial_missing_uid_still_counted_against_pool() -> None:
    """Unknown miners' demand still suppresses known miners' share.

    2 known miners ($5, $3) + 1 unknown ($2). Total demand = $10 against
    a $5.125 pool (50 alpha × 0.41 × $0.25). Per-miner weight =
    consumed / pool_usd, then normalize_weights renorms because the sum
    > 1. The unknown's $2 is dropped from the submitted vector but its
    weight contribution still inflates the renorm denominator, so A and
    B's u16 weights are smaller than they would be if UNKNOWN were
    ignored upstream.
    """
    rows = [
        _row("A", 5_000_000_000),  # $5
        _row("B", 3_000_000_000),  # $3
        _row("UNKNOWN", 2_000_000_000),  # $2
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},  # UNKNOWN absent
        # pool = 50 * 0.41 * 0.25 = $5.125
        epoch_summary=_summary("0.25", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.burn_uid == 99
    assert sum(vector.weights) == MAX_WEIGHT
    assert "UNKNOWN" not in {str(u) for u in vector.uids}

    # All three miners contribute to total_consumed_usd; the missing one
    # is silently dropped from the submission but counted in the pool
    # math.
    assert vector.epoch_result.total_consumed_usd == Decimal("10")

    # A:B u16 ratio matches their consumed ratio (5:3) to within
    # floor-rounding dust.
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    assert abs((weights_by_uid[1] / max(weights_by_uid[2], 1)) - (5 / 3)) < 0.01


def test_missing_emissions_alpha_raises_stale_epoch_summary() -> None:
    """epoch_summary.json without emissions_alpha => StaleEpochSummaryError.

    A pre-chain-read finalizer wrote the artifact; the validator defers
    rather than inventing a pool denominator. process_once() catches this
    and skips the processed-state mark so the next tick retries once
    the finalizer republishes.
    """
    rows = [_row("A", 1_000_000_000)]
    scores = score(rows)
    with pytest.raises(StaleEpochSummaryError) as exc_info:
        compute_weights(
            scores,
            miner_uid_lookup={"A": 1},
            epoch_summary=_summary("0.50", emissions_alpha=None),
            subnet_owner_uid=99,
        )
    message = str(exc_info.value)
    assert "emissions_alpha" in message
    assert "epoch 1" in message
