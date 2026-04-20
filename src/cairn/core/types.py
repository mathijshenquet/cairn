"""Core types for Cairn."""

from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import inspect
import json
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any, cast


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


_UNRESOLVED = object()
_MISSING = object()


def _resolve_name(fn: Any, name: str) -> Any:
    code = getattr(fn, "__code__", None)
    if code is not None:
        freevars = getattr(code, "co_freevars", ())
        if name in freevars:
            idx = freevars.index(name)
            closure = getattr(fn, "__closure__", None)
            if closure is not None and idx < len(closure):
                try:
                    return closure[idx].cell_contents
                except ValueError:
                    return _UNRESOLVED
    globals_ = getattr(fn, "__globals__", None)
    if isinstance(globals_, dict) and name in cast(dict[str, Any], globals_):
        return cast(dict[str, Any], globals_)[name]
    if hasattr(builtins, name):
        return getattr(builtins, name)
    return _UNRESOLVED


def _resolve_attribute_chain(fn: Any, node: ast.Attribute) -> tuple[str, Any]:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return "", _MISSING
    parts.append(cur.id)
    parts.reverse()
    dotted = ".".join(parts)

    value: Any = _resolve_name(fn, parts[0])
    if value is _UNRESOLVED:
        return dotted, _MISSING
    for attr in parts[1:]:
        try:
            value = getattr(value, attr)
        except AttributeError:
            return dotted, _MISSING
    return dotted, value


def _collect_refs(tree: ast.AST, fn: Any) -> dict[str, Any]:
    code = getattr(fn, "__code__", None)
    local_names: set[str] = set(getattr(code, "co_varnames", ()))

    refs: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted, value = _resolve_attribute_chain(fn, node)
            if dotted and dotted.split(".", 1)[0] not in local_names:
                refs[dotted] = value
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in local_names or node.id in refs:
                continue
            value = _resolve_name(fn, node.id)
            if value is not _UNRESOLVED:
                refs.setdefault(node.id, value)
    return refs


def _encode_ref(name: str, value: Any, _seen: dict[int, "Version"]) -> str:
    if value is _MISSING:
        return f"{name}=<missing>"
    if inspect.ismodule(value):
        ver = getattr(value, "__version__", None)
        if isinstance(ver, str):
            return f"{name}=<module:{value.__name__}@{ver}>"
        return f"{name}=<module:{value.__name__}>"
    if inspect.isclass(value):
        module = getattr(value, "__module__", "?")
        qualname = getattr(value, "__qualname__", getattr(value, "__name__", "?"))
        return f"{name}=<class:{module}:{qualname}>"
    if inspect.isfunction(value) or inspect.ismethod(value):
        sub = Version.from_function(value, _seen)
        return f"{name}={sub.hash}"
    if inspect.isbuiltin(value):
        module = getattr(value, "__module__", "?")
        qualname = getattr(value, "__qualname__", getattr(value, "__name__", "?"))
        return f"{name}=<builtin:{module}:{qualname}>"
    # Non-callable value: route through resolve_hashable so Path / partial /
    # user-registered hashers apply. Degrade to a stable fallback on TypeError
    # — AST refs include incidental module-level values the function may not
    # actually depend on, so silent passthrough beats blocking decoration.
    from .hash import resolve_hashable

    try:
        resolved = resolve_hashable(value)
        return f"{name}={json.dumps(resolved, sort_keys=True, separators=(',', ':'))}"
    except TypeError:
        if callable(value):
            module = getattr(value, "__module__", "?")
            qualname = getattr(value, "__qualname__", getattr(value, "__name__", "?"))
            return f"{name}=<callable:{module}:{qualname}>"
        return f"{name}=<opaque:{type(value).__name__}>"


class Version:
    """Version identifier derived from function source + resolved refs."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._hash = hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def from_function(cls, fn: object, _seen: dict[int, Version] | None = None) -> Version:
        # Trust an already-attached Version — that's how @step exposes its
        # (possibly user-overridden) version for callers like _hash_partial.
        existing = getattr(fn, "version", None)
        if isinstance(existing, Version):
            return existing

        # Peel @functools.wraps / @step wrappers so we hash the real body,
        # not our own library source.
        try:
            fn = inspect.unwrap(fn)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass

        if _seen is None:
            _seen = {}
        fn_id = id(fn)
        if fn_id in _seen:
            # Two cases collapse here: real cycle (sentinel is still in place
            # from an in-flight call) or duplicate reference to an already-
            # computed function (sentinel has been replaced with the real
            # Version). Returning _seen[fn_id] gives the right answer for both.
            return _seen[fn_id]
        _seen[fn_id] = cls("<cycle>")

        tree: ast.AST | None
        try:
            source = textwrap.dedent(inspect.getsource(fn))  # type: ignore[arg-type]
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            code = getattr(fn, "__code__", None)
            if code is not None:
                source = f"<no-source:co_code={code.co_code.hex()}>"
            else:
                tp = type(fn)
                source = f"<no-source:{tp.__module__}:{tp.__qualname__}>"
            tree = None

        parts: list[str] = [source]
        if tree is not None:
            refs = _collect_refs(tree, fn)
            for name in sorted(refs):
                parts.append(_encode_ref(name, refs[name], _seen))

        result = cls("\n".join(parts))
        _seen[fn_id] = result
        return result

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
