"""Core decorator and Handle implementation."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any, Awaitable, Callable, Generator, Generic, Literal, ParamSpec, TypeVar, overload

from .context import current_span, emit_event, next_id
from .store import MemoryStore, Store
from .types import CacheEntry, SpanMetrics, StepInfo, TaskSpan, TraceRecord

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
                "identity": span.info.name,
                "version": span.info.short_version(),
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


def trace(
    message: str,
    *,
    detail: str = "",
    progress: tuple[int, int] | None = None,
    state: str | None = None,
    level: Literal["info", "warn", "error"] = "info",
    cost: dict[str, int | float] | None = None,
    edge: bool = False,
) -> None:
    """Emit a trace annotation on the current span.

    Fields:
      message  — short label shown on the timeline
      detail   — optional free-form text shown when the trace is selected
      progress — (current, total); renders as a bar
      state    — sub-lifecycle tag ("waiting", "retrying", …)
      level    — severity; "info" (default), "warn", "error"
      cost     — numeric columns summed up the span tree, e.g.
                 {"tokens_in": 10, "tokens_out": 40, "cost_usd": 0.03}
      edge     — mark this trace as an edge annotation (fan-out/retry transition)
    """
    merged: dict[str, Any] = {}
    if detail:
        merged["detail"] = detail
    if progress is not None:
        merged["progress"] = progress
    if state is not None:
        merged["state"] = state
    if level != "info":
        merged["level"] = level
    if cost is not None:
        merged["cost"] = cost
    if edge:
        merged["edge"] = True

    parent = current_span.get()
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


StrOverride = str | Callable[..., str] | None


def _resolve_override(arg: StrOverride, fn: object) -> str | None:
    """Turn an `identity=` or `version=` kwarg into a string override (or None
    to mean "derive from fn")."""
    if arg is None:
        return None
    if isinstance(arg, str):
        return arg
    if callable(arg):
        return arg(fn)
    return None


def _bind_args(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Bind positional and keyword args to parameter names."""
    sig = inspect.signature(fn)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


@overload
def step(fn: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]: ...


@overload
def step(
    fn: Callable[P, Awaitable[R]],
    *,
    memo: bool = ...,
    identity: StrOverride = ...,
    version: StrOverride = ...,
) -> Callable[P, Handle[R]]: ...


@overload
def step(
    *,
    memo: bool = ...,
    identity: StrOverride = ...,
    version: StrOverride = ...,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Handle[R]]]: ...


def step(
    fn: Callable[..., Awaitable[Any]] | None = None,
    *,
    memo: bool = False,
    identity: StrOverride = None,
    version: StrOverride = None,
) -> Any:
    """Decorator that turns an async function into a tracked step.

    By default, the step always runs (memo=False) — suitable for orchestration.
    Use memo=True for expensive leaf operations (API calls, heavy computation)
    to cache results based on (identity, version, args).

    `identity` / `version` override the derived name / body as strings. To
    forward an existing `StepInfo` through a higher-order wrapper, pass
    `identity=info.name, version=info.body`.

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
    identity: StrOverride,
    version: StrOverride,
) -> Any:
    _info = StepInfo.from_function(
        fn,
        name=_resolve_override(identity, fn),
        body=_resolve_override(version, fn),
    )

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Handle[Any]:
        parent = current_span.get()
        span = TaskSpan(
            id=next_id(),
            parent_id=parent.id if parent else None,
            name=fn.__name__,
            info=_info,
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
                key = _info.cache_key(resolved)
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
                    err_key = _info.cache_key(resolved)
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
    wrapper.info = _info  # type: ignore[attr-defined]
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper
