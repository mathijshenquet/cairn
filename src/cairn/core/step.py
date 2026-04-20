"""Core decorator and Handle implementation."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any, Awaitable, Callable, Generator, Generic, ParamSpec, TypeVar, overload

from .context import current_span, emit_event, next_id
from .hash import compute_cache_key
from .store import MemoryStore, Store
from .types import CacheEntry, Identity, SpanMetrics, TaskSpan, TraceRecord, Version

P = ParamSpec("P")
R = TypeVar("R")

# ── Global store (set by Runtime) ──

from contextvars import ContextVar, Token

_store: ContextVar[Store] = ContextVar("_store")
_store_default = MemoryStore()


def get_store() -> Store:
    """Get the current cache store."""
    return _store.get(_store_default)


def set_store(store: Store) -> Token[Store]:
    """Set the cache store."""
    return _store.set(store)


def reset_store(token: Token[Store]) -> None:
    """Reset the store contextvar to its value before `set_store(...)`."""
    _store.reset(token)


# ── Handle ──


class Handle(Generic[R]):
    """Awaitable reference to a running step's eventual result."""

    def __init__(self, span: TaskSpan, task: asyncio.Task[R], args_summary: str = "", memo: bool = False) -> None:
        self._span = span
        self._task = task
        emit_event(
            "spawn",
            id=span.id,
            parent_id=span.parent_id,
            name=span.name,
            kwargs={
                "identity": span.identity.long(),
                "version": span.version.short(),
                "args": args_summary,
                "memo": memo,
            },
        )

    def __await__(self) -> Generator[Any, Any, R]:
        awaiter = current_span.get()
        if awaiter is None:
            return (yield from self._task.__await__())
        emit_event(
            "wait",
            id=awaiter.id,
            kwargs={"on": {"kind": "span", "id": self._span.id}},
        )
        awaiter.enter_await()
        try:
            result = yield from self._task.__await__()
        finally:
            awaiter.exit_await()
            emit_event("resume", id=awaiter.id)
        return result

    def cancel(self) -> None:
        """Cancel the underlying task."""
        self._task.cancel()

    def done(self) -> bool:
        """Check if the task has completed."""
        return self._task.done()

    @property
    def span(self) -> TaskSpan:
        """Access the span for this handle."""
        return self._span


# ── trace() ──


def trace(message: str, detail: str = "", **kwargs: Any) -> None:
    """Emit a trace annotation on the current span.

    `message` is the short label shown on the timeline. `detail` is optional
    markdown-shaped text shown when the trace is selected.

    Blessed kwargs (known to renderers and aggregators):
      progress: tuple[int, int] — (current, total), renders as a bar
      state:    str             — sub-lifecycle tag ("waiting", "retrying", …)
      level:    "info"|"warn"|"error" — severity; default "info"
      cost:     dict[str, int | float] — numeric columns summed up the span
                tree, e.g. {"tokens_in": 10, "tokens_out": 40, "cost_usd": 0.03}

    Any other kwargs are preserved in trace.jsonl and rendered generically.
    """
    parent = current_span.get()
    merged = dict(kwargs)
    if detail:
        merged["detail"] = detail
    emit_event(
        "trace",
        parent_id=parent.id if parent else None,
        message=message,
        kwargs=merged,
    )
    if parent is not None:
        parent.record_trace(message, merged)


# ── cached_output() / cached_tracing() ──


def cached_output() -> Any:
    """Get the previous cached result for the current step, or None."""
    span = current_span.get()
    if span is None:
        return None
    return span.cached_output_value


def cached_tracing() -> list[TraceRecord] | None:
    """Get the previous trace events for the current step, or None."""
    span = current_span.get()
    if span is None:
        return None
    return span.cached_tracing_value


# ── metrics ──


