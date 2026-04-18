"""Async wrapper around `claude -p --output-format stream-json`.

Public API:
  claude_stream(prompt, **kwargs) → AsyncIterator[dict]   raw JSONL events
  claude(prompt, **kwargs)        → str                   final text + live traces
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from cairn import trace

SYSTEM_PROMPT = (
    "You are a concise research assistant. Use WebSearch and WebFetch when "
    "current information is needed. Answer directly, no preamble, no "
    "meta-commentary. Respect terseness instructions in the user prompt."
)
MODEL = "haiku"
SEARCH_TOOLS = "WebSearch,WebFetch"
DENY_TOOLS = "Write,Edit,NotebookEdit,Bash,MultiEdit"


async def claude_stream(
    prompt: str,
    *,
    model: str = MODEL,
    system_prompt: str = SYSTEM_PROMPT,
    tools: str = "",
    deny_tools: str = DENY_TOOLS,
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON events from `claude -p --output-format stream-json`."""
    args = [
        "claude", "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        "--permission-mode", "bypassPermissions",
        "--tools", tools,
        "--disallowed-tools", deny_tools,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--",
        prompt,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            pass
    await proc.wait()
    if proc.returncode != 0:
        assert proc.stderr is not None
        stderr = await proc.stderr.read()
        msg = stderr.decode().strip()[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {msg}")


async def claude(
    prompt: str,
    *,
    tools: str = "",
    model: str = MODEL,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    """Run claude, trace tool calls live as they fire, return final text."""
    pending: dict[int, dict[str, str]] = {}  # block index → {name, partial_json}

    async for event in claude_stream(prompt, model=model, system_prompt=system_prompt, tools=tools):
        etype = event.get("type")

        if etype == "stream_event":
            e = event["event"]
            et = e.get("type")

            if et == "content_block_start":
                cb = e.get("content_block", {})
                if cb.get("type") == "tool_use":
                    pending[e["index"]] = {"name": cb["name"], "partial_json": ""}

            elif et == "content_block_delta":
                delta = e.get("delta", {})
                idx = e["index"]
                if delta.get("type") == "input_json_delta" and idx in pending:
                    pending[idx]["partial_json"] += delta.get("partial_json", "")

            elif et == "content_block_stop":
                idx = e.get("index")
                if idx in pending:
                    tool = pending.pop(idx)
                    try:
                        inp: dict[str, Any] = json.loads(tool["partial_json"]) if tool["partial_json"] else {}
                    except json.JSONDecodeError:
                        inp = {"_raw": tool["partial_json"]}
                    trace("tool_call", tool=tool["name"], **inp)

        elif etype == "result":
            if event.get("is_error"):
                raise RuntimeError(f"claude error: {event.get('result', '(no output)')}")
            return event.get("result", "")

    raise RuntimeError("claude stream ended without a result event")
