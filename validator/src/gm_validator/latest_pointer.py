"""Validated shape of the finalizer's latest-epoch pointer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class LatestFinalizedPointer(BaseModel):
    """Immutable pointer identifying the newest finalized epoch and its block geometry."""

    model_config = ConfigDict(frozen=True)

    schema_version: StrictInt = Field(alias="schema", ge=1, le=1)
    epoch_id: StrictInt = Field(ge=0)
    start_block: StrictInt = Field(ge=0)
    close_block: StrictInt = Field(ge=0)
    next_close_block: StrictInt = Field(ge=0)
    finalized_at: StrictStr

    @model_validator(mode="after")
    def validate_contract(self) -> LatestFinalizedPointer:
        """Reject impossible block ranges."""
        if not self.start_block <= self.close_block < self.next_close_block:
            raise ValueError("expected start_block <= close_block < next_close_block")
        return self
