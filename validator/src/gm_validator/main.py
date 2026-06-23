"""Entry point: `python -m gm_validator.main`."""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

import boto3
import botocore
from botocore.config import Config
from prometheus_client import start_http_server

from gm_validator.bittensor_adapter import (
    ChainCursor,
    MetagraphSource,
    MockChainCursor,
    MockSubmitter,
    Submitter,
)
from gm_validator.config import ValidatorConfig
from gm_validator.metrics import record_loop_error
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator


class HotkeyNotConfiguredError(RuntimeError):
    """Real submission requested but no hotkey seed was configured."""


def _use_mock_submitter(config: ValidatorConfig) -> bool:
    """Decide between the mock and the real (chain-signing) submitter.

    The mock submitter is selected only when ``BITTENSOR_MOCK`` is set
    explicitly. With mock off, a hotkey source is mandatory — either an
    on-disk wallet (``WALLET_NAME`` + ``WALLET_HOTKEY``) or an in-memory
    ``BITTENSOR_HOTKEY_SEED``. Neither configured raises here so a pod
    that forgot to mount the secret crashes at startup instead of
    silently running forever without ever submitting on-chain weights.

    Raises:
        HotkeyNotConfiguredError: Mock mode is off and no hotkey source
            (wallet or seed) is configured.
    """
    if config.bittensor_mock:
        return True
    has_seed = bool(config.bittensor_hotkey_seed and config.bittensor_hotkey_seed.strip())
    has_wallet = bool(config.bittensor_wallet_name and config.bittensor_wallet_hotkey)
    if not (has_seed or has_wallet):
        raise HotkeyNotConfiguredError(
            "no validator hotkey configured: set WALLET_NAME and WALLET_HOTKEY to "
            "import an on-disk wallet, or BITTENSOR_HOTKEY_SEED (BIP-39 mnemonic or "
            "0x-prefixed hex), or set BITTENSOR_MOCK=1 to run without on-chain submission"
        )
    return False


def _build_submitter(config: ValidatorConfig) -> Submitter:
    if _use_mock_submitter(config):
        # Mock mode forced: record submissions in memory. Useful for
        # build-phase smoke tests.
        return MockSubmitter()
    # _use_mock_submitter guarantees a wallet or seed is configured here.
    # Lazy import so the test path does not require bittensor-py.
    from gm_validator.bittensor_real import RealSubmitter

    return RealSubmitter(
        netuid=config.bittensor_netuid,
        endpoint=config.bittensor_endpoint,
        hotkey_seed=config.bittensor_hotkey_seed,
        wallet_name=config.bittensor_wallet_name,
        wallet_hotkey=config.bittensor_wallet_hotkey,
        wallet_path=config.bittensor_wallet_path,
        connect_timeout=config.subtensor_connect_timeout_secs,
        rpc_timeout=config.subtensor_rpc_timeout_secs,
    )


def _build_cursor(config: ValidatorConfig, submitter: Submitter) -> ChainCursor:
    """Build the chain-head epoch cursor.

    Real mode wraps the ``RealSubmitter``'s long-lived connection so the
    head poll and weight submission share one websocket. Mock mode has no
    chain to read, so the cursor reports no open epoch and the validator
    targets nothing each tick — a deliberate idle loop for a build-phase
    smoke run, logged loudly so it is never mistaken for a stuck chain.
    """
    from gm_validator.bittensor_real import RealChainCursor, RealSubmitter

    if isinstance(submitter, RealSubmitter):
        return RealChainCursor(submitter, config.blocks_per_epoch)
    logging.getLogger(__name__).warning(
        "BITTENSOR_MOCK set: chain cursor is idle — the validator will mirror and "
        "prune but never target an epoch or submit weights. Set BITTENSOR_MOCK=0 to "
        "enable chain-driven epoch discovery."
    )
    return MockChainCursor(epoch=None)


def _build_miner_uid_lookup(config: ValidatorConfig, submitter: Submitter) -> dict[str, int]:
    """Build the hotkey -> uid lookup from the subnet metagraph.

    Reads the metagraph over the ``RealSubmitter``'s already-open socket
    rather than dialing a second connection — a second rapid websocket to
    the public testnet endpoint is what stalled startup. Mock mode has no
    chain to query, so it returns an empty mapping.
    """
    from gm_validator.bittensor_real import RealSubmitter

    if not isinstance(submitter, RealSubmitter):
        return {}
    lookup = submitter.metagraph_hotkeys(config.bittensor_netuid)
    logging.getLogger(__name__).info(
        "metagraph netuid=%d: loaded %d hotkey->uid entries",
        config.bittensor_netuid,
        len(lookup),
    )
    return lookup


def _build_metagraph_source(
    config: ValidatorConfig, submitter: Submitter
) -> MetagraphSource | None:
    """Build the per-tick metagraph source when running against real subtensor.

    Real mode wraps the ``RealSubmitter``'s already-open socket so lookup
    refreshes do not dial a second connection. Mock mode keeps the injected
    static lookup and skips per-tick metagraph reads.
    """
    from gm_validator.bittensor_real import RealMetagraphSource, RealSubmitter

    if not isinstance(submitter, RealSubmitter):
        return None
    return RealMetagraphSource(submitter, config.bittensor_netuid)


