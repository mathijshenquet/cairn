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
from typing import Any, Protocol, TypeVar

from cairn.core import emit_event, next_id, step

T = TypeVar("T")


@dataclass
class InputRequest:
    """A single pending request for input.

    `schema` is the expected type — implementations are free to use it as a
    coercion/validation hint or ignore it. `metadata` is a bag for UI hints
    (placeholder text, multi-line toggle, allowed values, etc.) that sinks
    can use without forcing them into the public signature.
    """

    id: int
    prompt: str
    schema: type = str
    metadata: dict[str, Any] = field(default_factory=dict)


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


@step(memo=True)
async def _ask(prompt: str, schema_name: str, metadata: dict[str, Any]) -> Any:
    """Memoized core of `await_input`. Schema is passed as its name so the
    cache key stays hashable (types aren't hashable by default)."""
    sink = _interaction_sink.get()
    if sink is None:
        raise RuntimeError(
            "no interaction sink registered — call set_interaction_sink(...) "
            "or pass interaction_sink=... to run()."
        )

    req = InputRequest(id=next_id(), prompt=prompt, metadata=metadata)
    emit_event(
        "input_request",
        id=req.id,
        message=prompt,
        kwargs={"schema": schema_name, **metadata},
    )
    response = await sink.request(req)
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

    Only safe for a single concurrent request. Multi-prompt interactive UIs
    should provide their own sink.
    """

    async def request(self, req: InputRequest) -> Any:
        prompt = f"{req.prompt}\n> " if not req.prompt.endswith("\n") else req.prompt
        return await asyncio.to_thread(input, prompt)


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
