"""S3 watcher + local mirror.

The validator does not stream artifacts on every request; instead it
materialises a local mirror of
`s3://{bucket}/{prefix}/finalized/epoch={N}/` for each new epoch and
reads the cost-derived rows out of it. The mirror doubles as a cheap
on-disk audit log: operators can inspect any epoch the validator has
processed by browsing `${LOCAL_MIRROR_DIR}/epoch=N/`.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)

_ARTIFACTS = (
    "aggregated.jsonl",
    "epoch_summary.json",
    "_FINALIZED",
)


class S3Mirror:
    """Wraps a boto3 S3 client + a local cache directory."""

    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        prefix: str,
        local_root: str,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._local_root = local_root

    # ---- discovery ----

    def discover_finalized_epochs(self) -> list[int]:
        """Return the sorted list of epoch ids that have a `_FINALIZED` marker."""
        prefix = f"{self._prefix}/finalized/"
        paginator = self._s3.get_paginator("list_objects_v2")
        epochs: set[int] = set()
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []) or []:
                # cp = {"Prefix": "v1/finalized/epoch=142/"}
                segment = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
                if not segment.startswith("epoch="):
                    continue
                try:
                    epoch_id = int(segment.removeprefix("epoch="))
                except ValueError:
                    continue
                if self._marker_exists(epoch_id):
                    epochs.add(epoch_id)
        return sorted(epochs)

    # ---- mirror ----

    def mirror_epoch(self, epoch_id: int) -> str:
        """Download every artifact for the epoch to a local directory.

        Returns the local directory path; the validator reads
        ``aggregated.jsonl`` and ``epoch_summary.json`` from it.
        """
        local_dir = self._epoch_dir(epoch_id)
        os.makedirs(local_dir, exist_ok=True)
        for name in _ARTIFACTS:
            self._download(epoch_id, name, os.path.join(local_dir, name))
        return local_dir

    def epoch_already_mirrored(self, epoch_id: int) -> bool:
        """True iff every artifact is already on local disk."""
        local_dir = self._epoch_dir(epoch_id)
        return all(os.path.exists(os.path.join(local_dir, name)) for name in _ARTIFACTS)

    def invalidate_artifact(self, epoch_id: int, name: str) -> None:
        """Drop the cached copy of *name* so the next ``mirror_epoch`` refetches it.

        Used when the validator detects that the cached artifact is
        stale (e.g. an ``epoch_summary.json`` written by a pre-PR#176
        finalizer): the operator republishes the corrected artifact in
        S3 and the next tick must re-download it. ``_download`` is a
        no-op when the local file exists, so without this invalidation
        the validator would keep reading the stale cached copy forever.
        """
        path = os.path.join(self._epoch_dir(epoch_id), name)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    def _epoch_dir(self, epoch_id: int) -> str:
        return os.path.join(self._local_root, f"epoch={epoch_id}")

    def _download(self, epoch_id: int, name: str, local_path: str) -> None:
        if os.path.exists(local_path):
            return
        key = f"{self._prefix}/finalized/epoch={epoch_id}/{name}"
        LOGGER.info("downloading s3://%s/%s -> %s", self._bucket, key, local_path)
        tmp = local_path + ".part"
        self._s3.download_file(self._bucket, key, tmp)
        os.replace(tmp, local_path)

    def _marker_exists(self, epoch_id: int) -> bool:
        key = f"{self._prefix}/finalized/epoch={epoch_id}/_FINALIZED"
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except self._s3.exceptions.ClientError as e:
            err = e.response.get("Error", {}).get("Code", "")
            if err in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        else:
            return True

    # ---- cleanup ----

    def prune(self, retention_epochs: int) -> None:
        """Keep only the *retention_epochs* highest epoch mirrors on disk.

        Older mirrors are deleted. The local mirror is a convenience
        audit cache; keeping every epoch ever processed would grow it
        without bound, so we retain a fixed recent window. A
        non-positive *retention_epochs* disables pruning.
        """
        if retention_epochs <= 0 or not os.path.isdir(self._local_root):
            return
        epoch_dirs: list[tuple[int, str]] = []
        for entry in os.listdir(self._local_root):
            if not entry.startswith("epoch="):
                continue
            try:
                epoch_id = int(entry.removeprefix("epoch="))
            except ValueError:
                continue
            epoch_dirs.append((epoch_id, entry))
        epoch_dirs.sort(reverse=True)
        for _, entry in epoch_dirs[retention_epochs:]:
            path = os.path.join(self._local_root, entry)
            LOGGER.info("pruning stale local mirror: %s", path)
            for f in os.listdir(path):
                os.unlink(os.path.join(path, f))
            os.rmdir(path)
