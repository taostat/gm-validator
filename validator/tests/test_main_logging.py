"""Logging-level pin in ``main._configure_logging``.

Importing bittensor reaches into the ``gm_validator`` logger and raises
its level to CRITICAL. bittensor is imported lazily during the run, so a
pin applied before that import is silently clobbered and every per-tick /
per-epoch INFO line is dropped. ``_configure_logging`` forces the import
first, then pins, so the level survives.
"""

from __future__ import annotations

import logging

from gm_validator.main import _configure_logging


def test_configure_logging_pins_info_after_bittensor_clobber() -> None:
    """Simulate bittensor having raised the logger to CRITICAL; the pin
    in ``_configure_logging`` must restore INFO."""
    logging.getLogger("gm_validator").setLevel(logging.CRITICAL)

    _configure_logging()

    assert logging.getLogger("gm_validator").level == logging.INFO
