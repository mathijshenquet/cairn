"""Serialization utilities for cache storage."""

from __future__ import annotations

import json
from typing import Any, Callable

# Registry: type -> (serialize, deserialize)
_serializers: dict[type, tuple[Callable[[Any], bytes], Callable[[bytes, type], Any]]] = {}


def register_serializer(
    tp: type,
    serialize: Callable[[Any], bytes],
    deserialize: Callable[[bytes, type], Any],
) -> None:
    """Register a custom serializer for a type."""
    _serializers[tp] = (serialize, deserialize)


def serialize(value: Any) -> bytes:
    """Serialize a value for cache storage."""
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes):
        return value

    # Check registry with MRO
    for tp in type(value).__mro__:
        if tp in _serializers:
            return _serializers[tp][0](value)

    # Fall back to JSON
    try:
        return json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"Cannot serialize type {type(value).__name__}. "
            f"Register a serializer via configure(serializers={{...}})"
        ) from e


def deserialize(data: bytes, type_hint: type | None = None) -> Any:
    """Deserialize a value from cache storage."""
    if type_hint is str:
        return data.decode("utf-8")
    if type_hint is bytes:
        return data

    # Check registry
    if type_hint is not None:
        for tp in type_hint.__mro__:
            if tp in _serializers:
                return _serializers[tp][1](data, type_hint)

    # Fall back to JSON
    return json.loads(data)
