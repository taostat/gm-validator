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


def test_configure_logging_unmutes_child_loggers() -> None:
    """bittensor clobbers child loggers (e.g. ``gm_validator.validator``,
    imported before bittensor) to CRITICAL too. A child's explicit level
    overrides the parent, so pinning only the parent leaves the child muted
    and drops every per-tick / per-epoch / submit-failure line. The child
    must end up effectively at INFO.
    """
    child = logging.getLogger("gm_validator.validator")
    child.setLevel(logging.CRITICAL)

    _configure_logging()

    # NOTSET on the child + INFO on the parent => INFO inherited.
    assert child.getEffectiveLevel() == logging.INFO
    assert child.isEnabledFor(logging.INFO)
    assert child.isEnabledFor(logging.ERROR)
