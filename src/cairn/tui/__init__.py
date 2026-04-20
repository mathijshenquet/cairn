"""Textual TUI for running and browsing Cairn pipelines."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
import threading
from typing import Any, Callable, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header, Input, Static
from textual.widgets import Tree as TextualTree
from textual.widgets.tree import TreeNode

from cairn.core import Event, Handle, event_to_dict, set_sink, set_store
from cairn.interaction import InputRequest, set_interaction_sink
from cairn.run import CompositeSink, RunInfo, RunManager, SymlinkTracker, list_runs
from cairn.run.show import TRACE_RESERVED, format_cost
from cairn.run.spans import SpanGraph


def _trace_style(level: str) -> str:
    if level == "error":
        return "red"
    if level == "warn":
        return "yellow"
    return "dim"


def _render_trace_text(e: dict[str, Any]) -> Text:
    """Render a trace event to a styled Text based on blessed kwargs."""
    msg: str = e.get("msg", "")
    level: str = e.get("level", "info")
    state: str | None = e.get("state")
    progress: list[int] | None = e.get("progress")
    cost: dict[str, Any] | None = e.get("cost")

    parts: list[str] = []
    if msg:
        parts.append(msg)
    if progress:
        parts.append(f"({progress[0]}/{progress[1]})")
    if state:
        parts.append(f"[{state}]")
    if cost:
        parts.append(format_cost(cost))

    attrs = {k: v for k, v in e.items() if k not in TRACE_RESERVED}
    if attrs:
        kv = " ".join(f"{k}={v}" for k, v in attrs.items())
        parts.append(f"({kv})")

    return Text(" ".join(parts), style=_trace_style(level))


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


class InputRequestMessage(Message):
    """Pipeline is asking for input — routed from worker thread."""

    def __init__(
        self,
        req: InputRequest,
        fut: concurrent.futures.Future[Any],
        span_id: int | None,
    ) -> None:
        super().__init__()
        self.req = req
        self.fut = fut
        self.span_id = span_id


# ── Thread-safe sink that posts to Textual ──


class TuiSink:
    """Sink that posts events to a Textual app from any thread."""

    def __init__(self, app: CairnApp) -> None:
        self._app = app

    def emit(self, event: Event) -> None:
        event.ts = time.monotonic()
        d = event_to_dict(event)
        self._app.call_from_thread(self._app.post_message, PipelineEvent(d))


class TuiInteractionSink:
    """InteractionSink adapter that routes requests to the Textual app.

    The pipeline worker runs in its own thread + asyncio loop; Textual runs
    in the main thread. We use a concurrent.futures.Future to bridge: post
    a message into the app, then `asyncio.wrap_future(fut)` so the worker's
    await yields until the main thread resolves the future on submit.
    """

    def __init__(self, app: CairnApp) -> None:
        self._app = app

    async def request(self, req: InputRequest) -> Any:
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._app.call_from_thread(
            self._app.post_message, InputRequestMessage(req, fut, req.anchor_span)
        )
        return await asyncio.wrap_future(fut)


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
        self._pending_inputs: dict[int, concurrent.futures.Future[Any]] = {}
        # span_id → the Input widget awaiting response on that span
        self._pending_input_widgets: dict[int, Input] = {}
        self._reset_span_state()

    @property
    def _tree(self) -> TextualTree[str]:
        # query_one uses isinstance() on the expect_type, which fails with
        # subscripted generics — pass the base class and cast to the parameterized form.
        return cast(TextualTree[str], self.query_one("#tree", TextualTree))

    def _update_detail(self, content: "Text | str") -> None:
        detail = self.query_one("#detail", Static)
        detail.update(content)
        if isinstance(content, Text):
            self._detail_plain = content.plain
        else:
            self._detail_plain = Text.from_markup(content).plain

    # Effective-status values rendered by `_render_label`. A superset of the
    # raw Span.status: `awaiting_input` is propagated by
    # `SpanGraph.effective_status` up the wait-chain.
    STATUS_ICONS: dict[str, tuple[str, str]] = {
        "pending": ("○", "dim"),
        "running": ("◉", "yellow"),
        "awaiting_input": ("◐", "bold cyan"),
        "cached": ("⚡", "green"),
        "ok": ("✓", "green"),
        "error": ("✗", "red"),
        "cancelled": ("⊘", "dim"),
    }
    TERMINAL_STATUSES = frozenset({"cached", "ok", "error", "cancelled"})

    def _reset_span_state(self) -> None:
        self.graph: SpanGraph = SpanGraph()
        self.span_tree_nodes: dict[int, TreeNode[str]] = {}
        self.highlighted_span: int | None = None
        # When the user navigates onto a specific trace node, remember which
        # trace to expand in the detail pane. Otherwise (span-level select),
        # we default to expanding the virtual "Result" / last-detail trace.
        self.selected_trace: tuple[int, int] | None = None  # (span_id, trace_idx)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield TextualTree[str]("Cairn", id="tree")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail")
        yield Footer()

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],  # pyright: ignore[reportUnusedParameter] -- name must match DOMNode override
    ) -> bool | None:
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

        tree = self._tree
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
        tree = self._tree
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
        """Feed an event into the SpanGraph and reflect the change in the tree."""
        self.graph.apply(e)
        kind: str = e.get("e", "")
        tree = self._tree

        if kind == "spawn":
            span_id = int(e["id"])
            parent_id = e.get("parent")
            parent_node = (
                self.span_tree_nodes.get(int(parent_id))
                if parent_id is not None
                else None
            )
            if parent_node is None:
                parent_node = tree.root
            node = parent_node.add(self._render_label(span_id), data=f"span:{span_id}")
            node.expand()
            self.span_tree_nodes[span_id] = node

        elif kind == "start":
            self._refresh_label_chain(int(e["id"]))

        elif kind == "end":
            span_id = int(e["id"])
            s = self.graph.spans.get(span_id)
            dur_str = ""
            if s is not None and s.start_ts is not None and s.end_ts is not None:
                dur_str = self._format_duration(s.end_ts - s.start_ts)
            cached = s is not None and s.status == "cached"
            suffix = (f"cached {dur_str}".strip() if cached else dur_str)
            self._set_label(span_id, suffix)
            if cached:
                node = self.span_tree_nodes.get(span_id)
                if node is not None:
                    node.remove_children()
                    node.allow_expand = False
            self._refresh_label_chain(span_id, include_self=False)

        elif kind == "error":
            span_id = int(e["id"])
            s = self.graph.spans.get(span_id)
            err = (s.error if s is not None else None) or "error"
            short = err if len(err) <= 50 else err[:47] + "..."
            self._set_label(span_id, short)
            self._refresh_label_chain(span_id, include_self=False)

        elif kind == "cancel":
            span_id = int(e["id"])
            self._set_label(span_id, "cancelled")
            self._refresh_label_chain(span_id, include_self=False)

        elif kind == "trace":
            parent_id = e.get("parent")
            if parent_id is None:
                return
            parent_node = self.span_tree_nodes.get(int(parent_id))
            if parent_node is None:
                return
            s = self.graph.spans.get(int(parent_id))
            trace_idx = (len(s.traces) - 1) if s is not None else -1
            rec = s.traces[-1] if s is not None and s.traces else {}
            display = _render_trace_text(rec)
            if display.plain:
                node_data = (
                    f"trace:{parent_id}:{trace_idx}"
                    if trace_idx >= 0
                    else f"span:{parent_id}"
                )
                parent_node.add(display, data=node_data, allow_expand=False)

        elif kind in ("wait", "resume"):
            self._refresh_label_chain(int(e["id"]))

        # Refresh detail if the highlighted span (or a direct child) changed.
        if self.highlighted_span is not None:
            subject: int | None
            if kind == "trace":
                parent = e.get("parent")
                subject = int(parent) if parent is not None else None
            else:
                sid = e.get("id")
                subject = int(sid) if sid is not None else None
            if subject is not None:
                subj_span = self.graph.spans.get(subject)
                if (
                    subject == self.highlighted_span
                    or (subj_span is not None and subj_span.parent == self.highlighted_span)
                ):
                    self._refresh_detail(self.highlighted_span)

    def _render_label(
        self, span_id: int, suffix: str = "", status: str | None = None
    ) -> Text:
        """Build a tree/timeline label for a span at its current (or given) status."""
        s = self.graph.spans.get(span_id)
        if status is None:
            status = self.graph.effective_status(span_id) if s is not None else "pending"
        icon, style = self.STATUS_ICONS.get(status, self.STATUS_ICONS["pending"])
        name = s.name if s is not None else f"task-{span_id}"
        args_str = s.args if s is not None else ""
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

    def _refresh_label_chain(self, span_id: int, include_self: bool = True) -> None:
        """Refresh labels on a span and all its ancestors.

        Any status transition on a descendant can change an ancestor's
        effective_status (via wait-chain propagation), so ancestors need
        relabeling whenever their subtree's status shifts.
        """
        if include_self:
            self._set_label(span_id)
        cur = self.graph.spans.get(span_id)
        while cur is not None and cur.parent is not None:
            self._set_label(cur.parent)
            cur = self.graph.spans.get(cur.parent)

    def _format_duration(self, seconds: float) -> str:
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        return f"{seconds:.1f}s"

    def _refresh_detail(self, span_id: int) -> None:
        s = self.graph.spans.get(span_id)
        if s is None:
            self._update_detail("")
            return
        status = self.graph.effective_status(span_id)

        # Header: span label + duration if terminal
        suffix = ""
        if status in self.TERMINAL_STATUSES and s.start_ts is not None and s.end_ts is not None:
            dur = s.end_ts - s.start_ts
            if dur > 0:
                suffix = f"{dur:.3f}s"
        out = Text()
        out.append(self._render_label(span_id, suffix))
        out.append("\n\n")

        if status == "error" and s.error:
            out.append("Error:\n", style="bold red")
            out.append(f"{s.error}\n\n", style="red")

        traces = s.traces

        # Result (from cached output) — acts as the virtual last trace.
        cache_key = s.cache_key
        result_str: str | None = None
        if cache_key and status in ("ok", "cached"):
            output_path = os.path.join(self._store_path, "outputs", f"{cache_key}.json")
            if os.path.exists(output_path):
                try:
                    with open(output_path, "r") as f:
                        data: dict[str, Any] = json.load(f)
                    result = data.get("result")
                    result_str = (
                        result if isinstance(result, str) else json.dumps(result, indent=2)
                    )
                except (OSError, json.JSONDecodeError):
                    result_str = None

        # Which trace (if any) to expand inline. Result is always expanded.
        selected_trace_idx: int | None = None
        if self.selected_trace is not None and self.selected_trace[0] == span_id:
            selected_trace_idx = self.selected_trace[1]

        # Timeline entries: (ts, label, kind, trace_idx_or_None, detail_or_None)
        timeline: list[tuple[float, Text, str, int | None, str | None]] = []

        for i, t in enumerate(traces):
            ts = t.get("ts", 0.0)
            label = _render_trace_text(t)
            detail_raw = t.get("detail")
            detail_str: str | None = str(detail_raw) if detail_raw else None
            timeline.append((ts, label, "trace", i, detail_str))

        for cid in self.graph.children(span_id):
            cs = self.graph.spans.get(cid)
            if cs is None:
                continue
            cstatus = self.graph.effective_status(cid)
            if cs.start_ts is not None:
                timeline.append(
                    (cs.start_ts, self._render_label(cid, status="running"),
                     "child", None, None)
                )
            if cstatus in self.TERMINAL_STATUSES and cs.end_ts is not None:
                start_ts = cs.start_ts if cs.start_ts is not None else cs.end_ts
                dur = cs.end_ts - start_ts
                dur_str = f"{dur:.3f}s" if dur > 0.001 else ""
                extra = f"cached {dur_str}".strip() if cstatus == "cached" else dur_str
                timeline.append(
                    (cs.end_ts, self._render_label(cid, extra, status=cstatus),
                     "child", None, None)
                )

        timeline.sort(key=lambda x: x[0])

        base_ts = timeline[0][0] if timeline else 0.0
        prefix_pad = "            "  # gutter aligned roughly with trace label column

        if timeline or result_str is not None:
            for ts, label, kind, idx, detail_str in timeline:
                elapsed = ts - base_ts
                out.append(f"  {elapsed:7.3f}s  ")
                out.append(label)
                out.append("\n")
                if kind == "trace" and detail_str and idx == selected_trace_idx:
                    for line in detail_str.splitlines() or [detail_str]:
                        out.append(f"{prefix_pad}{line}\n", style="dim")

            # Mark completion in the timeline (aligned with the gutter), then
            # surface the Result as a flush-left section below — intentionally
            # unaligned with the trace column so long results aren't squeezed.
            if status in ("ok", "cached") and s.end_ts is not None:
                elapsed = s.end_ts - base_ts
                out.append(f"  {elapsed:7.3f}s  ")
                out.append("Completed\n", style="bold")

            if result_str is not None:
                out.append("\nResult:\n", style="bold")
                for line in result_str.splitlines() or [result_str]:
                    out.append(f"{line}\n")

        rolled = self.graph.rolled_cost(span_id)
        if rolled:
            out.append("\nCosts:\n", style="bold")
            key_w = max(len(k) for k in rolled)
            for k, v in rolled.items():
                val = f"{v:g}" if isinstance(v, float) else str(v)
                out.append(f"  {k.ljust(key_w)}  {val}\n", style="dim")

        self._update_detail(out)
        self._sync_input_visibility(span_id)

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
            self.selected_trace = None
            self._refresh_detail(span_id)
        elif data_str.startswith("trace:"):
            _, sid, tidx = data_str.split(":", 2)
            span_id = int(sid)
            trace_idx = int(tidx)
            self.highlighted_span = span_id
            self.selected_trace = (span_id, trace_idx)
            self._refresh_detail(span_id)
        elif data_str.startswith("run:"):
            self.highlighted_span = None
            self._sync_input_visibility(None)
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
            self._sync_input_visibility(None)
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
                interaction_token = set_interaction_sink(TuiInteractionSink(self))
                try:
                    handle = entry_fn()
                    return await handle
                finally:
                    from cairn.core import reset_sink, reset_store
                    from cairn.interaction import reset_interaction_sink

                    reset_interaction_sink(interaction_token)
                    reset_store(store_token)
                    reset_sink(sink_token)
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
            return
        self.notify("Pipeline complete")
        # Leave the tree cursor and detail pane where the user put them —
        # the Result is always reachable by highlighting the root span.

    def action_copy_detail(self) -> None:
        if self._detail_plain:
            self.copy_to_clipboard(self._detail_plain)
            self.notify("Detail copied to clipboard")

    # ── Interaction sink wiring ──

    @on(InputRequestMessage)
    def on_input_request(self, msg: InputRequestMessage) -> None:
        """Mount an Input widget (hidden by default) tied to the awaiting span.

        Visibility is driven by selection: the widget becomes visible only
        when the user navigates onto the span that's awaiting input.
        """
        self._pending_inputs[msg.req.id] = msg.fut
        scroll = self.query_one("#detail-scroll", VerticalScroll)
        placeholder = msg.req.metadata.get("placeholder") if msg.req.metadata else None
        prefill = str(msg.req.default) if msg.req.has_default else ""
        widget = Input(
            value=prefill,
            placeholder=str(placeholder) if placeholder else msg.req.prompt,
            id=f"input-{msg.req.id}",
        )
        if msg.span_id is not None:
            self._pending_input_widgets[msg.span_id] = widget
        scroll.mount(widget)
        visible = msg.span_id is None or self.highlighted_span == msg.span_id
        widget.display = visible
        if visible:
            widget.focus()
        elif msg.span_id is not None and not isinstance(self.focused, Input):
            # Not currently editing an input — navigate to this span so the
            # new widget gets focus via the highlight → _sync_input_visibility
            # chain. Skipped if the user is already typing somewhere.
            node = self.span_tree_nodes.get(msg.span_id)
            if node is not None:
                self._tree.select_node(node)

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        widget_id = event.input.id
        if not widget_id or not widget_id.startswith("input-"):
            return
        req_id = int(widget_id[len("input-"):])
        fut = self._pending_inputs.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result(event.value)
        for sid, w in list(self._pending_input_widgets.items()):
            if w is event.input:
                del self._pending_input_widgets[sid]
                break
        event.input.remove()

        # Refocus: next pending input in tree DFS order, else the tree itself.
        tree = self._tree
        next_span = self._next_pending_input_span()
        if next_span is not None:
            node = self.span_tree_nodes.get(next_span)
            if node is not None:
                # Selecting moves the cursor, which fires NodeHighlighted →
                # _sync_input_visibility focuses the pending Input for us.
                tree.select_node(node)
                return
        tree.focus()

    def _next_pending_input_span(self) -> int | None:
        """Next span awaiting input, in DFS tree order, cycling past current."""
        if not self._pending_input_widgets:
            return None
        tree = self._tree
        order: list[int] = []
        stack: list[TreeNode[str]] = list(reversed(list(tree.root.children)))
        while stack:
            node = stack.pop()
            data = node.data
            if data is not None and data.startswith("span:"):
                sid = int(data[5:])
                if sid in self._pending_input_widgets:
                    order.append(sid)
            stack.extend(reversed(list(node.children)))
        if not order:
            return None
        if self.highlighted_span in order:
            i = order.index(self.highlighted_span)
            return order[(i + 1) % len(order)]
        return order[0]

    def _sync_input_visibility(self, span_id: int | None) -> None:
        """Show only the pending Input widget belonging to span_id (if any)."""
        target = (
            self._pending_input_widgets.get(span_id) if span_id is not None else None
        )
        for w in self._pending_input_widgets.values():
            w.display = w is target
        if target is not None:
            target.focus()


# ── Entry points ──


def run_app(entry_fn: Any, store_path: str = ".cairn", label: str = "main") -> None:
    app = CairnApp(store_path, entry_fn=entry_fn, label=label)
    app.run()


def browse(store_path: str = ".cairn") -> None:
    app = CairnApp(store_path)
    app.run()
