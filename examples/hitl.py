"""Minimal human-in-the-loop example.

Demonstrates `await_input` inside a @step. Run in the TUI:

    cairn examples/hitl.py

An Input widget mounts in the detail pane when each step asks a question.
Submit the value to continue.

Run headless (stdin fallback):

    python examples/hitl.py
"""

from __future__ import annotations

from cairn import run, step, trace
from cairn.interaction import await_input


@step
async def greet() -> str:
    name = await await_input("What's your name?")
    trace("got name", name=name)
    return f"Hello, {name}!"


@step
async def ask_color() -> str:
    return await await_input("Favorite color?")


@step
async def pipeline() -> dict[str, str]:
    greeting = await greet()
    color = await ask_color()
    return {"greeting": greeting, "color": color}


main = pipeline


if __name__ == "__main__":
    from cairn.interaction import StdinInteractionSink, set_interaction_sink

    set_interaction_sink(StdinInteractionSink())
    result = run(pipeline, store_path=".cairn")
    print(result)