def _compute_metrics(span: TaskSpan, *, size: int, own_size: int) -> SpanMetrics:
    """Build a SpanMetrics for a span whose `start_ts`/`end_ts` are set.

    Invariant: `suspended_total <= wall`. Violation means Handle.enter_await
    and exit_await got unbalanced — a real bug worth surfacing.
    """
    wall = span.end_ts - span.start_ts
    assert span.suspended_total <= wall + 1e-9, (
        f"suspended_total ({span.suspended_total}) exceeds wall ({wall}) on "
        f"span {span.name!r} — enter/exit_await is unbalanced"
    )
    own_time = max(0.0, wall - span.suspended_total)  # clamp FP slop
    return SpanMetrics(size=size, own_size=own_size, time=wall, own_time=own_time)


# ── step decorator ──


def _resolve_identity(
    identity: str | Identity | Callable[..., str | Identity] | None,
    fn: object,
) -> Identity:
    if identity is None:
        return Identity.from_function(fn)
    if isinstance(identity, str):
        return Identity(identity)
    if isinstance(identity, Identity):
        return identity
    if callable(identity):
        result = identity(fn)
        if isinstance(result, str):
            return Identity(result)
        return result
    return Identity.from_function(fn)


def _resolve_version(
    version: str | Version | Callable[..., str | Version] | None,
    fn: object,
) -> Version:
    if version is None:
        return Version.from_function(fn)
    if isinstance(version, str):
        return Version(version)
    if isinstance(version, Version):
        return version
    if callable(version):
        result = version(fn)
        if isinstance(result, str):
            return Version(result)
        return result
    return Version.from_function(fn)


