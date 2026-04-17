Good reference. Streamlit's design cleanly separates *what* to hash (the function key vs. the call-arg key) from *how* to hash specific types (`hash_funcs`). Let me borrow that type-dispatch pattern and wire `Path` in as a first-class case.

A design question worth surfacing before I write it: **what should `Path` hash to?**

Three reasonable options, and they're not equivalent:

1. **String only.** Hash changes when the path string changes. Fast, no I/O. But two different contents at the same path produce the same hash, which is usually the wrong cache semantics — if the function reads the file, you want the cache to bust when the file changes.
2. **String + mtime.** Hash changes on rename *or* modification. Cheap (one `stat` call). Misses edits that preserve mtime (rare, but `touch -d` and some build tools do it). This is what most caching libraries pick.
3. **String + content hash.** Correct but reads the whole file on every hash. Fine for small config files, bad for large data.

I'll go with **(2) string + mtime** as the default, with hooks for users to register their own type handlers so they can opt into content hashing where it matters. That matches Streamlit's `hash_funcs` design.

A second question: **what about missing paths?** A function might take a `Path` that doesn't exist yet (output path) or might not exist anymore (deleted input). Stat will raise `FileNotFoundError`. I'll treat missing paths as a distinct hash value (`<missing>`) rather than raising, since the function's behavior may legitimately depend on "file doesn't exist" being a stable state.

```python
import ast
import builtins
import hashlib
import inspect
import textwrap
from pathlib import Path
from typing import Any, Callable

_UNRESOLVED = object()
_MISSING = object()

# Type-dispatch registry: maps a type to a function that returns a stable
# string representation for hashing. Subclasses are matched via isinstance
# in registration order, so register more specific types first.
TypeHasher = Callable[[Any], str]
_TYPE_HASHERS: list[tuple[type, TypeHasher]] = []


def register_type_hasher(tp: type, hasher: TypeHasher) -> None:
    """Register a custom hash function for a type. Later registrations for
    the same type take precedence; isinstance matching walks the list in order."""
    _TYPE_HASHERS.insert(0, (tp, hasher))


def _hash_path(p: Path) -> str:
    """Hash a Path by (resolved string, mtime_ns). Missing paths hash to a
    stable sentinel so 'file does not exist' is itself a cacheable state."""
    # Don't resolve() — that follows symlinks and may surprise users who
    # pass a symlink deliberately. Use the string as-given, plus a stat.
    path_str = str(p)
    try:
        st = p.stat()
        return f"path:{path_str}:mtime_ns={st.st_mtime_ns}:size={st.st_size}"
    except FileNotFoundError:
        return f"path:{path_str}:<missing>"
    except OSError as e:
        # Permission denied, broken symlink, etc. Include errno so different
        # failure modes don't collide.
        return f"path:{path_str}:<stat-error:{e.errno}>"


# Register Path (covers PosixPath and WindowsPath via isinstance).
register_type_hasher(Path, _hash_path)


def _resolve_name(fn, name: str) -> Any:
    if name in fn.__code__.co_freevars:
        idx = fn.__code__.co_freevars.index(name)
        return fn.__closure__[idx].cell_contents
    if name in fn.__globals__:
        return fn.__globals__[name]
    if hasattr(builtins, name):
        return getattr(builtins, name)
    return _UNRESOLVED


def _resolve_attribute_chain(fn, node: ast.Attribute) -> tuple[str, Any]:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return "", _MISSING
    parts.append(cur.id)
    parts.reverse()
    dotted = ".".join(parts)

    value: Any = _resolve_name(fn, parts[0])
    if value is _UNRESOLVED:
        return dotted, _MISSING
    for attr in parts[1:]:
        try:
            value = getattr(value, attr)
        except AttributeError:
            return dotted, _MISSING
    return dotted, value


def _hash_value(value: Any) -> str:
    """Stable hash of a value. Checks the type-hasher registry first, then
    falls back to structural hashing for containers, then repr."""
    # Custom type handlers take precedence. Registry is ordered most-specific
    # first (via insert(0, ...)), so first match wins.
    for tp, hasher in _TYPE_HASHERS:
        if isinstance(value, tp):
            try:
                return hashlib.sha256(hasher(value).encode()).hexdigest()[:16]
            except Exception as e:
                # A bad user-registered hasher shouldn't crash the whole hash.
                return hashlib.sha256(
                    f"<hasher-error:{type(value).__name__}:{e}>".encode()
                ).hexdigest()[:16]

    h = hashlib.sha256()
    try:
        if isinstance(value, (str, bytes, int, float, bool, type(None))):
            h.update(repr(value).encode())
        elif isinstance(value, (tuple, list)):
            for item in value:
                h.update(_hash_value(item).encode())
        elif isinstance(value, (set, frozenset)):
            for item in sorted(value, key=repr):
                h.update(_hash_value(item).encode())
        elif isinstance(value, dict):
            for k in sorted(value, key=repr):
                h.update(_hash_value(k).encode())
                h.update(_hash_value(value[k]).encode())
        else:
            h.update(repr(value).encode())
    except Exception:
        h.update(f"<unhashable:{type(value).__name__}>".encode())
    return h.hexdigest()[:16]


def _function_source_key(fn) -> str:
    source = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(source)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _function_hash(fn, _seen: dict[int, str] | None = None) -> str:
    if _seen is None:
        _seen = {}
    fn_id = id(fn)
    if fn_id in _seen:
        return _seen[fn_id]
    _seen[fn_id] = f"<cycle:{fn_id:x}>"

    source = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(source)

    refs: dict[str, Any] = {}
    local_names: set[str] = set(fn.__code__.co_varnames)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted, value = _resolve_attribute_chain(fn, node)
            if dotted and dotted.split(".", 1)[0] not in local_names:
                refs[dotted] = value
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in local_names:
                continue
            value = _resolve_name(fn, node.id)
            if value is not _UNRESOLVED:
                refs.setdefault(node.id, value)

    # Include module + qualname so identically-bodied functions in different
    # locations don't collide. (Borrowed from streamlit's _make_function_key.)
    parts: list[str] = [
        f"module={getattr(fn, '__module__', '?')}",
        f"qualname={getattr(fn, '__qualname__', '?')}",
        _function_source_key(fn),
    ]
    for name in sorted(refs):
        value = refs[name]
        if value is _MISSING:
            parts.append(f"{name}=<missing>")
        elif getattr(value, "__lucid__", False):
            parts.append(f"{name}={_function_hash(value, _seen)}")
        elif inspect.isfunction(value) or inspect.ismethod(value):
            try:
                src = textwrap.dedent(inspect.getsource(value))
                parts.append(f"{name}={hashlib.sha256(src.encode()).hexdigest()[:16]}")
            except (OSError, TypeError):
                parts.append(f"{name}=<opaque-callable:{getattr(value, '__qualname__', '?')}>")
        elif inspect.ismodule(value):
            parts.append(f"{name}=<module:{value.__name__}>")
        elif callable(value) and not any(isinstance(value, tp) for tp, _ in _TYPE_HASHERS):
            # A callable with no registered type hasher: don't try to hash its
            # state. (If someone registers a hasher for e.g. a callable class,
            # the registry check above would have matched first via _hash_value.)
            parts.append(f"{name}=<callable:{type(value).__name__}>")
        else:
            parts.append(f"{name}={_hash_value(value)}")

    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
    _seen[fn_id] = digest
    return digest
```

**What changed**

*Type-dispatch registry.* `_TYPE_HASHERS` is a list of `(type, hasher)` pairs. `_hash_value` consults it before doing anything else, and `register_type_hasher` prepends so later registrations shadow earlier ones and more-specific subclasses can be registered in front of their bases. I picked a list over a dict because you need `isinstance` semantics (a `PosixPath` should match a `Path` registration), and dict lookup by `type(value)` would miss subclasses.

*`_hash_path`.* Returns `path:<string>:mtime_ns=<n>:size=<n>` for existing paths, `:<missing>` for absent ones, `:<stat-error:errno>` for permission issues or broken symlinks. Three design calls worth flagging:
- **Not calling `resolve()`.** If you hand me `./data.csv`, that's what gets hashed, not `/home/you/project/data.csv`. Reason: symlinks are often deliberate (pointing at a "current" build artifact), and resolving them means the hash changes every time the symlink target swaps even if the user didn't intend that. If you want resolved paths, register a custom hasher.
- **Including `size`.** Cheap defense against mtime-preserving edits. Not bulletproof (same-size edits still sneak through), but catches most cases.
- **Not reading contents.** A user who needs content-hashing for a specific path type can `register_type_hasher(Path, lambda p: hashlib.sha256(p.read_bytes()).hexdigest())`, or define a wrapper class and register that.

*Registry also applies to `_function_hash`'s callable branch.* I added `and not any(isinstance(value, tp) ...)` so that if someone registers a hasher for a callable class (say, a dataclass with `__call__`), the registered hasher wins rather than the generic `<callable:...>` fallback.

*Module + qualname in the hash.* Lifted from Streamlit. Prevents two identically-named, identically-bodied functions in different modules from sharing a key. Cheap and worth having.

*Graceful handling of bad user hashers.* If a registered hasher raises, we catch it and embed the error type rather than blowing up the whole hash computation. Matches the spirit of `_hash_value`'s `except Exception` fallback.

**Usage**

```python
# Default Path behavior: string + mtime + size
_function_hash(some_fn)

# Opt into content-hashing for paths in a specific context
def content_hash_path(p: Path) -> str:
    try:
        return f"path:{p}:sha={hashlib.sha256(p.read_bytes()).hexdigest()[:16]}"
    except OSError as e:
        return f"path:{p}:<error:{e.errno}>"

register_type_hasher(Path, content_hash_path)

# Register for your own domain types
register_type_hasher(MyConfig, lambda c: c.version_string)
```

**Still worth deciding**

*Directory paths.* `_hash_path` currently stats a directory the same way as a file — you get the dir's own mtime, which changes when entries are added/removed but not when file *contents* inside change. If directory-as-input is a common case, you probably want a separate hasher that walks the tree. I didn't add one because it's a meaningful I/O cost and should be opt-in.

*Symlinks to missing targets.* `p.stat()` follows symlinks and raises `FileNotFoundError` for a dangling link, so those hash to `<missing>`. If you want to distinguish "no such path" from "dangling symlink", use `p.lstat()` and branch on `p.is_symlink()`.

*Registry is module-global.* Fine for most uses, awkward if two subsystems in the same process want different `Path` semantics. If that's a real concern, thread a `registry` parameter through `_function_hash` and `_hash_value`. I'd wait for the concrete need before adding the plumbing.

Want me to add the test harness now, or push on the directory-hashing case first?

---

Think though as many edge cases and possible failure modes as you can. Classify them into "Doesn't work & fails silently" an "Doesn't work & but (could) notify" the first is obviously a lot worse

---

Good exercise. Let me work through this systematically. I'll separate the two, and within each, group by theme.

## Silent failures (cache returns wrong value, no warning)

These are the dangerous ones because they produce stale hits or false negatives that you only notice when behavior goes wrong.

**Opaque-to-AST behavior dependencies**

- `eval(s)`, `exec(s)`, `globals()['X']`, `getattr(obj, name_str)` — the dependency is encoded in a string we can't follow. Function behavior changes, hash doesn't.
- `open('config.yaml')` inside the function body — we see the `open` builtin and the string constant but don't hash the file's contents. Any function that reads from disk without the path being an argument is invisible to us.
- `os.environ.get('FOO')` — we hash the method reference, not the env var's value.
- Random state, time of day, any process-global state not named in the AST.

These are all fundamental limits of static analysis. Worth being explicit about in docs so users don't assume more coverage than exists.

**Truncating / lossy reprs**

- `numpy.ndarray`, `pandas.DataFrame`, `torch.Tensor` — all have `__repr__` that truncates large objects. Two 10k-element arrays differing only in the middle hash identically via the `repr` fallback. **This is probably the single worst case for the target audience**, since data-shaped globals are common.
- Any object whose `__repr__` is lossy by design (long strings with `...`, big dicts, etc.).

**Non-deterministic `repr` ordering**

- Objects without a user-defined `__repr__` use `<ClassName at 0x7f...>`. When such an object is a dict key or set element, we sort by `repr` — which means sort order depends on memory addresses. The resulting hash is non-deterministic within a single run and across processes. Reads as "cache always misses" (false negative) or "cache hits wrong entry" depending on insertion order.

**`functools.partial` and callable state**

- `partial(fn, x=5)` hits the `<callable:partial>` branch. Changing `x` to `10` doesn't change the hash. This is common enough (config-bound callbacks, DI patterns) that it probably warrants a built-in hasher.
- Callable classes with `__call__` fall into the same bucket unless the class is explicitly registered.
- Bound methods of instances: we hash the method's source but not the instance's state. Usually fine because `self` is a parameter, but if a method closes over `self` implicitly (e.g., nested function inside a method referencing `self`), we miss.

**Path hashing holes**

- **mtime collision within 1-second resolution.** FAT, some NFS mounts, older ext3 — two edits in the same second produce the same mtime. If size also matches, silent hit.
- **Deliberately preserved mtime.** `rsync -a`, `cp --preserve`, `touch -r`, build tools, git checkouts — all can produce a content change with unchanged mtime. Size often changes too, but not always (think byte-patched binaries or format-preserving edits).
- **Directory paths.** A directory's mtime reflects entries added/removed, not content changes within entries. Users hashing a directory expecting content-tracking are silently wrong. (I flagged this before; including here because it's the specific failure mode.)
- **Relative paths.** `Path("data.csv")` hashes as the string `data.csv` regardless of CWD. Different CWDs → same hash → different actual files.
- **Symlink retargeting.** `p.stat()` follows symlinks. If you repoint the symlink to a different file with matching mtime/size, silent hit.
- **Inode replacement.** `mv new_file existing_path` replaces the inode. If mtime and size are preserved, silent hit. Including `st_ino` would help but breaks across remounts.

**Cross-process / cross-version instability**

