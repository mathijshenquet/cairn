"""Textual TUI for running and browsing Cairn pipelines."""

from __future__ import annotations

import json
import os
import time
import threading
from typing import Any, Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header, Static
from textual.widgets import Tree as TextualTree

from cairn.context import Event, set_sink
from cairn.core import Handle, set_store
from cairn.gc import RunInfo, list_runs
from cairn.run import CompositeSink, RunManager, SymlinkTracker
from cairn.sink import event_to_dict


# ── Messages for pipeline events ──


class PipelineEvent(Message):
    """An event from the running pipeline, posted from the worker thread."""

    def __init__(self, event_dict: dict[str, Any]) -> None:
        super().__init__()
        self.event_dict = event_dict


class PipelineDone(Message):
    def __init__(self, result: Any = None, error: str | None = None) -> None:
        super().__init__()
        self.result = result
        self.error = error


# ── Thread-safe sink that posts to Textual ──


class TuiSink:
    """Sink that posts events to a Textual app from any thread."""

    def __init__(self, app: CairnApp) -> None:
        self._app = app

    def emit(self, event: Event) -> None:
        event.ts = time.monotonic()
        d = event_to_dict(event)
        self._app.call_from_thread(self._app.post_message, PipelineEvent(d))


# ── Main app ──


class CairnApp(App[None]):
    """Unified TUI: run selector → run view (live or replayed)."""

    TITLE = "Cairn"
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("q", "quit", "Quit"),
        Binding("backspace", "go_back", "Back"),
        Binding("c", "copy_detail", "Copy"),
    ]
    CSS = """
    #main { height: 1fr; }
    #tree {
        width: 1fr;
        max-width: 50%;
        height: 1fr;
    }
    #detail-scroll {
        width: 1fr;
        height: 1fr;
    }
    #detail {
        padding: 0 1;
        width: 1fr;
        height: auto;
    }
    """

    def __init__(
        self,
        store_path: str,
        entry_fn: Callable[..., Handle[Any]] | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__()
        self._store_path = store_path
        self._entry_fn = entry_fn
        self._label = label or "main"
        self._runs_by_id: dict[str, RunInfo] = {}
        self._current_run_id: str | None = None  # None = selector view
        self._live_active: bool = False
        self._detail_plain: str = ""
        self._reset_span_state()

    def _update_detail(self, content: "Text | str") -> None:
        detail = self.query_one("#detail", Static)
        detail.update(content)
        if isinstance(content, Text):
            self._detail_plain = content.plain
        else:
            self._detail_plain = Text.from_markup(content).plain

    # status ∈ {"pending", "running", "cached", "ok", "error", "cancelled"}
    STATUS_ICONS: dict[str, tuple[str, str]] = {
        "pending": ("○", "dim"),
        "running": ("◉", "yellow"),
        "cached": ("⚡", "green"),
        "ok": ("✓", "green"),
        "error": ("✗", "red"),
        "cancelled": ("⊘", "dim"),
    }
    TERMINAL_STATUSES = frozenset({"cached", "ok", "error", "cancelled"})

    def _reset_span_state(self) -> None:
        self.span_names: dict[int, str] = {}
        self.span_parents: dict[int, int | None] = {}
        self.span_status: dict[int, str] = {}
        self.span_start_times: dict[int, float] = {}
        self.span_end_times: dict[int, float] = {}
        self.span_spawn_times: dict[int, float] = {}
        self.span_first_ts: float | None = None
        self.span_tree_nodes: dict[int, Any] = {}
        self.span_cache_keys: dict[int, str] = {}
        self.span_args: dict[int, str] = {}
        self.span_traces: dict[int, list[dict[str, Any]]] = {}
        self.span_errors: dict[int, str] = {}
        self.span_memo: set[int] = set()
        self.highlighted_span: int | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield TextualTree("Cairn", id="tree")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail")
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "go_back":
            return True if self._current_run_id is not None else None
        return True

    def on_mount(self) -> None:
        if self._entry_fn is not None:
            # Live mode: jump straight to a run view and feed events in.
            self._current_run_id = "__live__"
            self._live_active = True
            self._show_run_view(self._label)
            self._start_pipeline()
        else:
            self._show_selector()

    # ── Selector view ──

    def _show_selector(self) -> None:
        self._reset_span_state()
        self._current_run_id = None
        self.sub_title = ""
        self.refresh_bindings()
        runs = list_runs(self._store_path)
        self._runs_by_id = {r.run_id: r for r in runs}

        tree = self.query_one("#tree", TextualTree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()

        self._update_detail("")

        by_entry: dict[str, list[RunInfo]] = {}
        for r in runs:
            by_entry.setdefault(r.entry_name, []).append(r)

        for name, entry_runs in sorted(by_entry.items()):
            entry_runs.sort(key=lambda r: r.timestamp, reverse=True)
            count = len(entry_runs)
            entry_node = tree.root.add(
                f"[bold]{name}[/bold]  [dim]{count} run{'s' if count != 1 else ''}[/dim]",
                data=f"entry:{name}",
            )
            entry_node.expand()
            for i, r in enumerate(entry_runs):
                tag = "[cyan]latest[/cyan]" if i == 0 else "      "
                ts_short = r.timestamp.strftime("%Y-%m-%d %H:%M")
                entry_node.add(
                    f"{tag}  {ts_short}  [dim]{r.symlink_count} outputs[/dim]",
                    data=f"run:{r.run_id}",
                    allow_expand=False,
                )

    # ── Run view (live or replayed) ──

    def _show_run_view(self, title: str) -> None:
        """Reset to an empty span tree, ready for events."""
        self._reset_span_state()
        self.sub_title = title
        self.refresh_bindings()
        tree = self.query_one("#tree", TextualTree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        self._update_detail("")

    def _show_run(self, run_id: str) -> None:
        """Replay a stored run's trace.jsonl into the tree."""
        run_info = self._runs_by_id.get(run_id)
        if run_info is None:
            return
        self._current_run_id = run_id
        ts_short = run_info.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        self._show_run_view(f"{run_info.entry_name}  {ts_short}")

        trace_path = os.path.join(run_info.path, "trace.jsonl")
        if not os.path.exists(trace_path):
            return
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._apply_event(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # ── Event handling (shared by live and replay) ──

    def _apply_event(self, e: dict[str, Any]) -> None:
        """Mutate span state + tree in response to one event."""
        kind: str = e["e"]
        span_id: int = e.get("id", 0)
        ts: float = e.get("ts", 0.0)

        if self.span_first_ts is None:
            self.span_first_ts = ts

        tree = self.query_one("#tree", TextualTree)

        if kind == "spawn":
            name = e.get("name", "?")
            args_str: str = e.get("args", "")
            is_memo: bool = e.get("memo", False)
            self.span_names[span_id] = name
            self.span_parents[span_id] = e.get("parent")
            self.span_args[span_id] = args_str
            self.span_traces[span_id] = []
            self.span_spawn_times[span_id] = ts
            self.span_status[span_id] = "pending"
            if is_memo:
                self.span_memo.add(span_id)

            parent_id = self.span_parents.get(span_id)
            parent_node = self.span_tree_nodes.get(parent_id) if parent_id is not None else None  # type: ignore[arg-type]
            if parent_node is None:
                parent_node = tree.root

            node = parent_node.add(self._render_label(span_id), data=f"span:{span_id}")
            node.expand()
            self.span_tree_nodes[span_id] = node

        elif kind == "start":
            self.span_start_times[span_id] = ts
            self.span_status[span_id] = "running"
            self._set_label(span_id)

        elif kind == "end":
            cached = e.get("cached", False)
            cache_key = e.get("cache_key")
            if cache_key:
                self.span_cache_keys[span_id] = cache_key
            self.span_end_times[span_id] = ts
            self.span_status[span_id] = "cached" if cached else "ok"
            dur = ""
            if span_id in self.span_start_times:
                dur = self._format_duration(ts - self.span_start_times[span_id])
            suffix = f"cached {dur}".strip() if cached else dur
            self._set_label(span_id, suffix)
            if cached:
                node = self.span_tree_nodes.get(span_id)
                if node is not None:
                    node.remove_children()
                    node.allow_expand = False

        elif kind == "error":
            full_err = str(e.get("err", "error"))
            self.span_errors[span_id] = full_err
            self.span_end_times[span_id] = ts
            self.span_status[span_id] = "error"
            short = full_err if len(full_err) <= 50 else full_err[:47] + "..."
            self._set_label(span_id, short)

        elif kind == "cancel":
            self.span_end_times[span_id] = ts
            self.span_status[span_id] = "cancelled"
            self._set_label(span_id, "cancelled")

        elif kind == "trace":
            parent_id = e.get("parent", 0)
            msg_text = e.get("msg", "")
            if not msg_text:
                return

            if parent_id in self.span_traces:
                self.span_traces[parent_id].append({"msg": msg_text, "ts": ts, **e})

            parent_node = self.span_tree_nodes.get(parent_id)
            if parent_node is None:
                return

            display = Text()
            if e.get("progress"):
                cur, total = e["progress"][0], e["progress"][1]
                filled = int(10 * cur / total)
                display.append(
                    f"[{'█' * filled}{'░' * (10 - filled)}] {msg_text} ({cur}/{total})",
                    style="dim",
                )
            else:
                display.append(msg_text, style="dim")

            parent_node.add(display, data=f"span:{parent_id}", allow_expand=False)

        # Refresh detail if the event's subject (the span itself, or for
        # traces its parent) is the highlighted span — or a direct child of it.
        if self.highlighted_span is not None:
            subject = e.get("parent", 0) if kind == "trace" else span_id
            if (
                subject == self.highlighted_span
                or self.span_parents.get(subject) == self.highlighted_span
            ):
                self._refresh_detail(self.highlighted_span)

    def _render_label(
        self, span_id: int, suffix: str = "", status: str | None = None
    ) -> Text:
        """Build a tree/timeline label for a span at its current (or given) status."""
        if status is None:
            status = self.span_status.get(span_id, "pending")
        icon, style = self.STATUS_ICONS[status]
        name = self.span_names.get(span_id, f"task-{span_id}")
        args_str = self.span_args.get(span_id, "")
        label = Text()
        label.append(f"{icon} ", style=style)
        label.append(name, style="bold" if style != "dim" else "dim")
        if args_str:
            label.append(f"({args_str})", style="dim")
        if suffix:
            label.append(f" {suffix}", style="dim")
        return label

    def _set_label(self, span_id: int, suffix: str = "") -> None:
        node = self.span_tree_nodes.get(span_id)
        if node is not None:
            node.set_label(self._render_label(span_id, suffix))

    def _format_duration(self, seconds: float) -> str:
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        return f"{seconds:.1f}s"

    def _refresh_detail(self, span_id: int) -> None:
        status = self.span_status.get(span_id, "pending")

        # Header: span label + duration if terminal
        suffix = ""
        if status in self.TERMINAL_STATUSES and span_id in self.span_start_times:
            dur = self.span_end_times.get(span_id, 0) - self.span_start_times[span_id]
            if dur > 0:
                suffix = f"{dur:.3f}s"
        out = Text()
        out.append(self._render_label(span_id, suffix))
        out.append("\n\n")

        # Error details (full text, not truncated)
        if status == "error":
            err = self.span_errors.get(span_id, "")
            if err:
                out.append("Error:\n", style="bold red")
                out.append(f"{err}\n\n", style="red")

        # Result (if we have a cached output file)
        cache_key = self.span_cache_keys.get(span_id)
        if cache_key and status in ("ok", "cached"):
            output_path = os.path.join(self._store_path, "outputs", f"{cache_key}.json")
            if os.path.exists(output_path):
                with open(output_path, "r") as f:
                    data: dict[str, Any] = json.load(f)
                result = data.get("result")
                result_str = result if isinstance(result, str) else json.dumps(result, indent=2)
                out.append("Result: ", style="bold")
                out.append(f"{result_str}\n\n")

        # Timeline: traces + each child's start and terminal events
        timeline: list[tuple[float, Text]] = []

        last_progress = ""
        for t in self.span_traces.get(span_id, []):
            ts = t.get("ts", 0.0)
            msg = t.get("msg", "")
            progress = t.get("progress")
            if progress:
                last_progress = f" ({progress[0]}/{progress[1]})"
            entry = Text(f"{msg}{last_progress}", style="dim")
            timeline.append((ts, entry))

        children = [sid for sid, pid in self.span_parents.items() if pid == span_id]
        for cid in children:
            cstatus = self.span_status.get(cid, "pending")
            if cid in self.span_start_times:
                timeline.append(
                    (self.span_start_times[cid], self._render_label(cid, status="running"))
                )
            if cstatus in self.TERMINAL_STATUSES:
                end_ts = self.span_end_times.get(cid, 0.0)
                start_ts = self.span_start_times.get(cid, end_ts)
                dur = end_ts - start_ts
                dur_str = f"{dur:.3f}s" if dur > 0.001 else ""
                extra = f"cached {dur_str}".strip() if cstatus == "cached" else dur_str
                timeline.append((end_ts, self._render_label(cid, extra, status=cstatus)))

        timeline.sort(key=lambda x: x[0])
        if timeline:
            base_ts = timeline[0][0]
            for ts, entry in timeline:
                elapsed = ts - base_ts
                out.append(f"  {elapsed:7.3f}s ")
                out.append(entry)
                out.append("\n")

        self._update_detail(out)

    # ── Tree interactions ──

    @on(TextualTree.NodeSelected)
    def on_node_selected(self, event: TextualTree.NodeSelected[str]) -> None:
        data = event.node.data
        if data is None:
            return
        data_str = str(data)
        if data_str.startswith("run:") and self._current_run_id is None:
            self._show_run(data_str[4:])

    @on(TextualTree.NodeHighlighted)
    def on_node_highlighted(self, event: TextualTree.NodeHighlighted[str]) -> None:
        data = event.node.data
        if data is None:
            return
        data_str = str(data)
        if data_str.startswith("span:"):
            span_id = int(data_str[5:])
            self.highlighted_span = span_id
            self._refresh_detail(span_id)
        elif data_str.startswith("run:"):
            self.highlighted_span = None
            run_info = self._runs_by_id.get(data_str[4:])
            if run_info:
                self._update_detail(
                    f"[bold]{run_info.entry_name}[/bold]\n"
                    f"[dim]{run_info.timestamp}[/dim]\n"
                    f"[dim]{run_info.symlink_count} outputs[/dim]\n\n"
                    f"[dim]Press Enter to open[/dim]"
                )
        elif data_str.startswith("entry:"):
            self.highlighted_span = None
            self._update_detail("")

    # ── Navigation ──

    def action_go_back(self) -> None:
        if self._live_active:
            return
        if self._current_run_id is not None:
            self._show_selector()

    # ── Live pipeline ──

    def _start_pipeline(self) -> None:
        entry_fn = self._entry_fn
        assert entry_fn is not None

        def worker() -> None:
            import asyncio
            rm = RunManager(self._store_path, self._label)
            tracker = SymlinkTracker(rm, rm.sink)
            tui_sink = TuiSink(self)
            sink = CompositeSink(tracker, tui_sink)

            async def _run() -> Any:
                store_token = set_store(rm.store)
                sink_token = set_sink(sink)
                try:
                    handle = entry_fn()
                    return await handle
                finally:
                    from cairn import context, core
                    core._store.reset(store_token)  # type: ignore[attr-defined]
                    context._sink.reset(sink_token)  # type: ignore[attr-defined]
                    rm.close()

            try:
                result = asyncio.run(_run())
                self.call_from_thread(self.post_message, PipelineDone(result=result))
            except Exception as e:
                self.call_from_thread(self.post_message, PipelineDone(error=str(e)))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    @on(PipelineEvent)
    def on_pipeline_event(self, msg: PipelineEvent) -> None:
        self._apply_event(msg.event_dict)

    @on(PipelineDone)
    def on_pipeline_done(self, event: PipelineDone) -> None:
        self._live_active = False
        if event.error:
            self._update_detail(f"[red]Error: {event.error}[/red]")
            self.notify(f"Failed: {event.error}", severity="error")
        else:
            result_str = str(event.result)
            if len(result_str) > 1000:
                result_str = result_str[:997] + "..."
            self._update_detail(
                f"[green]Pipeline complete[/green]\n\n[bold]Result:[/bold]\n{result_str}"
            )
            self.notify("Pipeline complete")

    def action_copy_detail(self) -> None:
        if self._detail_plain:
            self.copy_to_clipboard(self._detail_plain)
            self.notify("Detail copied to clipboard")


# ── Entry points ──


def run_app(entry_fn: Any, store_path: str = ".cairn", label: str = "main") -> None:
    app = CairnApp(store_path, entry_fn=entry_fn, label=label)
    app.run()


def browse(store_path: str = ".cairn") -> None:
    app = CairnApp(store_path)
    app.run()
