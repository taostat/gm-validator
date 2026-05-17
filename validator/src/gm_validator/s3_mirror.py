"""S3 watcher + local mirror.

The validator does not stream artifacts on every request; instead it
materialises a local mirror of
`s3://{bucket}/{prefix}/finalized/epoch={N}/` for each new epoch, then
invokes the `gm-verifier` subprocess against that directory. Reasons:

- The Rust verifier needs file-path inputs (zstd decompression is
  streaming over a file handle).
- The mirror is a cheap audit log; operators can `gm-verifier verify
  --epoch N --dir /var/cache/gm-validator/epoch=N` on demand.
- A second run of `verify` against the same directory is a no-op,
  giving us a free idempotency check.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

LOGGER = logging.getLogger(__name__)


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

        Returns the local directory path, suitable as the `--dir` argument
        to `gm-verifier verify`.
        """
        local_dir = self._epoch_dir(epoch_id)
        os.makedirs(local_dir, exist_ok=True)
        names = ("raw.jsonl.zst", "aggregated.jsonl", "gateway_keys.json", "_FINALIZED")
        for name in names:
            self._download(epoch_id, name, os.path.join(local_dir, name))
        return local_dir

    def epoch_already_mirrored(self, epoch_id: int) -> bool:
        """True iff all four artifacts are already on local disk."""
        local_dir = self._epoch_dir(epoch_id)
        return all(
            os.path.exists(os.path.join(local_dir, name))
            for name in ("raw.jsonl.zst", "aggregated.jsonl", "gateway_keys.json", "_FINALIZED")
        )

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

    def prune(self, keep_epoch_ids: Iterable[int]) -> None:
        """Drop local mirrors for epochs not in *keep_epoch_ids*."""
        keep = set(keep_epoch_ids)
        if not os.path.isdir(self._local_root):
            return
        for entry in os.listdir(self._local_root):
            if not entry.startswith("epoch="):
                continue
            try:
                epoch_id = int(entry.removeprefix("epoch="))
            except ValueError:
                continue
            if epoch_id in keep:
                continue
            path = os.path.join(self._local_root, entry)
            LOGGER.info("pruning stale local mirror: %s", path)
            for f in os.listdir(path):
                os.unlink(os.path.join(path, f))
            os.rmdir(path)
