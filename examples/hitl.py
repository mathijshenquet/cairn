"""Minimal human-in-the-loop example.

Demonstrates `await_input` inside a @step. Run in the TUI:

    cairn examples/hitl.py

An Input widget mounts in the detail pane when each step asks a question.
Submit the value to continue.

Run headless (stdin fallback):

    python examples/hitl.py
"""

from __future__ import annotations
import asyncio

from cairn import run, step, trace
from cairn.interaction import await_input


@step
async def greet() -> str:
    name = await await_input("What's your name?")
    trace(f"Hello, {name}!")
    return name


@step
async def ask(thing: str) -> str:
    return await await_input(f"Favorite {thing}?")


@step
async def pipeline() -> str:
    name = await greet()

    favorites_ = {thing: ask(thing) for thing in ["color", "food"]}

    favorites = {thing: await fav for thing, fav in favorites_.items()}

    return "{name} likes {color} {food}'s".format(name=name, **favorites)


main = pipeline


if __name__ == "__main__":
    from cairn.interaction import StdinInteractionSink, set_interaction_sink

    set_interaction_sink(StdinInteractionSink())
    result = run(pipeline, store_path=".cairn")
    print(result)
