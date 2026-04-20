"""Span-tree reducer over the trace.jsonl event stream.

Shared by `show.LiveRenderer` and `tui.CairnApp`. Consumes events via
`SpanGraph.apply(event)` and exposes queries for derived state:

* `effective_status(id)` — wait-chain propagation: a parent blocked on a child
  blocked on a human surfaces as `awaiting_input`. Group waits reduce over
  their members.
* `pending_inputs_under(id)` — spawn-subtree scan for open input waits that
  wait-chain would miss (background siblings not yet awaited).
* `input_owner(request_id)` — the span that should host the input widget.

The reducer makes no UI decisions. Consumers render against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast


WaitKind = Literal["span", "group", "input"]
SpanStatus = Literal["pending", "running", "ok", "cached", "error", "cancelled"]

# Effective statuses surfaced by `effective_status(...)`. Superset of the raw
# statuses — includes the propagated "awaiting_*" variants.
EffectiveStatus = Literal[
    "pending", "running", "awaiting_input", "awaiting_group",
    "ok", "cached", "error", "cancelled",
]


@dataclass
class Wait:
    kind: WaitKind
    target: int | list[int]   # span id | list of span ids | input_request id


@dataclass
class Span:
    id: int
    parent: int | None
    name: str
    args: str = ""
    memo: bool = False
    status: SpanStatus = "pending"
    spawn_ts: float | None = None
    start_ts: float | None = None
    end_ts: float | None = None
    cache_key: str | None = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    traces: list[dict[str, Any]] = field(default_factory=lambda: cast(list[dict[str, Any]], []))


@dataclass
class InputInfo:
    id: int
    prompt: str
    owner: int | None             # anchor span, from `by` on input_request
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    pending: bool = True


@dataclass
class Transition:
    """What changed in response to a single applied event."""
    kind: str                     # the event kind
    span_id: int | None = None    # primary span affected
    parent_id: int | None = None  # for trace events: the span the trace annotates
    request_id: int | None = None # for input_request/response
    added_trace: dict[str, Any] | None = None


# Precedence for group-wait reduction. Higher wins.
_PRIORITY: dict[str, int] = {
    "error": 6,
    "awaiting_input": 5,
    "awaiting_group": 4,
    "running": 3,
    "pending": 2,
    "cancelled": 1,
    "ok": 0,
    "cached": 0,
}


class SpanGraph:
    """Mutable span-tree state built up from a trace event stream."""

    def __init__(self) -> None:
        self.spans: dict[int, Span] = {}
        self.open_waits: dict[int, list[Wait]] = {}      # span id → stack
        self.inputs: dict[int, InputInfo] = {}           # input req id → info
        self.first_ts: float | None = None

    # ── Reducer ──

    def apply(self, e: dict[str, Any]) -> Transition:
        kind = e.get("e", "")
        ts = e.get("ts", 0.0)
        if self.first_ts is None and ts:
            self.first_ts = ts

        if kind == "spawn":
            span_id = int(e["id"])
            s = Span(
                id=span_id,
                parent=e.get("parent"),
                name=e.get("name", "?"),
                args=str(e.get("args", "")),
                memo=bool(e.get("memo", False)),
                spawn_ts=ts,
            )
            self.spans[span_id] = s
            return Transition(kind, span_id=span_id, parent_id=s.parent)

        if kind == "start":
            span_id = int(e["id"])
            s = self.spans.get(span_id)
            if s is not None:
                s.start_ts = ts
                s.status = "running"
            return Transition(kind, span_id=span_id)

        if kind in ("end", "error", "cancel"):
            span_id = int(e["id"])
            s = self.spans.get(span_id)
            if s is not None:
                s.end_ts = ts
                if kind == "end":
                    s.status = "cached" if e.get("cached") else "ok"
                    s.cache_key = e.get("cache_key")
                elif kind == "error":
                    s.status = "error"
                    s.error = str(e.get("err", "error"))
                else:
                    s.status = "cancelled"
                for mk in ("size", "own_size", "time", "own_time"):
                    if mk in e:
                        s.metrics[mk] = e[mk]
            # terminal: drop any lingering waits
            self.open_waits.pop(span_id, None)
            return Transition(kind, span_id=span_id)

        if kind == "wait":
            span_id = int(e["id"])
            on_raw = e.get("on")
            on: dict[str, Any] = cast(dict[str, Any], on_raw) if isinstance(on_raw, dict) else {}
            wkind = on.get("kind")
            w: Wait | None = None
            if wkind == "span" and "id" in on:
                w = Wait(kind="span", target=int(on["id"]))
            elif wkind == "group":
                ids_raw = cast(list[Any], on.get("ids") or [])
                w = Wait(kind="group", target=[int(x) for x in ids_raw])
            elif wkind == "input" and "id" in on:
                w = Wait(kind="input", target=int(on["id"]))
            if w is not None:
                self.open_waits.setdefault(span_id, []).append(w)
            return Transition(kind, span_id=span_id)

        if kind == "resume":
            span_id = int(e["id"])
            stack = self.open_waits.get(span_id)
            if stack:
                stack.pop()
                if not stack:
                    del self.open_waits[span_id]
            return Transition(kind, span_id=span_id)

        if kind == "trace":
            parent_id = e.get("parent")
            rec = {k: v for k, v in e.items() if k != "e"}
            if parent_id is not None and int(parent_id) in self.spans:
                self.spans[int(parent_id)].traces.append(rec)
            return Transition(
                kind,
                parent_id=int(parent_id) if parent_id is not None else None,
                added_trace=rec,
            )

        if kind == "input_request":
            req_id = int(e["id"])
            owner = e.get("by")
            meta = {
                k: v for k, v in e.items()
                if k not in ("e", "ts", "id", "msg", "by")
            }
            self.inputs[req_id] = InputInfo(
                id=req_id,
                prompt=str(e.get("msg", "")),
                owner=int(owner) if owner is not None else None,
                metadata=meta,
                pending=True,
            )
            return Transition(kind, request_id=req_id, span_id=int(owner) if owner is not None else None)

        if kind == "input_response":
            req_id = int(e["id"])
            info = self.inputs.get(req_id)
            if info is not None:
                info.pending = False
            return Transition(kind, request_id=req_id)

        return Transition(kind)

    # ── Queries ──

    def depth(self, span_id: int) -> int:
        d = 0
        cur = self.spans.get(span_id)
        while cur is not None and cur.parent is not None:
            d += 1
            cur = self.spans.get(cur.parent)
        return d

    def children(self, span_id: int) -> list[int]:
        return [sid for sid, s in self.spans.items() if s.parent == span_id]

    def wait_stack(self, span_id: int) -> list[Wait]:
        return list(self.open_waits.get(span_id, []))

    def effective_status(self, span_id: int) -> str:
        return self._effective(span_id, set())

    def _effective(self, span_id: int, visited: set[int]) -> str:
        if span_id in visited:
            return "running"   # cycle guard, shouldn't happen in well-formed graphs
        visited = visited | {span_id}
        s = self.spans.get(span_id)
        if s is None:
            return "pending"
        if s.status in ("ok", "cached", "error", "cancelled"):
            return s.status
        stack = self.open_waits.get(span_id)
        if stack:
            w = stack[-1]
            if w.kind == "input":
                return "awaiting_input"
            if w.kind == "span":
                assert isinstance(w.target, int)
                return self._effective(w.target, visited)
            if w.kind == "group":
                assert isinstance(w.target, list)
                return self._reduce_group([self._effective(c, visited) for c in w.target])
        return s.status   # "running" or "pending"

    @staticmethod
    def _reduce_group(statuses: list[str]) -> str:
        if not statuses:
            return "ok"
        non_terminal = [
            x for x in statuses
            if x not in ("ok", "cached", "error", "cancelled")
        ]
        if not non_terminal:
            if "error" in statuses:
                return "error"
            if "cancelled" in statuses:
                return "cancelled"
            return "ok"
        # Error bubbles up even if some are non-terminal
        if "error" in statuses:
            return "error"
        return max(non_terminal, key=lambda x: _PRIORITY.get(x, 0))

    def pending_inputs_under(self, span_id: int) -> list[int]:
        """Open input_request ids held anywhere at-or-below `span_id`."""
        seen: set[int] = set()
        frontier: list[int] = [span_id]
        req_ids: list[int] = []
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for w in self.open_waits.get(cur, []):
                if w.kind == "input":
                    assert isinstance(w.target, int)
                    if w.target not in req_ids:
                        req_ids.append(w.target)
            for cid, s in self.spans.items():
                if s.parent == cur:
                    frontier.append(cid)
        return req_ids

    def input_owner(self, request_id: int) -> int | None:
        info = self.inputs.get(request_id)
        return info.owner if info else None

    def rolled_cost(self, span_id: int) -> dict[str, float]:
        """Sum numeric `cost` columns over this span's traces + descendants."""
        total: dict[str, float] = {}
        seen: set[int] = set()

        def walk(sid: int) -> None:
            if sid in seen:
                return
            seen.add(sid)
            s = self.spans.get(sid)
            if s is None:
                return
            for t in s.traces:
                cost = t.get("cost")
                if isinstance(cost, dict):
                    for k, v in cast(dict[str, Any], cost).items():
                        if isinstance(v, (int, float)):
                            total[k] = total.get(k, 0.0) + float(v)
            for cid in self.children(sid):
                walk(cid)

        walk(span_id)
        return total
