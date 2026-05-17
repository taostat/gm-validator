"""Entry point: `python -m gm_validator.main`."""

from __future__ import annotations

import logging
import signal
import time

import boto3
from prometheus_client import start_http_server

from gm_validator.bittensor_adapter import MockSubmitter, Submitter
from gm_validator.config import ValidatorConfig
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator


def _build_submitter(config: ValidatorConfig) -> Submitter:
    if config.bittensor_mock:
        return MockSubmitter()
    # Real bittensor adapter lands in Phase 2 — for the build phase we
    # default to the mock if no real wallet is configured.
    if not (config.bittensor_wallet_name and config.bittensor_wallet_hotkey):
        return MockSubmitter()
    # Lazy import so the test path does not require bittensor-py.
    from gm_validator.bittensor_real import RealSubmitter

    return RealSubmitter(
        netuid=config.bittensor_netuid,
        endpoint=config.bittensor_endpoint,
        wallet_name=config.bittensor_wallet_name,
        wallet_hotkey=config.bittensor_wallet_hotkey,
    )


def _run(config: ValidatorConfig) -> None:
    boto_kwargs = {"region_name": config.aws_region}
    if config.s3_endpoint_url:
        boto_kwargs["endpoint_url"] = config.s3_endpoint_url
    s3 = boto3.client("s3", **boto_kwargs)
    mirror = S3Mirror(s3, config.s3_bucket, config.s3_prefix, config.local_mirror_dir)
    submitter = _build_submitter(config)

    validator = Validator(config, mirror, submitter)

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
