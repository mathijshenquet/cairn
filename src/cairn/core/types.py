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


def _encode_ref(name: str, value: Any, _seen: dict[int, str]) -> str:
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
        sub = StepInfo.from_function(value, _seen=_seen)
        return f"{name}={sub.version_hash}"
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


def _derive_name(fn: Any) -> str:
    """Module-qualified name for a function: `module:qualname`."""
    module = getattr(fn, "__module__", "<unknown>")
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "<unknown>"))
    return f"{module}:{qualname}"


def _derive_body(fn: Any, _seen: dict[int, str] | None = None) -> str:
    """Canonical body string: source + sorted resolved refs.

    `_seen` dedupes work across a single walk. Cycle sentinel goes in on entry;
    real body string replaces it on exit. Revisit returns `_seen[fn_id]` — the
    cycle sentinel for an in-flight call, the real body for a completed one.
    """
    if _seen is None:
        _seen = {}
    fn_id = id(fn)
    if fn_id in _seen:
        return _seen[fn_id]
    _seen[fn_id] = "<cycle>"

    tree: ast.AST | None
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        code = getattr(fn, "__code__", None)
        if code is not None:
            source = f"<no-source:co_code={code.co_code.hex()}>"
        else:
            tp_name = f"{type(fn).__module__}:{type(fn).__qualname__}"
            source = f"<no-source:{tp_name}>"
        tree = None

    parts: list[str] = [source]
    if tree is not None:
        refs = _collect_refs(tree, fn)
        for name in sorted(refs):
            parts.append(_encode_ref(name, refs[name], _seen))

    body = "\n".join(parts)
    _seen[fn_id] = body
    return body


@dataclass(frozen=True)
class StepInfo:
    """Identification of a step: a nominal name + a structural fingerprint.

    `name` answers "what function is this?" (module:qualname by default, stable
    across edits). `body` answers "which implementation?" (source + resolved
    refs). The two hashes are projections; `cache_key(args)` combines both.
    """

    name: str
    body: str
    identity_hash: str = field(init=False, repr=False, compare=False)
    version_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Precompute hashes once. Frozen-dataclass assignment goes through
        # object.__setattr__. Hashes are derived from name/body so excluding
        # them from eq/hash is just avoiding redundant work.
        object.__setattr__(self, "identity_hash", hashlib.sha256(self.name.encode()).hexdigest())
        object.__setattr__(self, "version_hash", hashlib.sha256(self.body.encode()).hexdigest())

    @classmethod
    def from_function(
        cls,
        fn: object,
        *,
        name: str | None = None,
        body: str | None = None,
        _seen: dict[int, str] | None = None,
    ) -> StepInfo:
        """Derive StepInfo from fn. `name` / `body` override their derivation.

        Respects a pre-attached `.info` (how @step wrappers expose their, possibly
        user-overridden, info to downstream hashers like `_hash_partial`).
        Decorators are peeled via `inspect.unwrap` so the real body is hashed.
        """
        existing = getattr(fn, "info", None)
        if isinstance(existing, StepInfo) and name is None and body is None:
            return existing

        try:
            unwrapped: Any = inspect.unwrap(fn)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            unwrapped = fn

        resolved_name = name if name is not None else _derive_name(unwrapped)
        resolved_body = body if body is not None else _derive_body(unwrapped, _seen)
        return cls(resolved_name, resolved_body)

    def short_version(self) -> str:
        return self.version_hash[:8]

    def cache_key(self, args: dict[str, Any]) -> str:
        from .hash import compute_cache_key

        return compute_cache_key(self.identity_hash, self.version_hash, args)

    def __repr__(self) -> str:
        return f"StepInfo({self.name!r}, v={self.short_version()})"


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
    info: StepInfo

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
