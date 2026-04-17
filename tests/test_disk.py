"""Tests for on-disk store, JSONL trace, and run()."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cairn import step, run, trace


def test_run_creates_disk_layout(tmp_path: Path) -> None:
    """run() creates outputs/ and runs/ with proper structure."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def greet(name: str) -> str:
        trace("building greeting")
        return f"hello {name}"

    result = run(greet, store_path=store_path, args=("world",))
    assert result == "hello world"

    # Output store exists with at least one entry
    outputs = tmp_path / ".cairn" / "outputs"
    assert outputs.is_dir()
    output_files = list(outputs.glob("*.json"))
    assert len(output_files) >= 1

    # Output is valid JSON with expected structure
    with open(output_files[0], "r") as f:
        entry = json.load(f)
    assert "result" in entry
    assert "traces" in entry
    assert "duration" in entry

    # Runs directory exists
    runs = tmp_path / ".cairn" / "runs"
    assert runs.is_dir()

    # A run directory was created with trace.jsonl
    run_dirs = [d for d in runs.iterdir() if d.is_dir() and d.name.startswith("greet-")]
    assert len(run_dirs) == 1

    trace_file = run_dirs[0] / "trace.jsonl"
    assert trace_file.exists()

    # Trace is valid JSONL
    with open(trace_file, "r") as f:
        events = [json.loads(line) for line in f if line.strip()]
    assert len(events) > 0
    event_types = [e["e"] for e in events]
    assert "spawn" in event_types
    assert "start" in event_types
    assert "end" in event_types
    assert "trace" in event_types


def test_run_creates_symlinks(tmp_path: Path) -> None:
    """run() creates symlinks from run dir to outputs."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def add(a: int, b: int) -> int:
        return a + b

    result = run(add, store_path=store_path, args=(1, 2))
    assert result == 3

    # Find the run directory
    runs = tmp_path / ".cairn" / "runs"
    run_dirs = [d for d in runs.iterdir() if d.is_dir() and d.name.startswith("add-")]
    assert len(run_dirs) == 1

    # Should have symlinks (001-add or similar)
    symlinks = [f for f in run_dirs[0].iterdir() if f.is_symlink()]
    assert len(symlinks) >= 1

    # Symlink points to a valid output file
    for link in symlinks:
        target = link.resolve()
        assert target.exists()
        assert target.suffix == ".json"


def test_run_creates_gc_root_symlink(tmp_path: Path) -> None:
    """run() maintains a GC root symlink for the entry point."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def compute() -> int:
        return 42

    run(compute, store_path=store_path)

    gc_root = tmp_path / ".cairn" / "runs" / "compute"
    assert gc_root.is_symlink()
    assert (gc_root / "trace.jsonl").exists()


def test_run_caches_across_runs(tmp_path: Path) -> None:
    """Second run() reuses cached outputs from first run."""
    store_path = str(tmp_path / ".cairn")
    call_count = 0

    @step(memo=True)
    async def expensive() -> str:
        nonlocal call_count
        call_count += 1
        return "result"

    # First run
    result1 = run(expensive, store_path=store_path)
    assert result1 == "result"
    assert call_count == 1

    # Second run — should hit cache
    result2 = run(expensive, store_path=store_path)
    assert result2 == "result"
    assert call_count == 1  # not called again


def test_run_with_fanout(tmp_path: Path) -> None:
    """run() handles fan-out correctly on disk."""
    store_path = str(tmp_path / ".cairn")

    @step
    async def double(x: int) -> int:
        return x * 2

    @step
    async def pipeline() -> list[int]:
        handles = [double(i) for i in range(3)]
        return [await h for h in handles]

    result = run(pipeline, store_path=store_path)
    assert result == [0, 2, 4]

    # Should have output files for pipeline + 3 doubles
    outputs = list((tmp_path / ".cairn" / "outputs").glob("*.json"))
    assert len(outputs) >= 4

    # Run directory should have symlinks for each task
    runs = tmp_path / ".cairn" / "runs"
    run_dirs = [d for d in runs.iterdir() if d.is_dir() and d.name.startswith("pipeline-")]
    symlinks = [f for f in run_dirs[0].iterdir() if f.is_symlink()]
    assert len(symlinks) >= 4  # pipeline + 3 doubles