def _build_s3_client(config: ValidatorConfig) -> Any:
    """Build the boto3 S3 client.

    The client always carries a botocore ``Config`` pinning
    ``request_checksum_calculation`` / ``response_checksum_validation``
    to ``when_required``. botocore >=1.36 defaults these to
    ``when_supported``, which attaches CRC32 checksum headers to every
    request; S3-compatible providers such as OVH Object Storage reject
    those with an ``InvalidRequest`` error on operations like
    ``ListObjectsV2``. ``when_required`` only sends a checksum when the
    operation actually mandates one, which AWS S3 accepts as well.

    When ``config.s3_anonymous`` is set the client also signs no requests
    (``botocore.UNSIGNED``) — required for OVH public-read buckets or any
    bucket reachable without IAM credentials — and switches to
    ``virtual``-hosted addressing (``bucket.endpoint/key``). OVH serves
    public-read objects only via the virtual-hosted host; an *unsigned*
    path-style request (``endpoint/bucket/key``) is rejected with HTTP 400
    regardless of the object ACL. Signed (keyed) reads work either way, so
    only the anonymous path needs the switch.

    boto3-stubs types ``boto3.client()`` as an overload set keyed on the
    Literal service name; passing the remaining args via ``**kwargs``
    erases their types and falls outside every overload. The two
    ``endpoint_url`` cases are therefore spelled out with explicit named
    args.
    """
    client_config = Config(
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        signature_version=botocore.UNSIGNED if config.s3_anonymous else None,
        s3={"addressing_style": "virtual"} if config.s3_anonymous else {},
    )
    if config.s3_endpoint_url:
        return boto3.client(
            "s3",
            region_name=config.aws_region,
            endpoint_url=config.s3_endpoint_url,
            config=client_config,
        )
    return boto3.client("s3", region_name=config.aws_region, config=client_config)


def _run(config: ValidatorConfig) -> None:
    s3 = _build_s3_client(config)
    mirror = S3Mirror(
        s3,
        config.s3_bucket,
        config.s3_prefix,
        config.local_mirror_dir,
        anonymous=config.s3_anonymous,
    )
    submitter = _build_submitter(config)
    cursor = _build_cursor(config, submitter)
    miner_uid_lookup = _build_miner_uid_lookup(config, submitter)
    metagraph_source = _build_metagraph_source(config, submitter)

    validator = Validator(
        config,
        mirror,
        submitter,
        cursor,
        miner_uid_lookup=miner_uid_lookup,
        metagraph_source=metagraph_source,
    )

    stop = False

    def _on_signal(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while not stop:
        try:
            validator.process_once()
        except Exception:
            record_loop_error()
            logging.getLogger(__name__).exception("validator loop tick failed")
        time.sleep(config.poll_interval_secs)


def _configure_logging() -> None:
    """Install root handlers and pin the app logger to INFO.

    ``logging.basicConfig`` is a no-op once any handler exists, and the
    bittensor SDK installs its own at import time ("Enabling default
    logging (Warning level)") — so relying on basicConfig alone would
    leave the validator's INFO lines suppressed.

    bittensor's import also reaches into the ``gm_validator`` logger and
    raises its level to CRITICAL. bittensor is otherwise imported lazily
    (inside ``_build_submitter``), so a setLevel pin done before that
    import is silently clobbered and every per-tick / per-epoch INFO line
    is dropped. Force the import here so the clobber happens first, then
    pin — our level wins and the lines stay visible.

    Crucially, bittensor sets that CRITICAL level on *every* ``gm_validator``
    logger that already exists at import time — including child loggers such
    as ``gm_validator.validator``, which main.py imports (``from
    gm_validator.validator import Validator``) before this runs. A child's
    explicit level overrides its parent, so pinning only the ``gm_validator``
    parent leaves ``gm_validator.validator`` stuck at CRITICAL and silences
    the entire per-tick / per-epoch / submit-failure stream while
    ``gm_validator.bittensor_real`` (imported lazily, *after* the clobber, so
    never explicitly set) still logs. Reset every gm_validator child back to
    NOTSET so it inherits the parent, then pin the parent to INFO.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    import bittensor as _  # noqa: F401

    for name in list(logging.root.manager.loggerDict):
        if name == "gm_validator" or name.startswith("gm_validator."):
            logging.getLogger(name).setLevel(logging.NOTSET)
    logging.getLogger("gm_validator").setLevel(logging.INFO)


def main() -> None:
    _configure_logging()
    config = ValidatorConfig.from_env()
    if config.metrics_bind is not None:
        host, port = config.metrics_bind
        start_http_server(port, addr=host)
        logging.getLogger(__name__).info("metrics server listening on %s:%d", host, port)
    else:
        logging.getLogger(__name__).info(
            "GM_VALIDATOR_METRICS_BIND unset: no metrics server started"
        )
    _run(config)


if __name__ == "__main__":
    main()
