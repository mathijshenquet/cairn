"""Event sinks for trace output."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .context import Event


def event_to_dict(event: Event) -> dict[str, Any]:
    """Convert an Event to a JSON-serializable dict."""
    d: dict[str, Any] = {"e": event.kind, "ts": event.ts}
    if event.id is not None:
        d["id"] = event.id
    if event.parent_id is not None:
        d["parent"] = event.parent_id
    if event.name is not None:
        d["name"] = event.name
    if event.message is not None:
        d["msg"] = event.message
    if event.cached is not None:
        d["cached"] = event.cached
    if event.error is not None:
        d["err"] = event.error
    if event.by is not None:
        d["by"] = event.by
    if event.kwargs:
        d.update(event.kwargs)
    return d


class JSONLSink:
    """Writes events as JSONL to a file.

    Each event is one JSON line, flushed immediately.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._closed = False

    def emit(self, event: Event) -> None:
        if self._closed:
            return
        event.ts = time.monotonic()
        line = json.dumps(event_to_dict(event), default=str)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._closed = True
        self._file.close()

    @property
    def path(self) -> str:
        return self._path