def _bind_args(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Bind positional and keyword args to parameter names."""
    sig = inspect.signature(fn)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


IdentityArg = str | Identity | Callable[..., str | Identity] | None
VersionArg = str | Version | Callable[..., str | Version] | None


@overload
def step(fn: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]: ...


@overload
def step(
    fn: Callable[P, Awaitable[R]],
    *,
    memo: bool = ...,
    identity: IdentityArg = ...,
    version: VersionArg = ...,
) -> Callable[P, Handle[R]]: ...


@overload
def step(
    *,
    memo: bool = ...,
    identity: IdentityArg = ...,
    version: VersionArg = ...,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Handle[R]]]: ...


def step(
    fn: Callable[..., Awaitable[Any]] | None = None,
    *,
    memo: bool = False,
    identity: IdentityArg = None,
    version: VersionArg = None,
) -> Any:
    """Decorator that turns an async function into a tracked step.

    By default, the step always runs (memo=False) — suitable for orchestration.
    Use memo=True for expensive leaf operations (API calls, heavy computation)
    to cache results based on (identity, version, args).

    Returns Handle[T] on call instead of awaiting directly.
    """
    if fn is None:
        # Called with arguments: @step(memo=True)
        def decorator(f: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]:
            return _make_step(f, memo=memo, identity=identity, version=version)
        return decorator

    # Called without arguments: @step
    return _make_step(fn, memo=memo, identity=identity, version=version)


def _make_step(
    fn: Callable[..., Awaitable[Any]],
    *,
    memo: bool,
    identity: str | Identity | Callable[..., str | Identity] | None,
    version: str | Version | Callable[..., str | Version] | None,
) -> Any:
    _identity = _resolve_identity(identity, fn)
    _version = _resolve_version(version, fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Handle[Any]:
        parent = current_span.get()
        span = TaskSpan(
            id=next_id(),
            parent_id=parent.id if parent else None,
            name=fn.__name__,
            identity=_identity,
            version=_version,
        )

        async def run() -> Any:
            token = current_span.set(span)
            span.start_ts = time.monotonic()
            span.last_trace_ts = span.start_ts
            resolved: dict[str, Any] = {}
            try:
                # Resolve Handle arguments (awaits here count toward
                # suspended_total via Handle.__await__, so they don't bleed
                # into own_time).
                bound = _bind_args(fn, args, kwargs)
                for k, v in bound.items():
                    if isinstance(v, Handle):
                        resolved[k] = await v
                    else:
                        resolved[k] = v

                # Cache lookup
                store = get_store()
                key = compute_cache_key(_identity.hash, _version.hash, resolved)
                cached = store.get(key)

                if cached is not None and cached.error is None:
                    span.cached_output_value = cached.result
                    span.cached_tracing_value = cached.traces
                    if memo:
                        span.cached = True
                        span.end_ts = time.monotonic()
                        metrics = _compute_metrics(span, size=0, own_size=0)
                        emit_event(
                            "end",
                            id=span.id,
                            cached=True,
                            kwargs={"cache_key": key, **metrics.as_kwargs()},
                        )
                        return cached.result

                # Execute the step body
                emit_event("start", id=span.id)
                result = await fn(**resolved)

                # Structured concurrency cleanup counts as await time on this
                # span — the body has finished its work; this is just waiting.
                if span.child_tasks:
                    cleanup_start = time.monotonic()
                    await asyncio.gather(*span.child_tasks, return_exceptions=True)
                    span.suspended_total += time.monotonic() - cleanup_start

                span.end_ts = time.monotonic()
                wall = span.end_ts - span.start_ts
                own_time = wall - span.suspended_total

                # Store result
                stats = store.put(key, CacheEntry(
                    result=result,
                    traces=list(span.traces),
                    duration=wall,
                    own_duration=own_time,
                ))
                metrics = _compute_metrics(span, size=stats.size, own_size=stats.own_size)
                emit_event(
                    "end",
                    id=span.id,
                    kwargs={"cache_key": key, **metrics.as_kwargs()},
                )
                return result

            except BaseException as exc:
                # Non-cancel errors: let siblings finish before the exception
                # propagates. Without this, `asyncio.run()`'s teardown cancels
                # every still-running task in the loop — meaning an error in
                # one fan-out branch kills the others mid-flight, wasting
                # their work. Cancellation itself is left to propagate fast.
                if (
                    not isinstance(exc, asyncio.CancelledError)
                    and span.child_tasks
                    and any(not t.done() for t in span.child_tasks)
                ):
                    cleanup_start = time.monotonic()
                    await asyncio.gather(*span.child_tasks, return_exceptions=True)
                    span.suspended_total += time.monotonic() - cleanup_start

                span.end_ts = time.monotonic()
                if isinstance(exc, asyncio.CancelledError):
                    emit_event("cancel", id=span.id)
                else:
                    # Store error for browsability (keyed to resolved args)
                    store = get_store()
                    err_key = compute_cache_key(_identity.hash, _version.hash, resolved)
                    stored_error: Exception | None = exc if isinstance(exc, Exception) else None
                    wall = span.end_ts - span.start_ts
                    own_time = wall - span.suspended_total
                    stats = store.put(err_key, CacheEntry(
                        result=None,
                        traces=list(span.traces),
                        error=stored_error,
                        duration=wall,
                        own_duration=own_time,
                    ))
                    metrics = _compute_metrics(span, size=stats.size, own_size=stats.own_size)
                    emit_event(
                        "error",
                        id=span.id,
                        error=str(exc),
                        kwargs=metrics.as_kwargs(),
                    )
                raise

            finally:
                current_span.reset(token)

        # Build short args summary for display
        def _summarize_arg(v: Any) -> str:
            if isinstance(v, Handle):
                return "..."
            s = repr(v)
            return s if len(s) <= 30 else s[:27] + "..."

        try:
            bound_preview = _bind_args(fn, args, kwargs)
            args_parts = [f"{_summarize_arg(v)}" for v in bound_preview.values()]
            args_summary = ", ".join(args_parts)
        except Exception:
            args_summary = ""

        task = asyncio.create_task(run())

        # Register with parent for structured concurrency
        if parent is not None:
            parent.child_tasks.append(task)

        return Handle(span, task, args_summary, memo)

    # Attach metadata
    wrapper.identity = _identity  # type: ignore[attr-defined]
    wrapper.version = _version  # type: ignore[attr-defined]
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper
