"""Cairn core primitives.

Public surface of `cairn.core` — re-exported from dedicated submodules so
external code can say `from cairn.core import step` without knowing where
each name lives.
"""

from ._step import (
    Handle,
    cached_output,
    cached_tracing,
    get_store,
    reset_store,
    set_store,
    step,
    trace,
)
from .context import (
    Event,
    MemorySink,
    NullSink,
    Sink,
    emit_event,
    get_sink,
    next_id,
    reset_id_counter,
    reset_sink,
    set_sink,
)
from .hash import (
    clear_hash_funcs,
    compute_cache_key,
    register_hash_func,
    resolve_hashable,
    set_hash_funcs,
)
from .patterns import rate_limited, replayable
from .serial import deserialize, register_serializer, serialize
from .sink import JSONLSink, event_to_dict
from .store import FileStore, MemoryStore, Store
from .types import CacheEntry, Identity, TaskSpan, TraceRecord, Version

__all__ = [
    # decorator + Handle
    "step",
    "Handle",
    "trace",
    "cached_output",
    "cached_tracing",
    "get_store",
    "set_store",
    "reset_store",
    # context / event sink
    "Event",
    "Sink",
    "MemorySink",
    "NullSink",
    "get_sink",
    "set_sink",
    "reset_sink",
    "emit_event",
    "next_id",
    "reset_id_counter",
    # hash
    "compute_cache_key",
    "register_hash_func",
    "set_hash_funcs",
    "clear_hash_funcs",
    "resolve_hashable",
    # serial
    "serialize",
    "deserialize",
    "register_serializer",
    # sink
    "JSONLSink",
    "event_to_dict",
    # store
    "Store",
    "MemoryStore",
    "FileStore",
    # patterns
    "rate_limited",
    "replayable",
    # types
    "Identity",
    "Version",
    "TraceRecord",
    "CacheEntry",
    "TaskSpan",
]
