"""Cache storage backends."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from cairn.types import CacheEntry, TraceRecord


class Store(Protocol):
    """Protocol for cache storage backends."""

    def get(self, key: str) -> CacheEntry | None: ...
    def put(self, key: str, entry: CacheEntry) -> None: ...
    def has(self, key: str) -> bool: ...


class MemoryStore:
    """In-memory store for testing."""

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._data.get(key)

    def put(self, key: str, entry: CacheEntry) -> None:
        self._data[key] = entry

    def has(self, key: str) -> bool:
        return key in self._data


def _trace_to_dict(t: TraceRecord) -> dict[str, Any]:
    return {
        "message": t.message,
        "timestamp": t.timestamp,
        "delta": t.delta,
        "kwargs": t.kwargs,
    }


def _dict_to_trace(d: dict[str, Any]) -> TraceRecord:
    return TraceRecord(
        message=d["message"],
        timestamp=d["timestamp"],
        delta=d["delta"],
        kwargs=d.get("kwargs", {}),
    )


def _entry_to_json(entry: CacheEntry) -> str:
    """Serialize a CacheEntry to JSON."""
    return json.dumps({
        "result": entry.result,
        "traces": [_trace_to_dict(t) for t in entry.traces],
        "duration": entry.duration,
        "error": str(entry.error) if entry.error else None,
    }, sort_keys=True, default=str)


def _json_to_entry(data: str) -> CacheEntry:
    """Deserialize a CacheEntry from JSON."""
    d: dict[str, Any] = json.loads(data)
    return CacheEntry(
        result=d["result"],
        traces=[_dict_to_trace(t) for t in d["traces"]],
        duration=d.get("duration", 0.0),
        error=Exception(d["error"]) if d.get("error") else None,
    )


class FileStore:
    """File-based content-addressed store.

    Stores CacheEntries as JSON files keyed by cache hash.
    Layout: {base_path}/{key}.json
    """

    def __init__(self, base_path: str) -> None:
        self._base = base_path
        os.makedirs(self._base, exist_ok=True)

    @property
    def base_path(self) -> str:
        return self._base

    def _path(self, key: str) -> str:
        return os.path.join(self._base, f"{key}.json")

    def get(self, key: str) -> CacheEntry | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return _json_to_entry(f.read())

    def put(self, key: str, entry: CacheEntry) -> None:
        path = self._path(key)
        # Atomic write: write to temp, then rename
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(_entry_to_json(entry))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def has(self, key: str) -> bool:
        return os.path.exists(self._path(key))
