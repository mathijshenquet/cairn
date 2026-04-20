"""Cairn interaction: async human-in-the-loop primitive + sink protocol.

Design goals:
- Decoupled from the UI. Core knows nothing about this module; `cairn.interaction`
  depends only on `cairn.core` for the event sink + id counter.
- Pluggable transport. `InteractionSink` is a Protocol. In-process TUI, HTTP,
  Slack, test harnesses — each implements the same Protocol.
- Replay-friendly. Every request emits `input_request` and `input_response`
  events into the trace, so a later replay can feed back the recorded answer
  without contacting a human.

Usage:
    from cairn.interaction import await_input, set_interaction_sink

    answer = await await_input("Approve this plan?")

Set a sink once at the top of your run:
    set_interaction_sink(StdinInteractionSink())
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from cairn.core import cached_output, emit_event, next_id, step
from cairn.core.context import current_span

T = TypeVar("T")


_SENTINEL = object()


@dataclass
class InputRequest:
    """A single pending request for input.

    `default` is a prefill value from a previous run (via `cached_output()`).
    Sinks should surface it — TUIs as the widget's initial value, CLIs in
    brackets — so the human can accept by hitting enter or edit to change.
    `_SENTINEL` means "no prior value" (distinct from `None` as a real prior
    answer).

    `metadata` is a bag for UI hints (placeholder, multi-line, allowed
    values, etc.) that sinks can use without forcing them into the public
    signature.

    `anchor_span` is the span that should host this request in a UI — the
    span that called `await_input`, not the `_ask` step itself. Sinks use
    it directly instead of reading `current_span` at request time, so replay
    places widgets correctly from the event log alone.
    """

    id: int
    prompt: str
    default: Any = _SENTINEL
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    anchor_span: int | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not _SENTINEL


class InteractionSink(Protocol):
    """Transport for routing input requests to a human (or stand-in)."""

    async def request(self, req: InputRequest) -> Any: ...


# ── Contextvar plumbing (mirrors event sink) ──

_interaction_sink: ContextVar[InteractionSink | None] = ContextVar(
    "_interaction_sink", default=None
)


def get_interaction_sink() -> InteractionSink | None:
    return _interaction_sink.get()


def set_interaction_sink(sink: InteractionSink) -> Token[InteractionSink | None]:
    return _interaction_sink.set(sink)


def reset_interaction_sink(token: Token[InteractionSink | None]) -> None:
    _interaction_sink.reset(token)


# ── Primitive ──


@step
async def _ask(prompt: str, schema_name: str, metadata: dict[str, Any]) -> Any:
    """Core of `await_input`. Runs every time (memo=False) so the human
    stays in the loop; `cached_output()` supplies a prefill from the
    previous run when one exists."""
    sink = _interaction_sink.get()
    if sink is None:
        raise RuntimeError(
            "no interaction sink registered — call set_interaction_sink(...) "
            "or pass interaction_sink=... to run()."
        )

    prior = cached_output()
    self_span = current_span.get()
    # Anchor the UI widget on the caller of `await_input`, not on _ask itself.
    # _ask is a caching wrapper; the caller owns the conversational context.
    anchor = self_span.parent_id if self_span is not None else None
    req = InputRequest(
        id=next_id(),
        prompt=prompt,
        default=prior if prior is not None else _SENTINEL,
        metadata=metadata,
        anchor_span=anchor,
    )
    emit_event(
        "input_request",
        id=req.id,
        message=prompt,
        kwargs={"schema": schema_name, "by": anchor, **metadata},
    )
    if self_span is not None:
        emit_event(
            "wait",
            id=self_span.id,
            kwargs={"on": {"kind": "input", "id": req.id}},
        )
    try:
        response = await sink.request(req)
    finally:
        if self_span is not None:
            emit_event("resume", id=self_span.id)
    emit_event("input_response", id=req.id)
    return response


async def await_input(
    prompt: str, schema: type[T] = str, **metadata: Any
) -> T:  # type: ignore[assignment]
    """Block until an input sink answers `prompt`.

    Memoized: the answer is cached by (prompt, schema, metadata). A re-run
    with the same inputs returns the cached answer without calling the sink.

    Emits `input_request` / `input_response` events on the first run so the
    exchange is captured in the trace log. Raises RuntimeError if no
    interaction sink is registered and the cache misses.
    """
    return await _ask(prompt, schema.__name__, metadata)  # type: ignore[no-any-return]


# ── Built-in sinks ──


class QueueInteractionSink:
    """Pre-seeded queue of responses. For tests and programmatic runs."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for r in responses or []:
            self._queue.put_nowait(r)

    def push(self, response: Any) -> None:
        self._queue.put_nowait(response)

    async def request(self, req: InputRequest) -> Any:
        return await self._queue.get()


class StdinInteractionSink:
    """Fallback sink that reads from stdin on a worker thread.

    Shows a prefill in brackets: `"Favorite colour? [blue]\n> "`. Hitting
    enter on empty input returns the prefill; typing a value overrides it.
    Only safe for a single concurrent request.
    """

    async def request(self, req: InputRequest) -> Any:
        hint = f" [{req.default}]" if req.has_default else ""
        prompt = f"{req.prompt.rstrip()}{hint}\n> "
        answer = await asyncio.to_thread(input, prompt)
        if not answer and req.has_default:
            return req.default
        return answer


__all__ = [
    "InputRequest",
    "InteractionSink",
    "await_input",
    "get_interaction_sink",
    "set_interaction_sink",
    "reset_interaction_sink",
    "QueueInteractionSink",
    "StdinInteractionSink",
]
