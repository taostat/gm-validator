"""Tests for the S3 mirror prune retention window."""

from __future__ import annotations

import pathlib

from gm_validator.s3_mirror import S3Mirror


def _make_epoch_dir(root: pathlib.Path, epoch_id: int) -> pathlib.Path:
    path = root / f"epoch={epoch_id}"
    path.mkdir()
    (path / "_FINALIZED").write_bytes(b"")
    (path / "aggregated.jsonl").write_text("{}\n")
    return path


# ---------------------------------------------------------------------------
# S3Mirror.prune retention window
# ---------------------------------------------------------------------------


def test_prune_keeps_most_recent_n(tmp_path: pathlib.Path) -> None:
    for epoch_id in (1, 2, 3, 4, 5):
        _make_epoch_dir(tmp_path, epoch_id)
    mirror = S3Mirror(s3_client=None, bucket="b", prefix="v1", local_root=str(tmp_path))

    mirror.prune(retention_epochs=3)

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["epoch=3", "epoch=4", "epoch=5"]


def test_prune_noop_when_under_retention(tmp_path: pathlib.Path) -> None:
    for epoch_id in (10, 11):
        _make_epoch_dir(tmp_path, epoch_id)
    mirror = S3Mirror(s3_client=None, bucket="b", prefix="v1", local_root=str(tmp_path))

    mirror.prune(retention_epochs=5)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["epoch=10", "epoch=11"]


def test_prune_disabled_with_nonpositive_retention(tmp_path: pathlib.Path) -> None:
    for epoch_id in (1, 2, 3):
        _make_epoch_dir(tmp_path, epoch_id)
    mirror = S3Mirror(s3_client=None, bucket="b", prefix="v1", local_root=str(tmp_path))

    mirror.prune(retention_epochs=0)

    assert len(list(tmp_path.iterdir())) == 3


def test_prune_ignores_non_epoch_entries(tmp_path: pathlib.Path) -> None:
    _make_epoch_dir(tmp_path, 1)
    _make_epoch_dir(tmp_path, 2)
    (tmp_path / "not-an-epoch").mkdir()
    mirror = S3Mirror(s3_client=None, bucket="b", prefix="v1", local_root=str(tmp_path))

    mirror.prune(retention_epochs=1)

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["epoch=2", "not-an-epoch"]
