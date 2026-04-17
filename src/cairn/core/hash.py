"""Hashing utilities for cache key computation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, cast

# Global registry: type -> hash function
_hash_funcs: dict[type, Callable[[Any], Any]] = {}


def register_hash_func(tp: type, func: Callable[[Any], Any]) -> None:
    """Register a custom hash function for a type."""
    _hash_funcs[tp] = func


def set_hash_funcs(funcs: dict[type, Callable[[Any], Any]]) -> None:
    """Bulk-register hash functions."""
    _hash_funcs.update(funcs)


def clear_hash_funcs() -> None:
    """Clear all registered hash functions."""
    _hash_funcs.clear()


def resolve_hashable(value: Any) -> Any:
    """Turn any value into a canonical tree of primitives for hashing.

    Returns a JSON-serializable structure.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        d = cast(dict[Any, Any], value)
        return {"__dict__": {str(k): resolve_hashable(v) for k, v in sorted(d.items())}}
    if isinstance(value, (list, tuple)):
        seq = cast(list[Any] | tuple[Any, ...], value)
        tag = "__list__" if isinstance(value, list) else "__tuple__"
        return {tag: [resolve_hashable(v) for v in seq]}
    if isinstance(value, frozenset):
        fs = cast(frozenset[Any], value)
        resolved: list[Any] = [resolve_hashable(v) for v in fs]
        return {"__frozenset__": resolved}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}

    # Check registry with MRO
    for tp in type(value).__mro__:
        if tp in _hash_funcs:
            return resolve_hashable(_hash_funcs[tp](value))

    raise TypeError(
        f"Unhashable type for cache key: {type(value).__name__}. "
        f"Register a hash function via configure(hash_funcs={{...}})"
    )


def compute_cache_key(identity_hash: str, version_hash: str, resolved_args: dict[str, Any]) -> str:
    """Compute a cache key from identity, version, and resolved arguments."""
    canonical = json.dumps(
        {
            "identity": identity_hash,
            "version": version_hash,
            "args": resolve_hashable(resolved_args),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
