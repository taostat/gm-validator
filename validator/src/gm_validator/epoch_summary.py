"""Reader for the finalizer's per-epoch price snapshot.

The finalizer (gm/epoch-finalizer, PR #161 onward) writes
``epoch_summary.json`` next to ``aggregated.jsonl``. The validator reads
it for the alpha USD price at the epoch-close block — no live oracle
fetch needed, every validator sees identical inputs for the same epoch.

Decimals are stored as JSON strings to preserve full precision; this
module parses them back into :class:`Decimal`.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

LOGGER = logging.getLogger(__name__)

EPOCH_SUMMARY_FILENAME = "epoch_summary.json"

DecimalString = Annotated[Decimal, PlainSerializer(lambda v: f"{v}", return_type=str)]


class EpochSummary(BaseModel):
    """The on-disk shape of ``epoch_summary.json``.

    Mirrors ``gm_epoch_finalizer.epoch_summary.EpochSummary``. Kept as a
    sibling definition rather than a cross-repo import — the gm-validator
    package must stay independent of the finalizer's Python package.
    """

    model_config = ConfigDict(frozen=True)

    epoch_id: int
    finalized_at: str
    alpha_price_in_tao: DecimalString
    tao_price_usd: DecimalString
    alpha_price_usd: DecimalString
    price_block_height: int
    price_alpha_source: str
    price_tao_usd_source: str
    finalizer_version: str


def epoch_summary_path(mirror_dir: str) -> str:
    """Local path the mirror writes ``epoch_summary.json`` to."""
    return os.path.join(mirror_dir, EPOCH_SUMMARY_FILENAME)


def load_epoch_summary(path: str) -> EpochSummary | None:
    """Read ``epoch_summary.json``; return None if the file is absent.

    A missing file is the legacy-epoch signal — finalizers that pre-date
    PR #161 do not emit this artifact. The validator falls back to the
    naive scoring path on ``None``.
    """
    if not os.path.exists(path):
        LOGGER.warning("epoch_summary.json not present at %s — legacy epoch", path)
        return None
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return EpochSummary.model_validate(data)
