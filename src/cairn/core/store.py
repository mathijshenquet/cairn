"""Cache storage backends."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .types import CacheEntry, TraceRecord


@dataclass(frozen=True)
class StoreStats:
    """Size metrics for a stored entry.

    `own_size` excludes bytes shared with other entries (dedup). Without a
    content-addressed layer it equals `size`; the split exists so the API
    survives a future L0/CAS split without churn.
    """

    size: int
    own_size: int


class Store(Protocol):
    """Protocol for cache storage backends."""

    def get(self, key: str) -> CacheEntry | None: ...
    def put(self, key: str, entry: CacheEntry) -> StoreStats: ...
    def has(self, key: str) -> bool: ...


class MemoryStore:
    """In-memory store for testing."""

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._data.get(key)

    def put(self, key: str, entry: CacheEntry) -> StoreStats:
        self._data[key] = entry
        size = len(_entry_to_json(entry).encode("utf-8"))
        return StoreStats(size=size, own_size=size)

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
        "own_duration": entry.own_duration,
        "error": str(entry.error) if entry.error else None,
    }, sort_keys=True, default=str)


def _json_to_entry(data: str) -> CacheEntry:
    """Deserialize a CacheEntry from JSON."""
    d: dict[str, Any] = json.loads(data)
    return CacheEntry(
        result=d["result"],
        traces=[_dict_to_trace(t) for t in d["traces"]],
        duration=d.get("duration", 0.0),
        own_duration=d.get("own_duration", 0.0),
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

    def put(self, key: str, entry: CacheEntry) -> StoreStats:
        path = self._path(key)
        payload = _entry_to_json(entry)
        # Atomic write: write to temp, then rename
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        size = len(payload.encode("utf-8"))
        return StoreStats(size=size, own_size=size)

    def has(self, key: str) -> bool:
        return os.path.exists(self._path(key))
