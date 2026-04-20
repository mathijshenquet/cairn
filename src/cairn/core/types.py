"""Core types for Cairn."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass, field
from typing import Any


class Identity:
    """Stable identifier for a step function."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._hash = hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def from_function(cls, fn: object) -> Identity:
        module = getattr(fn, "__module__", "<unknown>")
        qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "<unknown>"))
        return cls(f"{module}:{qualname}")

    @property
    def value(self) -> str:
        return self._value

    @property
    def hash(self) -> str:
        return self._hash

    def short(self) -> str:
        return self._hash[:8]

    def long(self) -> str:
        return self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Identity):
            return self._value == other._value
        return NotImplemented

    def __repr__(self) -> str:
        return f"Identity({self._value!r})"


class Version:
    """Version identifier derived from function source."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._hash = hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def from_function(cls, fn: object) -> Version:
        try:
            source = inspect.getsource(fn)  # type: ignore[arg-type]
        except (OSError, TypeError):
            source = f"<no source:{id(fn)}>"
        return cls(source)

    @property
    def hash(self) -> str:
        return self._hash

    def short(self) -> str:
        return self._hash[:8]

    def long(self) -> str:
        return self._hash

    def __hash__(self) -> int:
        return hash(self._hash)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Version):
            return self._hash == other._hash
        return NotImplemented

    def __repr__(self) -> str:
        return f"Version({self._hash[:8]})"


@dataclass
class TraceRecord:
    """A single trace event, stored for cached_tracing() replay."""

    message: str
    timestamp: float
    delta: float
    kwargs: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class CacheEntry:
    """Stored result of a step invocation."""

    result: Any
    traces: list[TraceRecord]
    error: BaseException | None = None
    duration: float = 0.0
    own_duration: float = 0.0


@dataclass(frozen=True)
class SpanMetrics:
    """Size/time metrics emitted on a span's terminal event.

    `own_size` excludes bytes deduplicated via a content-addressed layer (today
    equal to `size`). `own_time` is wall time minus time spent awaiting child
    Handles. Cached hits report `size = own_size = 0` (nothing was written).
    """

    size: int
    own_size: int
    time: float
    own_time: float

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "own_size": self.own_size,
            "time": self.time,
            "own_time": self.own_time,
        }


@dataclass
class TaskSpan:
    """Runtime state for a single step invocation."""

    id: int
    parent_id: int | None
    name: str
    identity: Identity
    version: Version

    # Populated during execution
    traces: list[TraceRecord] = field(default_factory=lambda: [])
    cached_output_value: Any = field(default=None)
    cached_tracing_value: list[TraceRecord] | None = field(default=None)
    last_trace_ts: float = field(default=0.0)
    child_tasks: list[asyncio.Task[Any]] = field(default_factory=lambda: [])
    start_ts: float = field(default=0.0)
    end_ts: float = field(default=0.0)
    cached: bool = field(default=False)

    # Own-time tracking: wall time minus time spent awaiting child Handles.
    # `suspend_count` counts active Handle awaits (≥1 = this span is suspended);
    # `suspend_start` is the monotonic time the most-recent 0→1 transition began;
    # `suspended_total` accumulates closed intervals when count returns to 0.
    suspend_count: int = field(default=0)
    suspend_start: float = field(default=0.0)
    suspended_total: float = field(default=0.0)

    def enter_await(self) -> None:
        if self.suspend_count == 0:
            self.suspend_start = time.monotonic()
        self.suspend_count += 1

    def exit_await(self) -> None:
        self.suspend_count -= 1
        if self.suspend_count == 0:
            self.suspended_total += time.monotonic() - self.suspend_start

    def record_trace(self, message: str, kwargs: dict[str, Any]) -> None:
        now = time.monotonic()
        delta = now - self.last_trace_ts if self.last_trace_ts > 0 else 0.0
        self.last_trace_ts = now
        self.traces.append(TraceRecord(message, now, delta, kwargs))
