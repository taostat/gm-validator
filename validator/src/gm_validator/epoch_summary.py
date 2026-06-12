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
import os
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

EPOCH_SUMMARY_FILENAME = "epoch_summary.json"

DecimalString = Annotated[Decimal, PlainSerializer(lambda v: f"{v}", return_type=str)]


class EpochSummary(BaseModel):
    """The on-disk shape of ``epoch_summary.json``.

    Mirrors ``gm_epoch_finalizer.epoch_summary.EpochSummary``. Kept as a
    sibling definition rather than a cross-repo import — the gm-validator
    package must stay independent of the finalizer's Python package.

    ``emissions_alpha`` (whole alpha, full precision Decimal) is the
    per-epoch alpha emission read from chain by the finalizer (gm PR #176
    onward). It feeds directly into the validator's cap+burn pool math.
    The field is typed Optional only to tolerate epochs finalized by an
    earlier finalizer release — the validator defers them via
    :class:`scoring.StaleEpochSummaryError` instead of fabricating a
    fallback number.
    """

    model_config = ConfigDict(frozen=True)

    epoch_id: int
    finalized_at: str
    alpha_price_in_tao: DecimalString
    tao_price_usd: DecimalString
    alpha_price_usd: DecimalString
    emissions_alpha: DecimalString | None = None
    price_block_height: int
    price_alpha_source: str
    price_tao_usd_source: str
    emissions_alpha_source: str | None = None
    finalizer_version: str


def epoch_summary_path(mirror_dir: str) -> str:
    """Local path the mirror writes ``epoch_summary.json`` to."""
    return os.path.join(mirror_dir, EPOCH_SUMMARY_FILENAME)


def load_epoch_summary(path: str) -> EpochSummary:
    """Read ``epoch_summary.json``.

    Raises:
        FileNotFoundError: The file is absent. Every finalizer from
            gm-epoch-finalizer #161 onward emits this artifact; a
            missing file means an upstream regression and the epoch
            should not be scored until it's resolved.
    """
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return EpochSummary.model_validate(data)
