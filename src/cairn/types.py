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

    def record_trace(self, message: str, kwargs: dict[str, Any]) -> None:
        now = time.monotonic()
        delta = now - self.last_trace_ts if self.last_trace_ts > 0 else 0.0
        self.last_trace_ts = now
        self.traces.append(TraceRecord(message, now, delta, kwargs))
