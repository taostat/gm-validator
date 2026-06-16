"""Prometheus metrics for the validator.

Weight-staleness signal for monitoring (gm#327): the epoch and wall-clock
time of the last successful ``set_weights`` submission. An alert that
watches ``time() - gm_validator_last_weight_submission_timestamp_seconds``
exceed an epoch's expected wall-clock duration knows the validator has
stopped setting weights even while the process stays alive.
"""

from __future__ import annotations

import time

from prometheus_client import Gauge

LAST_WEIGHT_EPOCH = Gauge(
    "gm_validator_last_weight_epoch",
    "Epoch id of the most recent successful set_weights submission.",
)
LAST_WEIGHT_TIMESTAMP = Gauge(
    "gm_validator_last_weight_submission_timestamp_seconds",
    "Unix time of the most recent successful set_weights submission.",
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
