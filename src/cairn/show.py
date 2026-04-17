"""Terminal viewer for Cairn traces and store contents.

Usage:
    from cairn.show import show_trace, show_runs, show_output, LiveRenderer
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any

from cairn.context import Event
from cairn.gc import list_runs


# ── ANSI colors ──

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


# ── Live renderer — formats events as they arrive ──


class LiveRenderer:
    """Renders trace events to the terminal as they arrive.

    Can be used as a Sink (has an emit() method) to show live progress
    during execution.
    """

    def __init__(self, file: Any = None) -> None:
        self._out = file or sys.stderr
        self._names: dict[int, str] = {}
        self._parents: dict[int, int | None] = {}
        self._start_times: dict[int, float] = {}
        self._first_ts: float | None = None

    def _depth(self, span_id: int) -> int:
        d = 0
        current = span_id
        while current in self._parents and self._parents[current] is not None:
            d += 1
            parent = self._parents[current]
            if parent is None:
                break
            current = parent
        return d

    def _print(self, msg: str) -> None:
        self._out.write(msg + "\n")
        self._out.flush()

    def render_event(self, e: dict[str, Any]) -> None:
        """Render a single event dict (from JSONL or converted from Event)."""
        kind: str = e["e"]
        span_id: int = e.get("id", 0)

        if self._first_ts is None:
            self._first_ts = e.get("ts", 0.0)
        relative_ts: float = e.get("ts", 0.0) - (self._first_ts or 0.0)

        if kind == "spawn":
            name: str = e.get("name", "?")
            args_str: str = e.get("args", "")
            self._names[span_id] = name
            self._parents[span_id] = e.get("parent")
            d = self._depth(span_id)
            indent = "  " * d
            icon = _color("○", _DIM)
            args_display = f"({args_str})" if args_str else ""
            self._print(f"  {relative_ts:8.3f}s {indent}{icon} {_BOLD}{name}{_RESET}{_DIM}{args_display}{_RESET}")

        elif kind == "start":
            self._start_times[span_id] = e.get("ts", 0.0)
            name = self._names.get(span_id, f"task-{span_id}")
            d = self._depth(span_id)
            indent = "  " * d
            icon = _color("◉", _YELLOW)
            self._print(f"  {relative_ts:8.3f}s {indent}{icon} {name}")

        elif kind == "end":
            name = self._names.get(span_id, f"task-{span_id}")
            d = self._depth(span_id)
            indent = "  " * d
            cached = e.get("cached", False)
            duration = ""
            if span_id in self._start_times:
                dur = e.get("ts", 0.0) - self._start_times[span_id]
                duration = f" ({dur:.3f}s)"
            if cached:
                icon = _color("⚡", _GREEN)
                self._print(f"  {relative_ts:8.3f}s {indent}{icon} {name} {_DIM}cached{_RESET}{duration}")
            else:
                icon = _color("✓", _GREEN)
                self._print(f"  {relative_ts:8.3f}s {indent}{icon} {name} done{duration}")

        elif kind == "error":
            name = self._names.get(span_id, f"task-{span_id}")
            d = self._depth(span_id)
            indent = "  " * d
            err = e.get("err", "unknown error")
            icon = _color("✗", _RED)
            self._print(f"  {relative_ts:8.3f}s {indent}{icon} {name} {_color(str(err), _RED)}")

        elif kind == "cancel":
            name = self._names.get(span_id, f"task-{span_id}")
            d = self._depth(span_id)
            indent = "  " * d
            icon = _color("⊘", _DIM)
            self._print(f"  {relative_ts:8.3f}s {indent}{icon} {name} {_color('cancelled', _DIM)}")

        elif kind == "trace":
            parent_id: int = e.get("parent", 0)
            d = self._depth(parent_id) + 1 if parent_id in self._parents else 1
            indent = "  " * d
            msg: str = e.get("msg", "")
            progress: list[int] | None = e.get("progress")
            edge: bool = e.get("edge", False)

            if edge:
                icon = _color("→", _MAGENTA)
                self._print(f"  {relative_ts:8.3f}s {indent}{icon} {_color(msg, _MAGENTA)}")
            elif progress:
                cur, total = progress[0], progress[1]
                bar_width = 10
                filled = int(bar_width * cur / total)
                bar = "█" * filled + "░" * (bar_width - filled)
                self._print(f"  {relative_ts:8.3f}s {indent}{_DIM}[{bar}] {msg} ({cur}/{total}){_RESET}")
            else:
                self._print(f"  {relative_ts:8.3f}s {indent}{_DIM}{msg}{_RESET}")

    def emit(self, event: Event) -> None:
        """Sink-compatible emit: convert Event to dict and render."""
        from cairn.sink import event_to_dict
        event.ts = time.monotonic()
        self.render_event(event_to_dict(event))


# ── Show trace (batch, from file) ──


def show_trace(store_path: str, run_id: str | None = None) -> None:
    """Print a formatted trace from a run's trace.jsonl."""
    runs_dir = os.path.join(store_path, "runs")

    if run_id is None:
        for entry in os.scandir(runs_dir):
            if entry.is_symlink():
                run_id = os.readlink(entry.path)
                break
        if run_id is None:
            print("No runs found.")
            return

    trace_path = os.path.join(runs_dir, run_id, "trace.jsonl")
    if not os.path.exists(trace_path):
        print(f"Trace not found: {trace_path}")
        return

    print(f"\n{_BOLD}Trace: {run_id}{_RESET}\n")

    renderer = LiveRenderer(file=sys.stdout)
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                renderer.render_event(json.loads(line))
    print()


# ── Show runs ──


def show_runs(store_path: str) -> None:
    """Print a summary of all runs."""
    runs = list_runs(store_path)
    if not runs:
        print("No runs found.")
        return

    print(f"\n{_BOLD}Runs in {store_path}{_RESET}\n")
    for r in runs:
        latest = _color(" [latest]", _GREEN) if r.is_latest else ""
        age = datetime.now(r.timestamp.tzinfo) - r.timestamp
        age_str = f"{age.total_seconds():.0f}s ago"
        if age.total_seconds() > 3600:
            age_str = f"{age.total_seconds() / 3600:.1f}h ago"
        elif age.total_seconds() > 60:
            age_str = f"{age.total_seconds() / 60:.0f}m ago"

        print(f"  {r.entry_name:20s} {r.run_id:50s} {r.symlink_count:3d} outputs  {age_str}{latest}")
    print()


# ── Show output ──


def show_output(path: str) -> None:
    """Pretty-print a cached output file."""
    with open(path, "r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)

    result = data.get("result")
    traces = data.get("traces", [])
    duration = data.get("duration", 0)
    error = data.get("error")

    if error:
        print(f"\n{_color('ERROR', _RED)}: {error}")
    else:
        print(f"\n{_BOLD}Result:{_RESET}")
        if isinstance(result, str):
            print(f"  {result}")
        else:
            print(f"  {json.dumps(result, indent=2)}")

    if traces:
        print(f"\n{_BOLD}Traces:{_RESET}")
        first_ts = traces[0].get("timestamp", 0) if traces else 0
        for t in traces:
            msg = t.get("message", "")
            elapsed = t.get("timestamp", 0) - first_ts
            kwargs_str = ""
            kw: dict[str, Any] = t.get("kwargs", {})
            if kw:
                kwargs_str = " " + " ".join(f"{k}={v}" for k, v in kw.items())
                kwargs_str = _DIM + kwargs_str + _RESET
            print(f"  {elapsed:7.3f}s {msg}{kwargs_str}")

    print(f"\n{_DIM}Duration: {duration:.3f}s{_RESET}\n")
