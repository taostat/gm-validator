"""Entry point: `python -m gm_validator.main`."""

from __future__ import annotations

import logging
import signal
import time

import boto3
import botocore
from botocore.config import Config
from prometheus_client import start_http_server

from gm_validator.bittensor_adapter import MockSubmitter, Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator


def _real_submission_configured(config: ValidatorConfig) -> bool:
    """True iff a real wallet is configured for on-chain submission."""
    return (
        not config.bittensor_mock
        and bool(config.bittensor_wallet_name)
        and bool(config.bittensor_wallet_hotkey)
    )


def _build_submitter(config: ValidatorConfig) -> Submitter:
    if not _real_submission_configured(config):
        # No wallet configured (or mock forced): record submissions in
        # memory. Useful for build-phase smoke tests.
        return MockSubmitter()
    # Wallet/hotkey are non-None here (guarded by _real_submission_configured),
    # but the config types are Optional; assert to satisfy the type checker.
    assert config.bittensor_wallet_name is not None
    assert config.bittensor_wallet_hotkey is not None
    # Lazy import so the test path does not require bittensor-py.
    from gm_validator.bittensor_real import RealSubmitter

    return RealSubmitter(
        netuid=config.bittensor_netuid,
        endpoint=config.bittensor_endpoint,
        wallet_name=config.bittensor_wallet_name,
        wallet_hotkey=config.bittensor_wallet_hotkey,
    )


def _build_miner_uid_lookup(config: ValidatorConfig) -> dict[str, int]:
    """Build the hotkey -> uid lookup from the subnet metagraph.

    Returns an empty mapping when no real wallet is configured — the
    mock-submitter build path has no chain to query.
    """
    if not _real_submission_configured(config):
        return {}
    # Lazy import so the test path does not require bittensor-py.
    from gm_validator.metagraph import load_miner_uid_lookup

    return load_miner_uid_lookup(config.bittensor_netuid, config.bittensor_endpoint)


def _run(config: ValidatorConfig) -> None:
    # boto3-stubs types boto3.client() as an overload set keyed on the
    # Literal service_name. Passing the remaining args via **kwargs
    # erases their types and falls outside every overload, so build the
    # client with explicit named args instead. We enumerate each combination
    # of (endpoint_url, anonymous) to keep the explicit-arg pattern intact.
    anon_config = Config(signature_version=botocore.UNSIGNED) if config.s3_anonymous else None
    if config.s3_endpoint_url and anon_config:
        s3 = boto3.client(
            "s3",
            region_name=config.aws_region,
            endpoint_url=config.s3_endpoint_url,
            config=anon_config,
        )
    elif config.s3_endpoint_url:
        s3 = boto3.client(
            "s3",
            region_name=config.aws_region,
            endpoint_url=config.s3_endpoint_url,
        )
    elif anon_config:
        s3 = boto3.client("s3", region_name=config.aws_region, config=anon_config)
    else:
        s3 = boto3.client("s3", region_name=config.aws_region)
    mirror = S3Mirror(s3, config.s3_bucket, config.s3_prefix, config.local_mirror_dir)
    submitter = _build_submitter(config)
    miner_uid_lookup = _build_miner_uid_lookup(config)

    validator = Validator(config, mirror, submitter, miner_uid_lookup=miner_uid_lookup)

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
            logging.getLogger(__name__).exception("validator loop tick failed")
        time.sleep(config.poll_interval_secs)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    config = ValidatorConfig.from_env()
    start_http_server(config.metrics_port)
    _run(config)


if __name__ == "__main__":
    main()
