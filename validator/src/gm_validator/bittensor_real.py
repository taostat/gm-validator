"""Real bittensor submitter — Phase 2 stub.

The real implementation calls into ``bittensor-py`` to submit weights
to subtensor. v1 ships only the mock submitter (``bittensor_adapter``);
this stub exists so ``main.py``'s lazy import resolves and ty can
verify the surrounding code path. Constructing ``RealSubmitter`` raises
``NotImplementedError`` until Phase 2 fills in the body.
"""

from __future__ import annotations


class RealSubmitter:
    """Placeholder for the real bittensor weight submitter.

    Satisfies the ``Submitter`` Protocol from ``bittensor_adapter`` by
    exposing the matching ``submit`` signature. Construction raises
    ``NotImplementedError``; Phase 2 fills in the body that calls into
    ``bittensor-py``.
    """

    def __init__(
        self,
        netuid: int,
        endpoint: str | None,
        wallet_name: str,
        wallet_hotkey: str,
    ) -> None:
        raise NotImplementedError(
            "RealSubmitter is a Phase 2 deliverable; "
            "use MockSubmitter via bittensor_mock=true for v1."
        )

    def submit(
        self,
        *,
        netuid: int,
        uids: list[int],
        weights: list[float],
        epoch_id: int,
    ) -> None:
        raise NotImplementedError