- **Module version.** A referenced `import numpy` hashes as `<module:numpy>`. Version bumps don't invalidate. Common source of heisenbugs after `pip upgrade`.
- **My cycle sentinel uses `id()`**, so a cyclic graph `A -> B -> A` hashes differently every process. False negatives (cache always misses), not corruption. Fix: use `"<cycle>"` literal — the `_seen` map is doing the actual dedup work, the sentinel's uniqueness isn't load-bearing.
- **The registry isn't part of the hash.** If someone re-registers `Path` with a stricter content-hashing hasher, previously-cached entries look valid but were computed under the old rules.

**Transitive dependencies on non-`__lucid__` functions**

- An imported helper gets its source hashed, but *its* globals and dependencies don't. If `helper.py` uses a module-level config dict that mutates, we miss it. The `__lucid__` escape hatch only helps if you control every function in the chain.

**`co_varnames` miss cases**

- Walrus operators and some comprehension targets (pre-3.12) may or may not appear. If a comprehension variable `x` shadows a global `x`, we'd hash the global's value. Usually false negative (over-invalidation), not corruption, but worth naming.
- Decorators like `@functools.wraps` that set `__wrapped__`: `inspect.getsource(fn)` returns the wrapper's source, not the wrapped function's. Changes inside the wrapped body go invisible.

## Notify-able failures (raises, or produces obviously weird output)

These are recoverable because someone sees something.

**Outright crashes**

- `inspect.getsource(fn)` on REPL-defined functions, C extensions, Cython, or dynamically-compiled code: raises `OSError` / `TypeError`. We catch this for *referenced* functions but not for the top-level `fn` in `_function_hash`. Easy fix: wrap and fall back to `fn.__code__.co_code` + `fn.__qualname__`, matching Streamlit's approach.
- Recursive data structures in `_hash_value`: `d = {}; d['self'] = d` → unbounded recursion. Crash, not corruption. Fix with a `_seen` set.
- Very deep `__lucid__` call graphs: Python stack limit (~1000). Crash.
- Exotic `Path` subclasses without `.stat()` (zip paths, some S3-path libraries): my `except OSError` doesn't catch `AttributeError` or `NotImplementedError`. Broaden the except or check `hasattr(p, 'stat')` first.
- Custom hasher returning non-string: `.encode()` raises. I do catch `Exception`, so this lands in the error-embedded-in-hash path — but that's almost a silent failure. See below.

**Borderline (I classified as notify, but they're close to silent)**

- Custom hasher raises: we embed `<hasher-error:...>` in the hash string. The hash is *stable* (same error, same hash), so cache works, but silently uses a degenerate key. A user who didn't check logs wouldn't know their hasher is broken. Arguably should raise or at least log at WARNING.
- `_hash_value`'s bare `except Exception` wrapping `repr` fallback: if `repr` itself raises (some proxy objects do this), we hash `<unhashable:Type>`. Two different broken-repr objects of the same type collide. Same category as above.

**Stable but surprising behavior**

- Type annotations are hashed. `def f(x: SomeClass) -> OtherClass` pulls `SomeClass` and `OtherClass` into the reference set. With `from __future__ import annotations`, they become strings and aren't resolved — so the same code hashes differently depending on whether that import is present. Surprising, observable, not corrupting.
- `ast.dump` output changes between Python minor versions. Hash cache is not portable across 3.11 → 3.12. Everyone loses their cache on upgrade. Expected, but worth documenting.
- Property access during hashing. `_resolve_attribute_chain` calls `getattr`, which fires `@property` descriptors. If `config.api_key` hits the network, hashing hits the network. Noticeable (slow, observable side effects), not silently wrong.

## The asymmetry worth internalizing

Most of the silent failures share a pattern: **we're statically analyzing source but the dependency is expressed through a runtime-only channel** (string lookup, lossy repr, file content, module version). The fix isn't more cleverness in the analyzer; it's either (a) built-in handlers for common cases (`partial`, numpy arrays, directories) or (b) a clear user-facing contract: "if your function depends on X, and X isn't in the argument list or in a globally-named value we can resolve, register a hasher or pass X as an argument."

The biggest concrete improvements I'd prioritize:

1. **Change the cycle sentinel to `"<cycle>"`** — one-line fix, removes a cross-process instability class.
2. **Built-in `functools.partial` hasher** that recurses into `.func`, `.args`, `.keywords` — handles a very common pattern that currently silently fails.
3. **Refuse to fall through to `repr` for numpy/pandas/torch if they're importable** — either register hashers (content hash for small, shape+dtype+sample for large, with explicit opt-in for full) or raise with a clear "register a hasher for this type" message. Silent lossy-repr collisions are the worst failure mode for the likely user base.
4. **Catch source-retrieval failure at the top level** with a bytecode fallback, matching Streamlit.
5. **Elevate custom-hasher errors from "embedded in key" to "log warning + embed"** so silent breakage is at least traceable.

Want me to roll these into the implementation?