"""Tests for the prune retention window and durable processed-epoch state."""

from __future__ import annotations

import pathlib

from gm_validator.processed_state import ProcessedState
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


# ---------------------------------------------------------------------------
# ProcessedState persistence
# ---------------------------------------------------------------------------


def test_processed_state_empty_when_no_file(tmp_path: pathlib.Path) -> None:
    state = ProcessedState(str(tmp_path / "processed.json"))
    assert state.epochs == set()
    assert 5 not in state


def test_processed_state_persists_across_instances(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "processed.json")
    state = ProcessedState(path)
    state.mark(7)
    state.mark(8)

    # Simulate a restart: a fresh instance reads the same file.
    reloaded = ProcessedState(path)
    assert reloaded.epochs == {7, 8}
    assert 7 in reloaded
    assert 8 in reloaded
    assert 9 not in reloaded


def test_processed_state_mark_is_idempotent(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "processed.json")
    state = ProcessedState(path)
    state.mark(3)
    state.mark(3)
    assert state.epochs == {3}


def test_processed_state_creates_parent_dir(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "nested" / "dir" / "processed.json")
    state = ProcessedState(path)
    state.mark(1)
    assert pathlib.Path(path).exists()


def test_processed_state_corrupt_file_starts_empty(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "processed.json"
    path.write_text("{not valid json")
    state = ProcessedState(str(path))
    assert state.epochs == set()
    # A corrupt file must not block recording new progress.
    state.mark(4)
    assert ProcessedState(str(path)).epochs == {4}
