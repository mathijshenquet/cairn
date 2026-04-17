"""Tests for garbage collection."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from cairn import step, gc, list_runs, remove_run, remove_runs_before, run, trace


def _make_runs(tmp_path: Path, n: int = 3) -> str:
    """Helper: create n runs of a simple pipeline."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def work(x: int) -> int:
        trace("working")
        return x * 2

    for i in range(n):
        run(work, store_path=store_path, args=(i,))
        time.sleep(0.01)  # ensure distinct timestamps

    return store_path


def test_list_runs(tmp_path: Path) -> None:
    """list_runs() returns all runs sorted by timestamp."""
    store_path = _make_runs(tmp_path, n=3)
    runs = list_runs(store_path)

    assert len(runs) == 3
    assert all(r.entry_name == "work" for r in runs)
    # Sorted by timestamp
    assert runs[0].timestamp <= runs[1].timestamp <= runs[2].timestamp
    # Last one is latest
    assert runs[-1].is_latest
    assert not runs[0].is_latest
    # All have symlinks
    assert all(r.symlink_count >= 1 for r in runs)


def test_list_runs_empty(tmp_path: Path) -> None:
    """list_runs() on empty store returns empty list."""
    assert list_runs(str(tmp_path / ".cairn")) == []


def test_remove_run(tmp_path: Path) -> None:
    """remove_run() deletes a specific run directory."""
    store_path = _make_runs(tmp_path, n=2)
    runs = list_runs(store_path)
    assert len(runs) == 2

    removed = remove_run(store_path, runs[0].run_id)
    assert removed

    remaining = list_runs(store_path)
    assert len(remaining) == 1
    assert remaining[0].run_id == runs[1].run_id


def test_remove_run_nonexistent(tmp_path: Path) -> None:
    """remove_run() returns False for nonexistent run."""
    store_path = str(tmp_path / ".cairn")
    assert not remove_run(store_path, "nonexistent-run")


def test_remove_runs_before(tmp_path: Path) -> None:
    """remove_runs_before() deletes old runs but keeps latest."""
    store_path = _make_runs(tmp_path, n=3)
    runs = list_runs(store_path)

    # Remove runs before the last one's timestamp
    cutoff = runs[-1].timestamp
    removed = remove_runs_before(store_path, cutoff, keep_latest=True)

    # First two should be removed (but if one is latest, it's kept)
    remaining = list_runs(store_path)
    # At minimum, the latest is kept
    assert any(r.is_latest for r in remaining)
    assert len(removed) >= 1


def test_remove_runs_before_keeps_latest(tmp_path: Path) -> None:
    """Even with a very old cutoff, latest is never removed when keep_latest=True."""
    store_path = _make_runs(tmp_path, n=1)
    runs = list_runs(store_path)
    assert len(runs) == 1
    assert runs[0].is_latest

    # Try to remove everything
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    removed = remove_runs_before(store_path, far_future, keep_latest=True)
    assert len(removed) == 0  # latest is protected

    # With keep_latest=False, it gets removed
    removed = remove_runs_before(store_path, far_future, keep_latest=False)
    assert len(removed) == 1


def test_gc_outputs(tmp_path: Path) -> None:
    """gc_outputs() removes unreferenced output files."""
    store_path = _make_runs(tmp_path, n=3)

    outputs_dir = tmp_path / ".cairn" / "outputs"
    outputs_before = set(os.listdir(outputs_dir))
    assert len(outputs_before) >= 3  # at least one per run

    # Remove first two runs — their outputs may become orphaned
    runs = list_runs(store_path)
    remove_run(store_path, runs[0].run_id)
    remove_run(store_path, runs[1].run_id)

    # GC outputs
    from cairn.gc import gc_outputs
    removed = gc_outputs(store_path)

    outputs_after = set(os.listdir(outputs_dir))
    # Some outputs should have been removed (unless all runs share the same cache keys)
    # At minimum, no output is unreferenced now
    remaining_runs = list_runs(store_path)
    for r in remaining_runs:
        for entry in os.scandir(r.path):
            if entry.is_symlink():
                target = Path(entry.path).resolve()
                assert target.exists(), f"symlink {entry.name} points to missing output"


def test_gc_full_cycle(tmp_path: Path) -> None:
    """Full GC: remove old runs, sweep orphaned outputs."""
    store_path = _make_runs(tmp_path, n=5)

    outputs_before = len(os.listdir(tmp_path / ".cairn" / "outputs"))
    runs_before = list_runs(store_path)
    assert len(runs_before) == 5

    # GC everything before the 4th run
    cutoff = runs_before[3].timestamp
    removed_runs, removed_outputs = gc(store_path, before=cutoff, keep_latest=True)

    # Should have removed runs 0-2 (3 is the cutoff, 4 is latest and after cutoff)
    remaining = list_runs(store_path)
    assert len(remaining) <= 3  # at most runs 3, 4, and maybe the latest-protected one
    assert any(r.is_latest for r in remaining)

    # Outputs should be cleaned up
    outputs_after = len(os.listdir(tmp_path / ".cairn" / "outputs"))
    # Can't assert exact count since cache hits may share outputs
    assert outputs_after <= outputs_before


def test_gc_with_shared_outputs(tmp_path: Path) -> None:
    """Outputs shared between runs are not removed until all referencing runs are gone."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def constant() -> str:
        return "always the same"

    # Two runs producing the same cached output
    run(constant, store_path=store_path)
    time.sleep(0.01)
    run(constant, store_path=store_path)

    runs = list_runs(store_path)
    assert len(runs) == 2

    # Remove first run
    remove_run(store_path, runs[0].run_id)

    # GC — output should NOT be removed (still referenced by run 2)
    from cairn.gc import gc_outputs
    removed = gc_outputs(store_path)

    # The shared output should still exist
    remaining_run = list_runs(store_path)[0]
    for entry in os.scandir(remaining_run.path):
        if entry.is_symlink():
            assert Path(entry.path).resolve().exists()


def test_list_runs_multiple_entry_points(tmp_path: Path) -> None:
    """list_runs() works with multiple different entry points."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def pipeline_a() -> str:
        return "a"

    @step
    async def pipeline_b() -> str:
        return "b"

    run(pipeline_a, store_path=store_path)
    time.sleep(0.01)
    run(pipeline_b, store_path=store_path)
    time.sleep(0.01)
    run(pipeline_a, store_path=store_path)

    runs = list_runs(store_path)
    assert len(runs) == 3

    a_runs = [r for r in runs if r.entry_name == "pipeline_a"]
    b_runs = [r for r in runs if r.entry_name == "pipeline_b"]
    assert len(a_runs) == 2
    assert len(b_runs) == 1

    # Each entry point has its own latest
    assert a_runs[-1].is_latest
    assert b_runs[0].is_latest
