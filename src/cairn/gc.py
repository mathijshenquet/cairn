"""Garbage collection for the Cairn store.

Nix-style: remove runs, then sweep orphaned outputs.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class RunInfo:
    """Information about a single run."""

    run_id: str
    entry_name: str
    timestamp: datetime
    path: str
    is_latest: bool
    symlink_count: int


def _parse_run_id(run_id: str) -> tuple[str, datetime] | None:
    """Parse a run directory name into (entry_name, timestamp).

    Expected format: {entry_name}-{ISO datetime with microseconds}
    e.g. 'pipeline-2026-04-16T13:05:56.123456'
    """
    # Find the datetime part by looking for the T separator
    # The entry name can contain hyphens, so split from the right
    # looking for the ISO date pattern
    for i in range(len(run_id) - 1, 0, -1):
        if run_id[i] == "T" and i >= 11:
            # Try to parse everything from the date start
            # Date starts 10 chars before the T: YYYY-MM-DD
            date_start = i - 10
            if date_start > 0 and run_id[date_start - 1] == "-":
                entry_name = run_id[: date_start - 1]
                ts_str = run_id[date_start:]
                try:
                    ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                    return (entry_name, ts)
                except ValueError:
                    continue
    return None


def _get_gc_roots(runs_dir: str) -> set[str]:
    """Get the set of run directory names that are GC roots.

    GC roots are symlinks at the runs/ level: {entry_name} → {entry_name}-{datetime}.
    """
    roots: set[str] = set()
    if not os.path.isdir(runs_dir):
        return roots

    for entry in os.scandir(runs_dir):
        if entry.is_symlink():
            # This is a GC root symlink (e.g., 'pipeline' → 'pipeline-2026-...')
            target = os.readlink(entry.path)
            roots.add(os.path.basename(target))
    return roots


def _get_referenced_outputs(run_path: str) -> set[str]:
    """Get output filenames referenced by symlinks in a run directory."""
    refs: set[str] = set()
    for entry in os.scandir(run_path):
        if entry.is_symlink():
            target = os.readlink(entry.path)
            # target is like '../../outputs/abc123.json'
            refs.add(os.path.basename(target))
    return refs


def list_runs(store_path: str) -> list[RunInfo]:
    """List all runs in the store, sorted by timestamp (oldest first)."""
    runs_dir = os.path.join(store_path, "runs")
    if not os.path.isdir(runs_dir):
        return []

    gc_roots = _get_gc_roots(runs_dir)
    runs: list[RunInfo] = []

    for entry in os.scandir(runs_dir):
        if not entry.is_dir(follow_symlinks=False):
            continue
        parsed = _parse_run_id(entry.name)
        if parsed is None:
            continue

        entry_name, timestamp = parsed
        symlink_count = sum(1 for e in os.scandir(entry.path) if e.is_symlink())

        runs.append(RunInfo(
            run_id=entry.name,
            entry_name=entry_name,
            timestamp=timestamp,
            path=entry.path,
            is_latest=(entry.name in gc_roots),
            symlink_count=symlink_count,
        ))

    runs.sort(key=lambda r: r.timestamp)
    return runs


def remove_run(store_path: str, run_id: str) -> bool:
    """Remove a specific run directory. Returns True if removed."""
    run_path = os.path.join(store_path, "runs", run_id)
    if not os.path.isdir(run_path):
        return False
    shutil.rmtree(run_path)
    return True


def remove_runs_before(
    store_path: str,
    before: datetime,
    *,
    keep_latest: bool = True,
) -> list[str]:
    """Remove runs older than a given datetime.

    Args:
        store_path: Path to the .cairn directory.
        before: Remove runs with timestamps before this datetime.
        keep_latest: If True, never remove runs that are the 'latest' for their entry point.

    Returns:
        List of removed run IDs.
    """
    runs = list_runs(store_path)
    removed: list[str] = []

    for r in runs:
        if r.timestamp >= before:
            continue
        if keep_latest and r.is_latest:
            continue
        if remove_run(store_path, r.run_id):
            removed.append(r.run_id)

    return removed


def gc_outputs(store_path: str) -> list[str]:
    """Remove output files not referenced by any remaining run.

    Nix-style garbage collection: scan all run directories for symlinks,
    collect the set of referenced output files, delete everything else.

    Returns:
        List of removed output filenames.
    """
    outputs_dir = os.path.join(store_path, "outputs")
    runs_dir = os.path.join(store_path, "runs")

    if not os.path.isdir(outputs_dir):
        return []

    # Collect all referenced outputs across all remaining runs
    referenced: set[str] = set()
    if os.path.isdir(runs_dir):
        for entry in os.scandir(runs_dir):
            if entry.is_dir(follow_symlinks=False) and _parse_run_id(entry.name) is not None:
                referenced.update(_get_referenced_outputs(entry.path))

    # Remove unreferenced outputs
    removed: list[str] = []
    for entry in os.scandir(outputs_dir):
        if entry.is_file() and entry.name not in referenced:
            os.unlink(entry.path)
            removed.append(entry.name)

    return removed


def gc(
    store_path: str,
    *,
    before: datetime | None = None,
    keep_latest: bool = True,
) -> tuple[list[str], list[str]]:
    """Full garbage collection: remove old runs, then sweep orphaned outputs.

    Args:
        store_path: Path to the .cairn directory.
        before: If given, remove runs older than this. If None, don't remove any runs
                (only gc orphaned outputs).
        keep_latest: If True, never remove the latest run for each entry point.

    Returns:
        Tuple of (removed_run_ids, removed_output_files).
    """
    removed_runs: list[str] = []
    if before is not None:
        removed_runs = remove_runs_before(store_path, before, keep_latest=keep_latest)

    removed_outputs = gc_outputs(store_path)
    return removed_runs, removed_outputs
