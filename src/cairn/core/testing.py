"""Test utilities for Cairn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .context import Event, MemorySink, reset_id_counter, set_sink
from cairn.core import set_store
from .hash import clear_hash_funcs, set_hash_funcs
from .store import MemoryStore


@dataclass
class SpanInfo:
    """Summary info about a completed span."""

    id: int
    name: str
    parent_id: int | None
    identity: str
    cached: bool
    start_ts: float
    end_ts: float


class TraceInspector:
    """Convenience API for inspecting trace events in tests."""

    def __init__(self, sink: MemorySink) -> None:
        self._sink = sink

    @property
    def all_events(self) -> list[Event]:
        """All events in order."""
        return list(self._sink.events)

    def events(self, kind: str) -> list[Event]:
        """Get all events of a given kind."""
        return [e for e in self._sink.events if e.kind == kind]

    def span(self, name: str) -> SpanInfo:
        """Find a span by step name. Returns the first match."""
        spawns = [e for e in self._sink.events if e.kind == "spawn" and e.name == name]
        if not spawns:
            raise KeyError(f"No span found with name {name!r}")
        spawn = spawns[0]
        span_id = spawn.id
        assert span_id is not None

        ends = [e for e in self._sink.events if e.kind == "end" and e.id == span_id]
        end_ts = ends[0].ts if ends else 0.0
        cached = ends[0].cached if ends and ends[0].cached is not None else False

        identity_str: str = spawn.kwargs.get("identity", "")

        return SpanInfo(
            id=span_id,
            name=name,
            parent_id=spawn.parent_id,
            identity=identity_str,
            cached=cached,
            start_ts=spawn.ts,
            end_ts=end_ts,
        )

    def span_name(self, span_id: int) -> str | None:
        """Get the name of a span by its ID."""
        for e in self._sink.events:
            if e.kind == "spawn" and e.id == span_id:
                return e.name
        return None

    def child_events(self, parent_id: int, kind: str) -> list[Event]:
        """Get events of a given kind that are children of a span.

        For join events, matches on the 'by' field (who awaited).
        For other events, matches on parent_id.
        """
        if kind == "join":
            return [e for e in self._sink.events if e.kind == kind and e.by == parent_id]
        return [e for e in self._sink.events if e.kind == kind and e.parent_id == parent_id]

    def edge_annotations(self, parent_name: str) -> list[Event]:
        """Get trace events with edge=True under a named parent."""
        parent = self.span(parent_name)
        return [
            e
            for e in self._sink.events
            if e.kind == "trace"
            and e.parent_id == parent.id
            and e.kwargs.get("edge") is True
        ]

    def total_executions(self) -> int:
        """Count total start events (real executions, not cached)."""
        return len([e for e in self._sink.events if e.kind == "start"])

    def cached_count(self) -> int:
        """Count cached end events."""
        return len([e for e in self._sink.events if e.kind == "end" and e.cached is True])


class Runtime:
    """Test runtime context manager.

    Sets up an in-memory store and sink, provides trace inspection.
    """

    def __init__(
        self,
        hash_funcs: dict[type, Callable[[Any], Any]] | None = None,
    ) -> None:
        self._store = MemoryStore()
        self._sink = MemorySink()
        self._hash_funcs = hash_funcs or {}
        self._store_token: Any = None
        self._sink_token: Any = None
        self.trace = TraceInspector(self._sink)

    async def __aenter__(self) -> Runtime:
        reset_id_counter()
        self._store_token = set_store(self._store)
        self._sink_token = set_sink(self._sink)
        if self._hash_funcs:
            set_hash_funcs(self._hash_funcs)
        return self

    async def __aexit__(self, *args: Any) -> None:
        from ._step import reset_store
        from .context import reset_sink

        if self._store_token is not None:
            reset_store(self._store_token)
        if self._sink_token is not None:
            reset_sink(self._sink_token)
        clear_hash_funcs()
