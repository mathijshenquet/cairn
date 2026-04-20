"""Run management: directory layout, symlinks, entry point."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from cairn.core import Event, FileStore, Handle, JSONLSink, set_sink, set_store

R = TypeVar("R")


class RunManager:
    """Manages the on-disk layout for a single run.

    Layout:
        .cairn/
            outputs/                        # content-addressed store
                {cache_key}.json
            runs/
                {entry_point}-{datetime}/
                    trace.jsonl
                    {seqid:03d}-{name} → ../../outputs/{key}.json
                {entry_point}/
                    latest → ../{entry_point}-{datetime}
    """

    def __init__(self, base_path: str, entry_name: str) -> None:
        self._base = os.path.abspath(base_path)
        self._outputs_dir = os.path.join(self._base, "outputs")
        self._runs_dir = os.path.join(self._base, "runs")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        self._run_id = f"{entry_name}-{ts}"
        self._run_dir = os.path.join(self._runs_dir, self._run_id)
        self._entry_name = entry_name

        os.makedirs(self._outputs_dir, exist_ok=True)
        os.makedirs(self._run_dir, exist_ok=True)

        self._store = FileStore(self._outputs_dir)
        self._sink = JSONLSink(os.path.join(self._run_dir, "trace.jsonl"))
        self._seq = 0

    @property
    def store(self) -> FileStore:
        return self._store

    @property
    def sink(self) -> JSONLSink:
        return self._sink

    @property
    def run_dir(self) -> str:
        return self._run_dir

    def create_symlink(self, name: str, cache_key: str) -> None:
        """Create a symlink from the run directory to the output store."""
        self._seq += 1
        # Sanitize name for filesystem
        safe_name = name.replace("/", ".").replace("\\", ".").replace(" ", "_")
        link_name = f"{self._seq:03d}-{safe_name}"
        link_path = os.path.join(self._run_dir, link_name)
        target = os.path.relpath(
            os.path.join(self._outputs_dir, f"{cache_key}.json"),
            self._run_dir,
        )
        try:
            os.symlink(target, link_path)
        except OSError:
            pass  # symlink creation can fail on some systems

    def update_latest(self) -> None:
        """Update the GC root symlink for this entry point.

        Creates {entry_name} → {entry_name}-{datetime} at the runs/ level.
        This symlink IS the GC root — gc skips whatever it points to.
        """
        root_link = os.path.join(self._runs_dir, self._entry_name)
        target = self._run_id  # relative: just the dir name in the same directory
        try:
            if os.path.islink(root_link):
                os.unlink(root_link)
            elif os.path.isdir(root_link):
                # Clean up old-style directory if it exists
                import shutil
                shutil.rmtree(root_link)
            os.symlink(target, root_link)
        except OSError:
            pass

    def close(self) -> None:
        """Finalize the run."""
        self._sink.close()
        self.update_latest()


class SymlinkTracker:
    """Wraps a sink, creating symlinks when tasks complete."""

    def __init__(self, run_manager: RunManager, inner_sink: JSONLSink) -> None:
        self._rm = run_manager
        self._inner = inner_sink
        self._task_names: dict[int, str] = {}

    def emit(self, event: Event) -> None:
        self._inner.emit(event)

        if event.kind == "spawn" and event.id is not None and event.name is not None:
            self._task_names[event.id] = event.name

        if event.kind == "end" and event.id is not None:
            name = self._task_names.get(event.id, f"task-{event.id}")
            cache_key: str | None = event.kwargs.get("cache_key")
            if cache_key is not None:
                self._rm.create_symlink(name, cache_key)


class CompositeSink:
    """Fans out events to multiple sinks."""

    def __init__(self, *sinks: Any) -> None:
        self._sinks = list(sinks)

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            sink.emit(event)


def run(
    entry: Callable[..., Handle[R]],
    *,
    store_path: str = ".cairn",
    label: str | None = None,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> R:
    """Run a step function as the entry point.

    Sets up file-based output store, JSONL trace sink, and run directory.

    Args:
        entry: The @step-decorated entry point function.
        store_path: Path to the .cairn store directory.
        label: Human-readable label for the run (e.g., 'research_pipeline:main_slow').
               Defaults to the function's __name__.
        args: Positional arguments for the entry function.
        kwargs: Keyword arguments for the entry function.
    """
    entry_label = label or getattr(entry, "__name__", "main")
    rm = RunManager(store_path, entry_label)
    tracker = SymlinkTracker(rm, rm.sink)

    async def _run() -> R:
        store_token = set_store(rm.store)
        sink_token = set_sink(tracker)
        try:
            handle = entry(*args, **(kwargs or {}))
            result: R = await handle
            return result
        finally:
            from cairn.core import reset_sink, reset_store  # noqa: PLC0415

            reset_store(store_token)
            reset_sink(sink_token)
            rm.close()

    return asyncio.run(_run())


# Re-export gc + show for the public `cairn.run` surface.
from .gc import (  # noqa: E402
    RunInfo,
    gc,
    gc_outputs,
    list_runs,
    remove_run,
    remove_runs_before,
)
from .show import show_output, show_runs, show_trace  # noqa: E402
from .spans import SpanGraph  # noqa: E402

__all__ = [
    "RunManager",
    "SymlinkTracker",
    "CompositeSink",
    "run",
    "RunInfo",
    "list_runs",
    "remove_run",
    "remove_runs_before",
    "gc",
    "gc_outputs",
    "show_trace",
    "show_runs",
    "show_output",
    "SpanGraph",
]
