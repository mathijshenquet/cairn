"""Cairn interaction: typed human-in-the-loop primitives + sink protocol.

Design:

- Out-of-band channel. `InteractionSink` has typed methods — one per
  widget kind. Nothing flows through the core event log; the span that
  awaits simply stays `running` until the sink returns. Whether a widget
  renders next to the span tree is a sink implementation detail.

- Typed API. One async wrapper per widget kind, strongly typed at the
  call site. No `schema=` kwarg, no metadata dicts, no stringly-typed
  dispatch.

- Replay. Each wrapper is backed by a memoized `@step`, so answers are
  content-addressed in the output store. Edit the prompt, a choice
  option, or a default, and only that ask re-runs — every other cached
  answer hits.

Usage:

    from cairn.interaction import (
        await_input, await_choice, await_confirm, set_interaction_sink,
        StdinInteractionSink,
    )

    set_interaction_sink(StdinInteractionSink())

    name = await await_input("What's your name?")
    pick = await await_choice("Better?", {"A": text_a, "B": text_b})
    go   = await await_confirm("Proceed?", default=True)
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any, Mapping, Protocol, TypeVar, cast

from cairn.core import step
from cairn.core.context import current_span

K = TypeVar("K")


# ── Protocol ──


class InteractionSink(Protocol):
    """Transport for routing interaction requests to a human (or stand-in).

    `anchor_span` is the span on whose behalf the request is being made —
    the caller of the `await_*` wrapper, not the internal `@step` that
    wraps the sink call. Sinks that render widgets next to the span tree
    (the TUI) use it to attach the widget correctly; headless sinks
    (stdin, queue) ignore it.
    """

    async def request_input(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: str | None = None,
        placeholder: str | None = None,
    ) -> str: ...

    async def request_choice(
        self,
        prompt: str,
        options: Mapping[K, str],
        *,
        anchor_span: int | None,
        default: K | None = None,
    ) -> K: ...

    async def request_confirm(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: bool | None = None,
    ) -> bool: ...


# ── Contextvar plumbing ──


_interaction_sink: ContextVar[InteractionSink | None] = ContextVar(
    "_interaction_sink", default=None
)


def get_interaction_sink() -> InteractionSink | None:
    return _interaction_sink.get()


def set_interaction_sink(sink: InteractionSink) -> Token[InteractionSink | None]:
    return _interaction_sink.set(sink)


def reset_interaction_sink(token: Token[InteractionSink | None]) -> None:
    _interaction_sink.reset(token)


def _require_sink() -> InteractionSink:
    sink = _interaction_sink.get()
    if sink is None:
        raise RuntimeError(
            "no interaction sink registered — call set_interaction_sink(...) "
            "or pass interaction_sink=... to run()."
        )
    return sink


def _caller_span() -> int | None:
    # Inside an `_input` / `_choice` / `_confirm` step, current_span is the
    # step itself; its parent is the user code that called the wrapper.
    # Widgets belong next to the conversation, not the caching wrapper.
    s = current_span.get()
    return s.parent_id if s is not None else None


# ── Memoized internals (one @step per widget) ──


@step(memo=True)
async def _input(
    prompt: str, default: str | None, placeholder: str | None
) -> str:
    return await _require_sink().request_input(
        prompt,
        anchor_span=_caller_span(),
        default=default,
        placeholder=placeholder,
    )


@step(memo=True)
async def _choice(
    prompt: str, options: dict[Any, str], default: Any
) -> Any:
    return await _require_sink().request_choice(
        prompt,
        options,
        anchor_span=_caller_span(),
        default=default,
    )


@step(memo=True)
async def _confirm(prompt: str, default: bool | None) -> bool:
    return await _require_sink().request_confirm(
        prompt,
        anchor_span=_caller_span(),
        default=default,
    )


# ── Public API ──


async def await_input(
    prompt: str,
    *,
    default: str | None = None,
    placeholder: str | None = None,
) -> str:
    """Ask a human for a free-form string.

    Memoized by (prompt, default, placeholder).
    """
    return await _input(prompt, default, placeholder)


async def await_choice(
    prompt: str,
    options: Mapping[K, str],
    *,
    default: K | None = None,
) -> K:
    """Ask a human to pick one of `options`; returns the chosen key.

    Memoized by (prompt, options, default). Changing any key, its
    rendered value, or the default invalidates the cache.
    """
    raw = await _choice(prompt, dict(options), default)
    if raw not in options:
        raise ValueError(
            f"sink returned {raw!r} not in options {list(options)!r}"
        )
    return cast(K, raw)


async def await_confirm(
    prompt: str, *, default: bool | None = None
) -> bool:
    """Ask a yes/no question. Memoized by (prompt, default)."""
    return await _confirm(prompt, default)


# ── Built-in sinks ──


class QueueInteractionSink:
    """Pre-seeded queue, shared across all request_* methods.

    Responses are consumed in FIFO order regardless of request kind — for
    tests and scripted runs where the caller knows the request order.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for r in responses or []:
            self._queue.put_nowait(r)

    def push(self, response: Any) -> None:
        self._queue.put_nowait(response)

    async def request_input(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: str | None = None,
        placeholder: str | None = None,
    ) -> str:
        return cast(str, await self._queue.get())

    async def request_choice(
        self,
        prompt: str,
        options: Mapping[K, str],
        *,
        anchor_span: int | None,
        default: K | None = None,
    ) -> K:
        return cast(K, await self._queue.get())

    async def request_confirm(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: bool | None = None,
    ) -> bool:
        return cast(bool, await self._queue.get())


class StdinInteractionSink:
    """Fallback sink that reads from stdin on a worker thread.

    Safe for sequential requests only. Shows defaults in brackets; an
    empty line accepts the default.
    """

    async def request_input(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: str | None = None,
        placeholder: str | None = None,
    ) -> str:
        hint = f" [{default}]" if default is not None else ""
        line = await asyncio.to_thread(input, f"{prompt.rstrip()}{hint}\n> ")
        if not line and default is not None:
            return default
        return line

    async def request_choice(
        self,
        prompt: str,
        options: Mapping[K, str],
        *,
        anchor_span: int | None,
        default: K | None = None,
    ) -> K:
        keys = list(options.keys())
        print(prompt)
        for k, v in options.items():
            marker = "*" if default is not None and k == default else " "
            print(f"  {marker} [{k}] {v}")
        tail = f" (default {default!r})" if default is not None else ""
        prompt_line = f"Pick one of {keys!r}{tail}\n> "
        while True:
            raw = (await asyncio.to_thread(input, prompt_line)).strip()
            if not raw and default is not None:
                return default
            for k in keys:
                if str(k).lower() == raw.lower():
                    return k
            print(f"invalid choice: {raw!r}")

    async def request_confirm(
        self,
        prompt: str,
        *,
        anchor_span: int | None,
        default: bool | None = None,
    ) -> bool:
        hint = "[y/n]" if default is None else ("[Y/n]" if default else "[y/N]")
        while True:
            line = (await asyncio.to_thread(input, f"{prompt.rstrip()} {hint}\n> ")).strip().lower()
            if not line and default is not None:
                return default
            if line in ("y", "yes"):
                return True
            if line in ("n", "no"):
                return False
            print(f"invalid: expected y/n, got {line!r}")


__all__ = [
    "InteractionSink",
    "await_input",
    "await_choice",
    "await_confirm",
    "get_interaction_sink",
    "set_interaction_sink",
    "reset_interaction_sink",
    "QueueInteractionSink",
    "StdinInteractionSink",
]
