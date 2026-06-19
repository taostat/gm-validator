"""Prometheus metrics for the validator.

Weight-staleness signal for monitoring (gm#327): the epoch and wall-clock
time of the last successful ``set_weights`` submission. An alert that
watches ``time() - gm_validator_last_weight_submission_timestamp_seconds``
exceed an epoch's expected wall-clock duration knows the validator has
stopped setting weights even while the process stays alive.

Payment-accuracy gauges (gm#376): observation-only signals quantifying how
faithfully the u16 quantization reproduces each miner's intended pool share.
They are derived from the intended-vs-submitted weights inside
:func:`gm_validator.alpha_economics.normalize_weights` and never feed back
into the submitted vector.
"""

from __future__ import annotations

import time

from prometheus_client import Counter, Gauge

LAST_WEIGHT_EPOCH = Gauge(
    "gm_validator_last_weight_epoch",
    "Epoch id of the most recent successful set_weights submission.",
)
LAST_WEIGHT_TIMESTAMP = Gauge(
    "gm_validator_last_weight_submission_timestamp_seconds",
    "Unix time of the most recent successful set_weights submission.",
)

WEIGHT_QUANTIZATION_RESIDUAL = Gauge(
    "gm_validator_weight_quantization_residual",
    "L1 distance between intended pool shares and submitted u16 shares, "
    "summed over every scored miner in the submitted vector.",
)
MINERS_BELOW_QUANTUM = Gauge(
    "gm_validator_miners_below_quantum",
    "Count of positively-scored miners whose intended share is below the 1/65535 u16 quantum.",
)
FLOORED_WEIGHT_TOTAL = Gauge(
    "gm_validator_floored_weight_total",
    "Signed u16 weight manufactured by flooring sub-quantum miners up "
    "(positive = overpay) versus dropping them to zero (negative = underpay).",
)

SUBMIT_FAILURES = Counter(
    "gm_validator_submit_failures_total",
    "Count of set_weights submission attempts that raised an error.",
)
LOOP_ERRORS = Counter(
    "gm_validator_loop_errors_total",
    "Count of validator loop ticks that aborted on an unhandled exception.",
)

_highest_submitted = -1


def record_weight_submission(epoch_id: int) -> None:
    """Advance the staleness gauges after a successful weight submission.

    ``LAST_WEIGHT_EPOCH`` only ever moves forward: the validator never
    re-submits an older epoch than one already submitted, but guarding
    here keeps the gauge monotonic regardless.
    """
    global _highest_submitted
    if epoch_id > _highest_submitted:
        _highest_submitted = epoch_id
        LAST_WEIGHT_EPOCH.set(epoch_id)
    LAST_WEIGHT_TIMESTAMP.set(time.time())


def record_submit_failure() -> None:
    """Increment the counter for a failed ``set_weights`` submission."""
    SUBMIT_FAILURES.inc()


def record_loop_error() -> None:
    """Increment the counter for a validator loop tick that aborted on error."""
    LOOP_ERRORS.inc()


def record_payment_accuracy(
    *,
    quantization_residual: float,
    miners_below_quantum: int,
    floored_weight_total: float,
) -> None:
    """Publish the observation-only payment-accuracy gauges for one epoch.

    Called from :func:`gm_validator.alpha_economics.normalize_weights` with
    values derived from the intended-vs-submitted weight comparison. Strictly
    a sink — it reads nothing back into the weight vector.
    """
    WEIGHT_QUANTIZATION_RESIDUAL.set(quantization_residual)
    MINERS_BELOW_QUANTUM.set(miners_below_quantum)
    FLOORED_WEIGHT_TOTAL.set(floored_weight_total)
